"""Where audit documents go.

A deliberately narrow interface: one method to write a batch, one to close. It
exists so the storage backend stays swappable, and so the latency-critical
machinery in :mod:`backend.audit.queue` never has to know what Cosmos is.

Every sink must treat failure as *its* problem to report, never to raise into
the writer loop — a broken sink must degrade to counted drops, never to a
stalled conversation.
"""

import asyncio
import json
import logging
import os
from typing import Protocol

logger = logging.getLogger(__name__)


class AuditSink(Protocol):
    """Storage backend for audit documents."""

    async def write(self, documents: list[dict]) -> int:
        """Persist a batch. Returns the number successfully written."""
        ...

    async def close(self) -> None:
        ...


class NullSink:
    """Accept and discard.

    Not a no-op feature switch — that is ``ENABLE_AUDIT=false``, which skips the
    capture code entirely. This exists to isolate the *sink* during latency A/B
    testing: it exercises capture, queue and writer at full cost while removing
    storage from the picture.
    """

    async def write(self, documents: list[dict]) -> int:
        return len(documents)

    async def close(self) -> None:
        return None


class FileSink:
    """Newline-delimited JSON on local disk, for development.

    Appends so a restart never truncates an existing trail. The write itself is
    pushed to a thread because this runs in the writer task, which shares the
    event loop with audio delivery — a synchronous disk write on a slow volume
    would be felt by the user.
    """

    def __init__(self, path: str = "audit-log.jsonl"):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    def _append(self, documents: list[dict]) -> int:
        with open(self.path, "a", encoding="utf-8") as fh:
            for doc in documents:
                fh.write(json.dumps(doc, ensure_ascii=False, default=str) + "\n")
        return len(documents)

    async def write(self, documents: list[dict]) -> int:
        if not documents:
            return 0
        return await asyncio.to_thread(self._append, documents)

    async def close(self) -> None:
        return None
