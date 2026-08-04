"""Quick smoke test for the Azure AI Search index.

Runs a hybrid (BM25 + vector) query with the semantic configuration enabled,
prints the top K results including date metadata fields.

Supports optional OData date filtering via --year and --month flags.

Also supports --list-meetings to print all unique meetings in the index
(faceted on meeting_date / year / month) — handy for sanity-checking what
was actually ingested.

Usage:
    uv run python scripts/smoke_aisearch_query.py "what was discussed about dividends"
    uv run python scripts/smoke_aisearch_query.py "board chair election" -k 3
    uv run python scripts/smoke_aisearch_query.py "meeting summary" --year 2025 --month 2
    uv run python scripts/smoke_aisearch_query.py --list-meetings

Env: same as scripts/setup_aisearch_index.py (AZURE_SEARCH_ENDPOINT,
SEARCH_INDEX_NAME, PROJECT_ENDPOINT, EMBEDDING_DEPLOYMENT, optional
AZURE_SEARCH_API_KEY, AZURE_OPENAI_API_VERSION).
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlparse

from openai import AzureOpenAI
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery, QueryType
from dotenv import load_dotenv

load_dotenv()

SEMANTIC_CONFIG = os.getenv("SEARCH_SEMANTIC_CONFIG", "default-semantic")


def _require(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        print(f"Missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def _aad() -> DefaultAzureCredential:
    return DefaultAzureCredential()


def make_search_client() -> SearchClient:
    endpoint = _require("AZURE_SEARCH_ENDPOINT").rstrip("/")
    index = _require("SEARCH_INDEX_NAME")
    key = os.getenv("AZURE_SEARCH_API_KEY", "").strip()
    cred = AzureKeyCredential(key) if key else _aad()
    return SearchClient(endpoint=endpoint, index_name=index, credential=cred)


def make_embeddings_client() -> AzureOpenAI:
    project_endpoint = _require("PROJECT_ENDPOINT")
    parsed = urlparse(project_endpoint)
    azure_endpoint = f"{parsed.scheme}://{parsed.netloc}"
    token_provider = get_bearer_token_provider(_aad(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(
        azure_endpoint=azure_endpoint,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_ad_token_provider=token_provider,
    )


def embed(query: str) -> list[float]:
    client = make_embeddings_client()
    deployment = os.getenv("EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    resp = client.embeddings.create(model=deployment, input=[query])
    return resp.data[0].embedding


def list_meetings() -> None:
    """Facet on meeting_date / year / month and print what's actually in the index."""
    search = make_search_client()
    results = search.search(
        search_text="*",
        facets=[
            "meeting_date,count:1000,sort:value",
            "year,count:50,sort:value",
            "month,count:12,sort:value",
        ],
        top=0,
        include_total_count=True,
    )
    total = results.get_count()
    facets = results.get_facets() or {}
    dates = facets.get("meeting_date", []) or []
    years = facets.get("year", []) or []
    months = facets.get("month", []) or []

    print(f"\nIndex contents — total chunks: {total}")
    print("-" * 60)
    print(f"\nMeetings by date ({len(dates)} unique):")
    for f in dates:
        v = f.get("value")
        c = f.get("count")
        # meeting_date is DateTimeOffset; show date portion only
        date_str = v.split("T")[0] if isinstance(v, str) and "T" in v else v
        print(f"  {date_str}  ({c} chunks)")
    print(f"\nBy year: {[(f['value'], f['count']) for f in years]}")
    print(f"By month: {[(f['value'], f['count']) for f in months]}")
    print()


def run(query: str, k: int, year: int | None = None, month: int | None = None) -> None:
    vec = embed(query)
    search = make_search_client()

    # Build OData filter for date fields if provided
    filters: list[str] = []
    if year is not None:
        filters.append(f"year eq {year}")
    if month is not None:
        filters.append(f"month eq {month}")
    filter_expr = " and ".join(filters) if filters else None

    results = search.search(
        search_text=query,
        vector_queries=[VectorizedQuery(vector=vec, k_nearest_neighbors=k, fields="content_vector")],
        query_type=QueryType.SEMANTIC,
        semantic_configuration_name=SEMANTIC_CONFIG,
        filter=filter_expr,
        select=["id", "title", "source", "chunk_index", "content", "meeting_date", "year", "month"],
        top=k,
    )

    filter_info = f"  filter: {filter_expr}" if filter_expr else ""
    print(f"\nQuery: {query!r}   (top {k}, hybrid + semantic){filter_info}\n" + "-" * 72)
    for i, r in enumerate(results, 1):
        score = r.get("@search.score")
        rerank = r.get("@search.reranker_score")
        snippet = (r.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240] + "..."
        meeting_date = r.get("meeting_date") or "N/A"
        print(f"\n[{i}] {r.get('title')}  (chunk {r.get('chunk_index')})")
        print(f"    source : {r.get('source')}")
        print(f"    date   : {meeting_date}  (year={r.get('year')}, month={r.get('month')})")
        print(f"    score  : {score:.4f}" + (f"   rerank: {rerank:.4f}" if rerank is not None else ""))
        print(f"    text   : {snippet}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Hybrid + semantic search smoke test")
    ap.add_argument("query", nargs="*", help="Query text (omit when using --list-meetings)")
    ap.add_argument("-k", type=int, default=5, help="Top K results (default 5)")
    ap.add_argument("--year", type=int, default=None, help="Filter by meeting year (e.g. 2025)")
    ap.add_argument("--month", type=int, default=None, help="Filter by meeting month (1-12)")
    ap.add_argument(
        "--list-meetings",
        action="store_true",
        help="Print all unique meetings in the index (facets on meeting_date) and exit.",
    )
    args = ap.parse_args()
    if args.list_meetings:
        list_meetings()
        return
    if not args.query:
        ap.error("query is required unless --list-meetings is given")
    run(" ".join(args.query), args.k, year=args.year, month=args.month)


if __name__ == "__main__":
    main()
