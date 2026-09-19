"""Create (or update) the Azure AI Search index used by the Foundry agent.

Defaults to whole sections of structured, dated meeting DOCX files under
``data/``, excluding designated policy folders. Explicit ``CHUNKING_MODE=window``
and ``DOCUMENT_SCOPE=all`` retain general DOCX/PDF/Markdown/text ingestion.
The index stores both text and embeddings and has a semantic configuration;
the agent query mode defaults to lexical/BM25 with semantic reranking.

Each chunk is enriched with date metadata extracted from the document filename
(pattern: ``Board Meeting – DD Month YYYY``). A ``meeting_date`` field enables
OData date-range filtering, and ``year``/``month`` fields support faceting. The
document title and date are prepended to every chunk so that embeddings always
carry temporal context — fixing issues where relative-date queries (e.g.
"February last year") retrieved the wrong meeting.

Embeddings are generated against the Foundry resource's Azure OpenAI route
(``/openai/deployments/<dep>/embeddings``), authenticated with
``DefaultAzureCredential`` — no separate ``AZURE_OPENAI_ENDPOINT`` required.

The vector dimension is **auto-detected** from the deployment at runtime, so
switching between ``text-embedding-3-small`` (1536) and ``text-embedding-3-large``
(3072) — or any other embedding model — is configurable. Section mode requires
a new index version for changed documents/layout/dimensions. The legacy window
path retains explicit ``RECREATE_INDEX=true``; section mode rejects it.

Required environment variables (see ``.env.example``):
    AZURE_SEARCH_ENDPOINT      https://<svc>.search.windows.net
    SEARCH_INDEX_NAME          e.g. knowledge-index
    PROJECT_ENDPOINT           https://<resource>.services.ai.azure.com/api/projects/<project>
    EMBEDDING_DEPLOYMENT       Foundry-deployed embedding model
                               (default: text-embedding-3-small)

Optional:
    AZURE_OPENAI_API_VERSION   default: 2024-10-21
    AZURE_SEARCH_API_KEY       if unset, uses DefaultAzureCredential
    DATA_DIR                   default: ./data
    CHUNKING_MODE              section (default), or window
    DOCUMENT_SCOPE            minutes (default), or all with window mode
    CHUNK_SIZE                 window mode chars per chunk, default: 1200
    CHUNK_OVERLAP              window mode char overlap, default: 200
    RECREATE_INDEX             window-only drop+recreate, default: false

Auth: ``az login``. Signed-in user needs:
  - "Search Index Data Contributor" + "Search Service Contributor" on the search service
  - "Foundry User" on the Foundry **account** (not the project) — this is what grants
    ``Microsoft.CognitiveServices/*`` data actions, including the embeddings call below.
    Subscription Owner/Contributor are control-plane roles and are NOT sufficient.
    ``azd up`` assigns this for you; a freshly-created assignment can take several
    minutes to take effect, which the script waits out.

Usage:
    uv run python scripts/setup_aisearch_index.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from openai import AzureOpenAI
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.document_titles import display_document_title
from backend.document_sections import SECTION_FIELDS, build_evidence_chunks, section_document
from docx import Document
from pypdf import PdfReader
from dotenv import load_dotenv

from rbac_propagation import wait_for_data_plane

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("setup-aisearch")

EMBED_DIM_DEFAULT = 1536  # text-embedding-3-small; auto-detected at runtime
# Internal search-index structural identifiers. These are arbitrary names
# baked into the index definition (not used by the runtime backend, which
# queries via a plain filter, nor by the agent's VECTOR_SIMPLE_HYBRID tool).
# Exposed as env overrides so an existing index built with different names
# (e.g. a prior client deployment) stays compatible without code changes.
VECTOR_PROFILE = os.getenv("SEARCH_VECTOR_PROFILE", "default-vector-profile")
HNSW_ALGO = os.getenv("SEARCH_HNSW_ALGO", "default-hnsw")
SEMANTIC_CONFIG = os.getenv("SEARCH_SEMANTIC_CONFIG", "default-semantic")
VECTORIZER_NAME = os.getenv("SEARCH_VECTORIZER", "default-vectorizer")


# ---------- settings ----------

def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        log.error("Missing required env var: %s", name)
        sys.exit(1)
    return val


def load_settings() -> dict:
    mode = os.getenv("CHUNKING_MODE", "section").strip().lower()
    scope = os.getenv("DOCUMENT_SCOPE", "minutes").strip().lower()
    if mode not in ("window", "section"):
        raise ValueError("CHUNKING_MODE must be 'window' or 'section'")
    if scope not in ("all", "minutes"):
        raise ValueError("DOCUMENT_SCOPE must be 'all' or 'minutes'")
    if mode == "section" and scope != "minutes":
        raise ValueError("Section mode requires DOCUMENT_SCOPE=minutes; policy material is not included")
    settings = {
        "search_endpoint": _require("AZURE_SEARCH_ENDPOINT").rstrip("/"),
        "index_name": _require("SEARCH_INDEX_NAME"),
        "search_key": os.getenv("AZURE_SEARCH_API_KEY", "").strip(),
        "project_endpoint": _require("PROJECT_ENDPOINT").rstrip("/"),
        "embed_deployment": os.getenv("EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
        "aoai_api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        "data_dir": Path(os.getenv("DATA_DIR", "data")).resolve(),
        "chunk_size": int(os.getenv("CHUNK_SIZE", "1200")),
        "chunk_overlap": int(os.getenv("CHUNK_OVERLAP", "200")),
        "recreate": os.getenv("RECREATE_INDEX", "false").lower() == "true",
        "chunking_mode": mode,
        "document_scope": scope,
        "vector_profile": os.getenv("SEARCH_VECTOR_PROFILE", "default-vector-profile"),
        "hnsw_algo": os.getenv("SEARCH_HNSW_ALGO", "default-hnsw"),
        "semantic_config": os.getenv("SEARCH_SEMANTIC_CONFIG", "default-semantic"),
        "vectorizer": os.getenv("SEARCH_VECTORIZER", "default-vectorizer"),
        # Filled in by detect_embed_dim() before ensure_index() runs.
        "embed_dim": None,
    }
    if mode == "section" and settings["recreate"]:
        raise ValueError("Section indexes are versioned: choose a new SEARCH_INDEX_NAME instead of RECREATE_INDEX")
    if settings["chunk_size"] <= 0 or not 0 <= settings["chunk_overlap"] < settings["chunk_size"]:
        raise ValueError("CHUNK_SIZE must be positive and CHUNK_OVERLAP must be smaller than it")
    return settings


# ---------- date extraction ----------

# Matches patterns like "Board Meeting – 15 February 2026" or "Board Meeting - 5 March 2019"
_DATE_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{4})",
    re.IGNORECASE,
)


def parse_meeting_date(filename: str) -> Optional[datetime]:
    """Extract meeting date from filename. Returns None if no date found."""
    m = _DATE_RE.search(filename)
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    try:
        return datetime(year, _month_number(month_name), day)
    except ValueError:
        return None


def _month_number(name: str) -> int:
    months = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    return months.index(name.lower()) + 1


# ---------- clients ----------

def _aad():
    return DefaultAzureCredential(process_timeout=90)


def make_index_client(s: dict) -> SearchIndexClient:
    cred = AzureKeyCredential(s["search_key"]) if s["search_key"] else _aad()
    return SearchIndexClient(endpoint=s["search_endpoint"], credential=cred)


def make_search_client(s: dict) -> SearchClient:
    cred = AzureKeyCredential(s["search_key"]) if s["search_key"] else _aad()
    return SearchClient(endpoint=s["search_endpoint"], index_name=s["index_name"], credential=cred)


def make_embeddings_client(s: dict):
    """AzureOpenAI client pointed at the Foundry resource (for embeddings).

    Foundry's OpenAI-compat (/openai/v1/...) only serves chat/responses; the
    embeddings endpoint lives under the AOAI route /openai/deployments/<dep>/embeddings,
    which is reachable at the Foundry resource root.
    """
    parsed = urlparse(s["project_endpoint"])
    azure_endpoint = f"{parsed.scheme}://{parsed.netloc}"
    token_provider = get_bearer_token_provider(_aad(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(
        azure_endpoint=azure_endpoint,
        api_version=s["aoai_api_version"],
        azure_ad_token_provider=token_provider,
    )


# ---------- index ----------

def build_index(name: str, s: dict) -> SearchIndex:
    embed_dim = s.get("embed_dim") or EMBED_DIM_DEFAULT
    vector_profile = s.get("vector_profile", VECTOR_PROFILE)
    hnsw_algo = s.get("hnsw_algo", HNSW_ALGO)
    vectorizer_name = s.get("vectorizer", VECTORIZER_NAME)
    semantic_config = s.get("semantic_config", SEMANTIC_CONFIG)
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="source", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="documentType", type=SearchFieldDataType.String,
                    filterable=True, facetable=True),
        SimpleField(name="meeting_date", type=SearchFieldDataType.DateTimeOffset,
                    filterable=True, sortable=True, facetable=True),
        SimpleField(name="year", type=SearchFieldDataType.Int32,
                    filterable=True, sortable=True, facetable=True),
        SimpleField(name="month", type=SearchFieldDataType.Int32,
                    filterable=True, sortable=True, facetable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=embed_dim,
            vector_search_profile_name=vector_profile,
        ),
    ]
    if s.get("chunking_mode", "section") == "section":
        fields.extend([
            SimpleField(name="section", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="source_sha256", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="source_paragraph", type=SearchFieldDataType.Int32, filterable=True),
        ])

    # Build the Azure OpenAI vectorizer so AI Search can auto-vectorize text queries
    parsed = urlparse(s["project_endpoint"])
    azure_endpoint = f"{parsed.scheme}://{parsed.netloc}"

    vectorizer = AzureOpenAIVectorizer(
        vectorizer_name=vectorizer_name,
        parameters=AzureOpenAIVectorizerParameters(
            resource_url=azure_endpoint,
            deployment_name=s["embed_deployment"],
            model_name=s["embed_deployment"],
        ),
    )

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=hnsw_algo,
                parameters=HnswParameters(metric=VectorSearchAlgorithmMetric.COSINE),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=vector_profile,
                algorithm_configuration_name=hnsw_algo,
                vectorizer_name=vectorizer_name,
            )
        ],
        vectorizers=[vectorizer],
    )

    semantic_search = SemanticSearch(
        default_configuration_name=semantic_config if s.get("chunking_mode", "section") == "section" else None,
        configurations=[
            SemanticConfiguration(
                name=semantic_config,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content")],
                ),
            )
        ]
    )

    # No scoring profile. Earlier rounds set a server-side "recency-boost"
    # default that boosted newer docs on every query — it fixed "last meeting"
    # but broke "first meeting" / "Nigeria 2008" / any historical lookup
    # (old docs were always buried). Recency is now handled OUTSIDE the
    # index: the backend injects a MEETINGS LIST (date catalogue) as a
    # system message at session start so the model can resolve
    # first / last / count / listing questions directly without searching,
    # and can phrase precise content searches using the right meeting date.
    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )


def ensure_index(s: dict) -> None:
    client = make_index_client(s)
    existing_indexes = {i.name: i for i in client.list_indexes()}
    mode = s.get("chunking_mode", "section")
    if s["index_name"] in existing_indexes:
        existing = existing_indexes[s["index_name"]]
        fields = {field.name for field in existing.fields}
        has_section_fields = bool(fields & SECTION_FIELDS)
        if (mode == "section") != has_section_fields:
            raise ValueError("Chunking layout cannot be changed in place. Choose a new versioned SEARCH_INDEX_NAME; the existing index is retained.")
        if mode == "section":
            if not SECTION_FIELDS <= fields:
                raise ValueError("Existing section index schema is incomplete; use a new index version")
            expected_semantic = s.get("semantic_config", SEMANTIC_CONFIG)
            if not existing.semantic_search or existing.semantic_search.default_configuration_name != expected_semantic:
                raise ValueError("Existing semantic configuration differs; use a new index version")
            if existing.scoring_profiles or existing.default_scoring_profile:
                raise ValueError("Existing index has a different scoring profile; use a new index version")
            s["section_index_exists"] = True
        else:
            s["section_index_exists"] = False
    else:
        s["section_index_exists"] = False

    # Vector dimensions are immutable on an existing index. If the user
    # switched embedding models without setting RECREATE_INDEX=true, the
    # downstream create_or_update_index call would fail with a confusing
    # server-side error — surface the real cause up front.
    if s["index_name"] in existing_indexes and not s["recreate"]:
        existing = existing_indexes[s["index_name"]]
        existing_dim = next(
            (
                getattr(f, "vector_search_dimensions", None)
                for f in existing.fields
                if f.name == "content_vector"
            ),
            None,
        )
        if existing_dim and s.get("embed_dim") and existing_dim != s["embed_dim"]:
            log.error(
                "Existing index '%s' has vector dim %d but the deployment '%s' produces %d-dim embeddings. "
                "Vector dimensions are immutable on an existing index — set RECREATE_INDEX=true to drop "
                "and rebuild from scratch.",
                s["index_name"], existing_dim, s["embed_deployment"], s["embed_dim"],
            )
            sys.exit(2)

    if mode == "section" and s.get("recreate"):
        raise ValueError("Section indexes are immutable versions; RECREATE_INDEX is not allowed")
    if s.get("section_index_exists"):
        log.info("Retaining existing section index '%s'; exact corpus verification follows.", s["index_name"])
        return
    if s["index_name"] in existing_indexes and s["recreate"]:
        log.info("Deleting existing index '%s'", s["index_name"])
        client.delete_index(s["index_name"])
        existing_indexes.pop(s["index_name"], None)

    if s["index_name"] in existing_indexes:
        log.info("Updating index '%s'", s["index_name"])
        client.create_or_update_index(build_index(s["index_name"], s))
    else:
        log.info("Creating index '%s'", s["index_name"])
        client.create_index(build_index(s["index_name"], s))


# ---------- ingest ----------

def read_docx(path: Path) -> str:
    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            parts.append(text)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def read_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "").strip() for page in reader.pages)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# Map file extension -> reader. Extend here to support more formats.
READERS = {
    ".docx": read_docx,
    ".pdf": read_pdf,
    ".md": read_text,
    ".markdown": read_text,
    ".txt": read_text,
}


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                idx = window.rfind(sep)
                if idx >= int(size * 0.6):
                    end = start + idx + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def embed_batch(client, deployment: str, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=deployment, input=texts)
    if len(resp.data) != len(texts) or {item.index for item in resp.data} != set(range(len(texts))):
        raise RuntimeError("Embedding response does not map one-to-one to input chunks")
    return [item.embedding for item in sorted(resp.data, key=lambda item: item.index)]


def detect_embed_dim(client, deployment: str) -> int:
    """Probe the deployment to learn its embedding output dimension.

    Lets the script work with any embedding model (3-small=1536, 3-large=3072,
    ada-002=1536, etc.) without hardcoding. Costs one tiny embedding call at
    setup time.
    """
    resp = client.embeddings.create(model=deployment, input=["dimension probe"])
    return len(resp.data[0].embedding)


DOC_TYPE_MINUTES = "MeetingMinutes"
DOC_TYPE_POLICY = "Policy"

# Sub-directories of data/ whose contents are policy documents rather than minutes.
POLICY_DIRS = {"policies"}

# Repo documentation that lives alongside the corpus but is NOT corpus content.
# data/README.md explains how to build the index; indexing it would let build
# instructions surface as if they were an executive document.
EXCLUDED_STEMS = {"readme"}


def classify_document(path: Path, data_dir: Path) -> str:
    """Derive documentType from where the file sits under ``data/``.

    Anything beneath ``data/policies/`` is a Policy; everything else is treated as
    meeting minutes. The value is both indexed as a filterable field and prepended
    into the chunk text, because only the text form participates in the embedding
    and the BM25 index -- the field alone cannot influence retrieval.
    """
    try:
        rel = path.relative_to(data_dir)
    except ValueError:
        return DOC_TYPE_MINUTES
    parents = {part.lower() for part in rel.parts[:-1]}
    return DOC_TYPE_POLICY if parents & POLICY_DIRS else DOC_TYPE_MINUTES


def document_files(s: dict) -> list[Path]:
    return sorted(
        f for f in s["data_dir"].rglob("*")
        if f.is_file()
        and f.suffix.lower() in READERS
        and f.stem.lower() not in EXCLUDED_STEMS
        and (s.get("document_scope", "minutes") == "all" or classify_document(f, s["data_dir"]) == DOC_TYPE_MINUTES)
    )


def prepare_section_documents(s: dict) -> list[dict]:
    files = document_files(s)
    if not files:
        raise ValueError("Section mode requires original meeting documents")
    if len({path.name for path in files}) != len(files):
        raise ValueError("Section mode requires unique source filenames across DATA_DIR")
    documents = []
    for path in files:
        if path.suffix.lower() != ".docx" or classify_document(path, s["data_dir"]) != DOC_TYPE_MINUTES:
            raise ValueError(f"{path.name}: section mode supports structured meeting DOCX files only")
        meeting_dt = parse_meeting_date(path.stem)
        if meeting_dt is None:
            raise ValueError(f"{path.name}: a meeting date is required for section mode")
        doc = Document(str(path))
        if doc.tables:
            raise ValueError(f"{path.name}: table-bearing minutes need an explicitly reviewed section layout")
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        for tag in ("ins", "del", "footnoteReference", "endnoteReference", "drawing", "pict"):
            if next(doc.element.iter(f"{namespace}{tag}"), None) is not None:
                raise ValueError(f"{path.name}: {tag} needs review before section ingestion")
        documents.append(section_document(
            path.name, hashlib.sha256(path.read_bytes()).hexdigest(),
            meeting_dt, (paragraph.text for paragraph in doc.paragraphs),
        ))
    return build_evidence_chunks(documents, "section")


def iter_documents(s: dict, aoai) -> Iterable[dict]:
    if s.get("chunking_mode", "section") == "section":
        documents = s.get("section_documents")
        if documents is None:
            documents = prepare_section_documents(s)
        for offset in range(0, len(documents), 16):
            batch = documents[offset:offset + 16]
            vectors = embed_batch(aoai, s["embed_deployment"], [doc["content"] for doc in batch])
            for doc, vector in zip(batch, vectors):
                yield {**doc, "content_vector": vector}
        return
    files = document_files(s)
    if not files:
        log.warning("No supported files (%s) found in %s",
                    ", ".join(sorted(READERS)), s["data_dir"])
        return

    for path in files:
        reader = READERS[path.suffix.lower()]
        doc_type = classify_document(path, s["data_dir"])
        title = display_document_title(path.stem, doc_type)
        log.info("Reading %s  [%s]", path.name, doc_type)
        try:
            raw = reader(path)
        except Exception as e:
            log.warning("  failed to read %s: %s", path.name, e)
            continue

        # Extract meeting date from filename
        meeting_dt = parse_meeting_date(path.stem)
        if meeting_dt:
            date_prefix = (
                f"[Document: {title} | Type: {doc_type} | "
                f"Meeting Date: {meeting_dt.strftime('%d %B %Y')}]\n\n"
            )
            log.info("  meeting date: %s", meeting_dt.strftime("%Y-%m-%d"))
        else:
            date_prefix = f"[Document: {title} | Type: {doc_type}]\n\n"
            if doc_type == DOC_TYPE_MINUTES:
                log.warning("  no date found in filename")

        chunks = chunk_text(raw, s["chunk_size"], s["chunk_overlap"])
        if not chunks:
            log.warning("  no text extracted from %s", path.name)
            continue
        log.info("  %d chunks", len(chunks))

        # Prepend date/title context to each chunk for embedding
        enriched_chunks = [date_prefix + chunk for chunk in chunks]

        BATCH = 16
        for i in range(0, len(enriched_chunks), BATCH):
            batch = enriched_chunks[i : i + BATCH]
            vectors = embed_batch(aoai, s["embed_deployment"], batch)
            for j, (text, vec) in enumerate(zip(batch, vectors)):
                idx = i + j
                doc = {
                    "id": f"{uuid.uuid5(uuid.NAMESPACE_URL, f'{path.name}:{idx}')}",
                    "title": title,
                    "source": path.name,
                    "documentType": doc_type,
                    "chunk_index": idx,
                    "content": text,
                    "content_vector": vec,
                }
                if meeting_dt:
                    doc["meeting_date"] = meeting_dt.isoformat() + "Z"
                    doc["year"] = meeting_dt.year
                    doc["month"] = meeting_dt.month
                yield doc


def upload(s: dict, docs: Iterable[dict]) -> int:
    search = make_search_client(s)
    BATCH = 100
    buf: list[dict] = []
    total = 0

    def upload_batch(batch: list[dict]) -> None:
        expected = {doc["id"] for doc in batch}
        if len(expected) != len(batch):
            raise ValueError("Duplicate document IDs in upload batch")
        results = search.upload_documents(documents=batch)
        failures = [f"{result.key}: {result.error_message}" for result in results if not result.succeeded]
        if failures or len(results) != len(batch) or {result.key for result in results} != expected:
            raise RuntimeError(f"Search upload failed or returned incomplete results: {failures}")

    for d in docs:
        buf.append(d)
        if len(buf) >= BATCH:
            upload_batch(buf)
            total += len(buf)
            log.info("  uploaded %d (running total %d)", len(buf), total)
            buf.clear()
    if buf:
        upload_batch(buf)
        total += len(buf)
        log.info("  uploaded %d (running total %d)", len(buf), total)
    return total


def verify_section_documents(s: dict, *, timeout_s: float = 120) -> None:
    expected = {doc["id"]: doc for doc in s["section_documents"]}
    deadline = time.monotonic() + timeout_s
    with make_search_client(s) as search:
        while True:
            fields = list(next(iter(expected.values())))
            rows = list(search.search(search_text="*", select=fields))
            for row in rows:
                if row["id"] not in expected:
                    raise ValueError("Versioned index contains extra documents; choose a new index version")
                for key, value in expected[row["id"]].items():
                    actual = row.get(key)
                    if key == "meeting_date":
                        actual = datetime.fromisoformat(str(actual).replace("Z", "+00:00")).astimezone(timezone.utc)
                        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if actual != value:
                        raise ValueError(f"Versioned index differs at {row['id']} field {key}; choose a new index version")
            if len(rows) == len(expected):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Index readback incomplete: {len(rows)}/{len(expected)} documents. No existing documents were overwritten.")
            log.info("Waiting for index visibility: %d/%d", len(rows), len(expected))
            time.sleep(2)


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--manifest", type=Path, help="Optional ingestion receipt; contains hashes, not source text.")
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("Explicit --env-file does not exist")
        load_dotenv(args.env_file, override=True)
    s = load_settings()
    if s["chunking_mode"] == "section":
        s["section_documents"] = prepare_section_documents(s)
    log.info("Search:    %s  /  index=%s", s["search_endpoint"], s["index_name"])
    log.info("Foundry:   %s  /  embed=%s", s["project_endpoint"], s["embed_deployment"])
    log.info("Data dir:  %s", s["data_dir"])

    aoai = make_embeddings_client(s)
    # First data-plane call of the deploy, so this is where a just-created role
    # assignment shows up as 401 while it propagates. See scripts/rbac_propagation.py.
    s["embed_dim"] = wait_for_data_plane(
        lambda: detect_embed_dim(aoai, s["embed_deployment"]),
        what="probing the embedding deployment",
        log=log.info,
    )
    log.info("Embedding dim (detected): %d", s["embed_dim"])

    # BYO Search roles may have been assigned moments ago by postprovision.
    # Foundry and Search propagate independently, so the successful embedding
    # probe above does not imply that index management is authorized yet.
    wait_for_data_plane(
        lambda: ensure_index(s),
        what=f"creating/updating Search index '{s['index_name']}'",
        log=log.info,
    )
    # Index management and document access use different Search data roles.
    # Probe the document plane separately before doing any potentially expensive
    # embedding work, rather than discovering propagation on the first upload.
    search = make_search_client(s)
    wait_for_data_plane(
        search.get_document_count,
        what=f"accessing documents in Search index '{s['index_name']}'",
        log=log.info,
    )
    if s.get("section_index_exists"):
        verify_section_documents(s, timeout_s=0)
        n = len(s["section_documents"])
        log.info("Existing version matches exactly; no document embeddings or uploads were repeated.")
    else:
        n = upload(s, iter_documents(s, aoai))
        if s["chunking_mode"] == "section":
            verify_section_documents(s)
    if args.manifest:
        receipt = {
            "index": s["index_name"], "chunking_mode": s["chunking_mode"],
            "document_scope": s["document_scope"], "documents": n,
            "semantic_config": s["semantic_config"],
            "embedding_deployment": s["embed_deployment"], "embedding_dimensions": s["embed_dim"],
        }
        if s["chunking_mode"] == "section":
            receipt["corpus_sha256"] = hashlib.sha256(
                json.dumps(s["section_documents"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            receipt["source_hashes"] = {doc["source"]: doc["source_sha256"] for doc in s["section_documents"]}
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    log.info("Done. Indexed %d chunks into '%s'.", n, s["index_name"])


if __name__ == "__main__":
    main()
