"""Offline original-document references and span-based retrieval scoring.

Reference text belongs in private artifacts, never in committed fixtures.
This module neither calls Azure nor reads policy subdirectories.
"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.document_sections import SECTION_HEADINGS as SECTIONS
from backend.document_sections import build_evidence_chunks as layout_chunks

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DEVELOPMENT_DATES = {"2006-03-15", "2013-06-30", "2018-07-05", "2023-09-15", "2026-02-15"}
INTENTS = {
    "attendees": "attendees attendance list",
    "decisions": "decisions approved agreed",
    "actions": "action items owners next actions",
    "discussion": "key discussion points",
}
INTENT_SECTION = {
    "decisions": "Decisions Made:",
    "actions": "Action Items:",
    "discussion": "Key Discussion Points:",
}
NATURAL_QUERIES = {
    "attendees": "Who was present at the board meeting on {date}?",
    "decisions": "What did the board decide at its {date} meeting?",
    "actions": "Which follow-up tasks were assigned and who owns them from the {date} board meeting?",
    "discussion": "Summarise the main issues debated at the board meeting on {date}.",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def query_for(case: dict, style: str) -> str:
    if style == "canonical":
        return case["query"]
    if style != "natural":
        raise ValueError(f"Unknown query style: {style}")
    date = datetime.fromisoformat(case["meeting_date"])
    return NATURAL_QUERIES[case["intent"]].format(date=f"{date.day} {date.strftime('%B %Y')}")


def read_original(path: Path) -> dict:
    """Read document.xml independently of the production python-docx reader."""
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    for tag in ("ins", "del", "footnoteReference", "endnoteReference", "drawing", "pict"):
        if root.findall(f".//{W}{tag}"):
            raise ValueError(f"{path.name}: {tag} needs a source-review policy before evaluation")
    body = root.find(f"{W}body")
    if body is None:
        raise ValueError(f"{path.name}: missing document body")
    date_match = re.search(r"(\d{1,2} [A-Za-z]+ \d{4})", path.stem)
    if not date_match:
        raise ValueError(f"{path.name}: no meeting date")
    date = datetime.strptime(date_match[1], "%d %B %Y")
    paragraphs = []
    section = "Header"
    full_text = ""
    for element in body.iter(f"{W}p"):
        parts = []
        for item in element.iter():
            if item.tag == f"{W}t":
                parts.append(item.text or "")
            elif item.tag in (f"{W}tab", f"{W}br", f"{W}cr"):
                parts.append(" ")
        text = normalize("".join(parts))
        if not text:
            continue
        if text in SECTIONS:
            section = text
        start = len(full_text) + (1 if full_text else 0)
        full_text += (" " if full_text else "") + text
        paragraphs.append({
            "ordinal": len(paragraphs), "section": section,
            "text": text, "start": start, "end": len(full_text),
        })
    if not paragraphs:
        raise ValueError(f"{path.name}: no text")
    return {
        "source": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "meeting_date": date.strftime("%Y-%m-%d"),
        "date_label": f"{date.day} {date.strftime('%B %Y')}",
        "paragraphs": paragraphs, "text": full_text,
        "tables": len(body.findall(f".//{W}tbl")),
    }


def build_cases(documents: list[dict]) -> list[dict]:
    cases = []
    for document in sorted(documents, key=lambda doc: doc["meeting_date"]):
        for intent, query_terms in INTENTS.items():
            evidence = []
            for paragraph in document["paragraphs"]:
                text = paragraph["text"]
                if intent == "attendees" and "Attendees:" in text:
                    offset = text.index("Attendees:") + len("Attendees:")
                    while offset < len(text) and text[offset].isspace():
                        offset += 1
                    evidence.append({
                        "paragraph": paragraph["ordinal"],
                        "start": paragraph["start"] + offset, "end": paragraph["end"],
                        "text": text[offset:],
                    })
                elif intent != "attendees" and paragraph["section"] == INTENT_SECTION[intent] and text not in SECTIONS:
                    evidence.append({
                        "paragraph": paragraph["ordinal"], "start": paragraph["start"],
                        "end": paragraph["end"], "text": text,
                    })
            if not evidence:
                raise ValueError(f"{document['source']}: no original evidence for {intent}")
            cases.append({
                "id": f"{document['meeting_date']}-{intent}",
                "source": document["source"], "source_sha256": document["sha256"],
                "meeting_date": document["meeting_date"], "intent": intent,
                "split": "development" if document["meeting_date"] in DEVELOPMENT_DATES else "held_out",
                "query": f"Board Meeting {document['date_label']} {query_terms}",
                "evidence": evidence,
            })
    return cases


def locate_chunk(content: str, document: dict) -> tuple[int, int]:
    """Locate actual chunk text in its original, excluding generated metadata."""
    prefix = re.match(r"^\[Document:.*?\]\s*", content, flags=re.DOTALL)
    if not prefix:
        raise ValueError(f"{document['source']}: chunk lacks expected metadata prefix")
    text = normalize(content[prefix.end():])
    if not text:
        raise ValueError(f"{document['source']}: empty chunk")
    start = document["text"].find(text)
    if start < 0:
        raise ValueError(f"{document['source']}: chunk text is not present verbatim in original")
    if document["text"].find(text, start + 1) >= 0:
        raise ValueError(f"{document['source']}: ambiguous repeated chunk")
    return start, start + len(text)


def native_chunk_map(hits: list[dict], audited: dict[str, dict], documents: dict[str, dict]) -> dict[str, dict]:
    """Score the text actually delivered by the tool, not its document IDs."""
    mapped = {}
    for hit in hits:
        source = audited[hit["id"]]["source"]
        content = hit["content"]
        marker = content.find("[Document:")
        if marker < 0:
            raise ValueError("Native tool passage has no auditable source metadata")
        start, end = locate_chunk(content[marker:], documents[source])
        mapped[hit["id"]] = {"source": source, "start": start, "end": end}
    return mapped


def covered_length(start: int, end: int, spans: list[tuple[int, int]]) -> int:
    cursor, total = start, 0
    for left, right in sorted(spans):
        left, right = max(start, left), min(end, right)
        if right <= max(cursor, left):
            continue
        total += right - max(cursor, left)
        cursor = max(cursor, right)
    return total


def score(case: dict, hits: list[dict], chunk_map: dict[str, dict]) -> dict:
    spans = []
    relevant = 0
    wrong_source = 0
    first_relevant = None
    for rank, hit in enumerate(hits, 1):
        chunk = chunk_map[hit["id"]]
        if chunk["source"] != case["source"]:
            wrong_source += 1
            continue
        interval = (chunk["start"], chunk["end"])
        spans.append(interval)
        if any(covered_length(e["start"], e["end"], [interval]) > 0 for e in case["evidence"]):
            relevant += 1
            first_relevant = first_relevant or rank
    evidence = [
        {
            "paragraph": unit["paragraph"],
            "covered_chars": covered_length(unit["start"], unit["end"], spans),
            "required_chars": unit["end"] - unit["start"],
        }
        for unit in case["evidence"]
    ]
    complete = sum(unit["covered_chars"] == unit["required_chars"] for unit in evidence)
    required = sum(unit["required_chars"] for unit in evidence)
    return {
        "complete_evidence_units": complete, "required_evidence_units": len(evidence),
        "complete_case": complete == len(evidence),
        "evidence_char_coverage": sum(unit["covered_chars"] for unit in evidence) / required,
        "wrong_source_hits": wrong_source, "retrieved_hits": len(hits),
        "evidence_hit_precision": relevant / len(hits) if hits else 0.0,
        "reciprocal_rank": 1 / first_relevant if first_relevant else 0.0,
        "evidence": evidence,
    }
