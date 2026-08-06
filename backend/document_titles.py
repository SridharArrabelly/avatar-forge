"""Human-readable titles for documents whose source names are filenames."""

from __future__ import annotations

import re

_POLICY_CODE = re.compile(r"^[A-Z]\d{3,}\s+", re.IGNORECASE)
_POLICY_STATUS_SUFFIX = re.compile(
    r"(?:\s+(?:final|signed|approved|executed))+\s*$", re.IGNORECASE
)
_POLICY_VERSION_DATE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{4}\b",
    re.IGNORECASE,
)


def display_document_title(raw: str, document_type: str) -> str:
    """Remove filename/version noise from policy titles.

    Meeting titles are already curated and may contain meaningful dashes or
    dates, so only Policy titles are normalized.
    """
    title = (raw or "").strip()
    if document_type.lower() != "policy":
        return title

    title = re.sub(r"[_-]+", " ", title)
    title = _POLICY_CODE.sub("", title)
    title = _POLICY_STATUS_SUFFIX.sub("", title)
    title = _POLICY_VERSION_DATE.sub("", title)
    return re.sub(r"\s+", " ", title).strip(" ._-")
