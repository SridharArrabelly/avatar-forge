"""What the counters say when records are lost.

On 10 August 2026 the shutdown line read::

    [AUDIT] writer stopped — submitted=10 written=5 dropped=0 failed=0

Five records had been rejected by the Cosmos firewall. Each rejection was logged
individually. Not one of them reached a counter, because ``CosmosSink`` catches
per-document exceptions and *returns a count* rather than raising, and the
writer only incremented ``failed`` when ``write()`` raised.

For a compliance trail that is the wrong way round: the summary an operator
reads on shutdown said everything was fine. These arms pin the accounting so
that any record which is accepted and then lost is counted and surfaced.

Run: uv run --no-sync python tests/test_audit_loss_accounting.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["ENABLE_AUDIT"] = "true"
os.environ["AUDIT_SINK"] = "none"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


class PartialSink:
    """Stores some documents and silently discards the rest.

    This is CosmosSink's real contract: it catches each document's exception,
    logs it, and returns how many it managed to store. It never raises, so a
    writer that only counts exceptions sees nothing at all.
    """

    def __init__(self, accept: int):
        self._accept = accept
        self.documents: list[dict] = []

    async def write(self, documents: list[dict]) -> int:
        taken = documents[: self._accept]
        self.documents.extend(taken)
        self._accept -= len(taken)
        return len(taken)

    async def close(self) -> None:
        pass


class RaisingSink:
    """Fails the whole batch by raising, the way a broken client would."""

    async def write(self, documents: list[dict]) -> int:
        raise RuntimeError("sink is down")

    async def close(self) -> None:
        pass


def _record(audit, index: int):
    from backend.audit.records import TurnRecord

    return TurnRecord(
        session_id="sess_loss_test",
        turn_index=index,
        channel="web",
        binding="model",
    )


async def arm_partial_write() -> None:
    """The production shape: some documents stored, the rest quietly dropped."""
    import backend.audit as audit
    from backend.audit.queue import AuditQueue

    print("\na sink that loses documents without raising")
    print("-" * 70)

    sink = PartialSink(accept=5)
    queue = AuditQueue(sink, max_size=100, retention_days=1, redact=True)
    queue.start()

    for i in range(10):
        queue.submit(_record(audit, i))

    await queue.drain()
    s = queue.stats()

    check("all ten were submitted", s["submitted"] == 10, f"got {s['submitted']}")
    check("five were written", s["written"] == 5, f"got {s['written']}")
    check(
        "the five that were not written are counted as failed",
        s["failed"] == 5,
        f"got {s['failed']} — this read 0 in production",
    )
    check(
        "submitted is fully accounted for",
        s["submitted"] == s["written"] + s["failed"] + s["queued"],
        f"{s['submitted']} != {s['written']} + {s['failed']} + {s['queued']}",
    )
    check("loss is surfaced as a single flag", s["lossy"] is True, f"got {s['lossy']!r}")


async def arm_raising_sink() -> None:
    """A sink that raises loses the whole batch, and must count all of it."""
    import backend.audit as audit
    from backend.audit.queue import AuditQueue

    print("\na sink that raises loses the whole batch")
    print("-" * 70)

    queue = AuditQueue(RaisingSink(), max_size=100, retention_days=1, redact=True)
    queue.start()
    for i in range(3):
        queue.submit(_record(audit, i))
    await queue.drain(timeout=3.0)
    s = queue.stats()

    check("nothing was written", s["written"] == 0, f"got {s['written']}")
    check(
        "every record in the batch is counted, not just the batch",
        s["failed"] >= 3,
        f"got {s['failed']} — a single += 1 would report 1",
    )
    check("loss is surfaced", s["lossy"] is True, f"got {s['lossy']!r}")


async def arm_unrenderable() -> None:
    """A record that cannot be serialised never reaches the sink."""
    import backend.audit as audit
    from backend.audit.queue import AuditQueue

    print("\na record that cannot be rendered is still counted")
    print("-" * 70)

    class Unrenderable:
        tools_pending = False
        conversation_id = None

        def to_document(self, **kwargs):
            raise ValueError("cannot serialise this")

    sink = PartialSink(accept=100)
    queue = AuditQueue(sink, max_size=100, retention_days=1, redact=True)
    queue.start()

    queue.submit(_record(audit, 0))
    queue.submit(Unrenderable())
    queue.submit(_record(audit, 1))

    await queue.drain()
    s = queue.stats()

    check("the two good records were written", s["written"] == 2, f"got {s['written']}")
    check(
        "the unrenderable record is counted as failed",
        s["failed"] == 1,
        f"got {s['failed']} — render failures used to vanish silently",
    )
    check("loss is surfaced", s["lossy"] is True, f"got {s['lossy']!r}")


async def arm_clean_run() -> None:
    """A healthy run must NOT report loss — the flag has to mean something."""
    import backend.audit as audit
    from backend.audit.queue import AuditQueue

    print("\na healthy run reports no loss")
    print("-" * 70)

    sink = PartialSink(accept=100)
    queue = AuditQueue(sink, max_size=100, retention_days=1, redact=True)
    queue.start()
    for i in range(4):
        queue.submit(_record(audit, i))
    await queue.drain()
    s = queue.stats()

    check("everything was written", s["written"] == 4, f"got {s['written']}")
    check("nothing is counted as failed", s["failed"] == 0, f"got {s['failed']}")
    check("lossy is false on a clean run", s["lossy"] is False, f"got {s['lossy']!r}")


async def arm_health_surface() -> None:
    """`/health` must expose loss, not just a degraded sink."""
    import backend.audit as audit
    from backend.audit.queue import AuditQueue

    print("\nloss is visible without reading logs")
    print("-" * 70)

    sink = PartialSink(accept=0)
    queue = AuditQueue(sink, max_size=100, retention_days=1, redact=True)
    queue.start()
    audit._queue = queue
    audit._sink_name = "PartialSink"

    queue.submit(_record(audit, 0))
    await queue.drain()

    state = audit.stats()
    check(
        "audit.stats() carries the loss flag",
        state.get("lossy") is True,
        f"got {state.get('lossy')!r}",
    )
    check(
        "the sink is not reported as degraded — it built fine",
        not state.get("degraded"),
        "loss and a fallback sink are different failures and must not be conflated",
    )

    from backend.api.routes import health_check

    body = await health_check()
    check(
        "/health reports the loss",
        (body.get("audit") or {}).get("lossy") is True,
        f"got {body.get('audit')!r}",
    )

    await audit.shutdown_audit()


async def main() -> int:
    await arm_partial_write()
    await arm_raising_sink()
    await arm_unrenderable()
    await arm_clean_run()
    await arm_health_surface()

    print("\n" + "-" * 70)
    if failures:
        print(f"{len(failures)} FAILED: " + "; ".join(failures))
        return 1
    print("All loss-accounting assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
