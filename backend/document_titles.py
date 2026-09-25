"""Human-readable titles for documents whose source names are filenames."""

from __future__ import annotations


def display_document_title(raw: str, document_type: str) -> str:
    """Return the stored document title with caller-compatible arguments."""
    return (raw or "").strip()
