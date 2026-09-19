"""Versioned, source-preserving section layout for structured meeting minutes."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable


SECTION_HEADINGS = ("Agenda:", "Key Discussion Points:", "Decisions Made:", "Action Items:", "Next Steps:")
SECTION_FIELDS = frozenset({"section", "source_sha256", "source_paragraph"})


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def catalogue_title(title: str) -> str:
    """Hide the section layout's header marker in the meeting catalogue."""
    return title.removesuffix(" | Header")


def section_document(source: str, sha256: str, meeting_date: datetime, paragraphs: Iterable[str]) -> dict:
    """Prepare production parser output without depending on evaluation labels."""
    texts = [normalize_text(text) for text in paragraphs if normalize_text(text)]
    if not texts or texts[0] != normalize_text(Path(source).stem):
        raise ValueError(f"{source}: section mode requires a matching document-title paragraph")
    missing = set(SECTION_HEADINGS) - set(texts)
    if missing:
        raise ValueError(f"{source}: missing structured minutes headings: {', '.join(sorted(missing))}")
    current = "Header"
    blocks = []
    for ordinal, text in enumerate(texts):
        if text in SECTION_HEADINGS:
            current = text
        blocks.append({"ordinal": ordinal, "section": current, "text": text})
    return {
        "source": source, "sha256": sha256,
        "meeting_date": meeting_date.strftime("%Y-%m-%d"),
        "date_label": f"{meeting_date.day} {meeting_date.strftime('%B %Y')}",
        "paragraphs": blocks,
    }


def build_evidence_chunks(documents: list[dict], layout: str = "section") -> list[dict]:
    """Shared by normal ingestion and evaluation; IDs/layout remain versioned."""
    if layout not in ("paragraph", "section"):
        raise ValueError(f"Unknown evidence layout: {layout}")
    chunks = []
    for doc in documents:
        groups = {}
        for paragraph in doc["paragraphs"]:
            if paragraph["text"] in SECTION_HEADINGS or paragraph["ordinal"] == 0:
                continue
            key = paragraph["section"] if layout == "section" else str(paragraph["ordinal"])
            groups.setdefault(key, []).append(paragraph)
        for ordinal, (key, paragraphs) in enumerate(groups.items()):
            section = paragraphs[0]["section"].rstrip(":")
            title = Path(doc["source"]).stem
            text = "\n".join(paragraph["text"] for paragraph in paragraphs)
            if len(text) > 12000:
                raise ValueError(f"{doc['source']}: evidence block exceeds the versioned layout's 12,000-character limit")
            namespace = "section-v1" if layout == "paragraph" else "section-block-v1"
            chunks.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{doc['source']}:{key}")),
                "title": f"{title} | {section}", "source": doc["source"],
                "documentType": "MeetingMinutes", "meeting_date": f"{doc['meeting_date']}T00:00:00Z",
                "year": int(doc["meeting_date"][:4]), "month": int(doc["meeting_date"][5:7]),
                "chunk_index": ordinal, "section": section, "source_sha256": doc["sha256"],
                "source_paragraph": paragraphs[0]["ordinal"],
                "content": f"[Document: {title} | Type: MeetingMinutes | Meeting Date: {doc['date_label']} | Section: {section}]\n\n{text}",
            })
    return chunks
