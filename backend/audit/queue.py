"""The bounded queue and background writer that keep audit off the hot path.

This module is where the latency guarantee lives, so the rules it enforces are
worth stating plainly:

**Capture never blocks.** ``submit()`` uses ``put_nowait`` and swallows
``QueueFull``. ``await queue.put()`` on a full queue would suspend the caller —
and the caller is the event loop that is streaming audio to the user. A dropped
audit record is a bad day; a stalled conversation is a broken product. The
tradeoff is decided here, once, permanently.

**Capture does no CPU work.** ``submit()`` enqueues the live ``TurnRecord``
object by reference. Redaction, truncation and JSON serialisation all happen in
the writer, offloaded to a worker thread with ``asyncio.to_thread``.

**Capture cannot raise.** Every public entry point is wrapped. An exception
escaping into ``handle_event`` would abandon the rest of that event's handling,
including audio relay.
"""

import asyncio
import logging
import time
from typing import Optional

from .records import TurnRecord
from .sinks import AuditSink

logger = logging.getLogger(__name__)

# How long the writer waits for a batch to fill before flushing what it has.
# Purely a throughput/efficiency knob — nothing user-facing waits on it.
_BATCH_WINDOW_S = 2.0
_BATCH_MAX = 50

# How often the idle writer wakes to notice a shutdown. Only affects how
# promptly drain() completes; nothing user-facing depends on it.
_POLL_S = 0.25

# Dropping is expected under a sink outage and could otherwise emit a log line
# per turn per session. Warn at most this often.
_DROP_WARN_INTERVAL_S = 30.0


class AuditQueue:
    """Bounded queue plus a single background writer task."""

    def __init__(
        self,
        sink: AuditSink,
        *,
        max_size: int = 1000,
        retention_days: int = 365,
        redact: bool = True,
        max_payload_bytes: int = 32 * 1024,
    ):
        self._queue: asyncio.Queue[TurnRecord] = asyncio.Queue(maxsize=max_size)
        self._sink = sink
        self._retention_days = retention_days
        self._redact = redact
        self._max_payload_bytes = max_payload_bytes
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._draining = False
        self._last_drop_warn = 0.0

        # Counters. `dropped` being non-zero is the signal that this deployment
        # needs a durable spool rather than best-effort delivery.
        self.submitted = 0
        self.dropped = 0
        self.written = 0
        self.failed = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="audit-writer")
        logger.info(
            f"[AUDIT] writer started (queue max={self._queue.maxsize}, "
            f"retention={self._retention_days}d, redact={self._redact})"
        )

    def submit(self, record: TurnRecord) -> bool:
        """Enqueue a finished turn. Returns False if it was dropped.

        The only audit function called from the event loop. It must stay O(1),
        allocation-light, and incapable of raising.
        """
        try:
            self._queue.put_nowait(record)
            self.submitted += 1
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_drop_warn > _DROP_WARN_INTERVAL_S:
                self._last_drop_warn = now
                logger.warning(
                    f"[AUDIT] queue full — dropping records to protect latency "
                    f"(dropped={self.dropped} total)"
                )
            return False
        except Exception as e:
            # Never let an audit bug break the turn that produced it.
            self.dropped += 1
            logger.debug(f"[AUDIT] submit failed: {e}")
            return False

    async def _collect_batch(self) -> list[TurnRecord]:
        """Wait for one record, then opportunistically drain up to a batch.

        Polls rather than blocking indefinitely so that :meth:`drain` can stop
        the writer promptly at shutdown. Returns an empty list to mean "nothing
        left and we are shutting down".
        """
        first = None
        while first is None:
            if self._draining and self._queue.empty():
                return []
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=_POLL_S)
            except asyncio.TimeoutError:
                continue

        batch = [first]
        # While draining, take only what is already queued: waiting out the
        # batch window would delay shutdown for no benefit.
        window = 0.0 if self._draining else _BATCH_WINDOW_S
        deadline = time.monotonic() + window
        while len(batch) < _BATCH_MAX:
            if self._draining:
                if self._queue.empty():
                    break
                batch.append(self._queue.get_nowait())
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # Wait in slices so a shutdown starting mid-window is noticed
                # promptly rather than after the full batch window.
                batch.append(
                    await asyncio.wait_for(
                        self._queue.get(), timeout=min(remaining, _POLL_S)
                    )
                )
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    break
                continue
        return batch

    def _render(self, batch: list[TurnRecord]) -> list[dict]:
        """Redact + serialise. Runs in a worker thread, not on the event loop."""
        documents = []
        for record in batch:
            try:
                documents.append(
                    record.to_document(
                        retention_days=self._retention_days,
                        do_redact=self._redact,
                        max_payload_bytes=self._max_payload_bytes,
                    )
                )
            except Exception as e:
                logger.warning(f"[AUDIT] could not render record: {e}")
        return documents

    async def _reconcile(self, batch: list[TurnRecord]) -> None:
        """Fill in agent-binding tool I/O before the batch is rendered.

        Agent binding relays no tool events at all, so this after-the-fact fetch
        from Foundry is the only way to record what was searched and what came
        back. It is safe to do here because conversations outlive the session —
        nothing user-facing is waiting on it, and each fetch is time-bounded so
        a slow Foundry cannot back the writer up.

        A failure leaves ``toolsPending`` true and the record is still written:
        incomplete, but explicitly and visibly so.
        """
        pending = [r for r in batch if r.tools_pending and r.conversation_id]
        if not pending:
            return
        try:
            from .foundry import reconcile
        except Exception as e:
            logger.debug(f"[AUDIT] reconciler unavailable: {e}")
            return
        for record in pending:
            try:
                await reconcile(record)
            except Exception as e:
                logger.debug(f"[AUDIT] reconcile failed: {e}")

    async def _run(self) -> None:
        while True:
            try:
                batch = await self._collect_batch()
                if not batch:
                    # Draining and nothing left — the writer is done.
                    return
                await self._reconcile(batch)
                documents = await asyncio.to_thread(self._render, batch)
                if documents:
                    written = await self._sink.write(documents)
                    self.written += written
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failed += 1
                # Back off briefly so a persistently failing sink does not spin.
                logger.warning(f"[AUDIT] write failed: {e}")
                await asyncio.sleep(1.0)

    async def drain(self, timeout: float = 5.0) -> None:
        """Flush what is queued, then stop. Called on shutdown.

        Waits for the writer to finish the batch it is holding, not merely for
        the queue to empty — records are dequeued *before* they are written, so
        checking the queue alone would cut the writer off mid-flight and lose
        them. Bounded by ``timeout`` so an unreachable sink cannot hang
        shutdown; anything still unwritten at that point is lost by design.
        """
        self._draining = True
        self._running = False

        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception as e:
                logger.debug(f"[AUDIT] writer stopped with error: {e}")
            self._task = None

        try:
            await self._sink.close()
        except Exception as e:
            logger.debug(f"[AUDIT] sink close failed: {e}")

        logger.info(
            f"[AUDIT] writer stopped — submitted={self.submitted} "
            f"written={self.written} dropped={self.dropped} failed={self.failed}"
        )

    def stats(self) -> dict:
        return {
            "submitted": self.submitted,
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
            "queued": self._queue.qsize(),
        }
