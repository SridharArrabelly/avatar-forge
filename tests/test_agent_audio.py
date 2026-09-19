"""Offline checks for explicitly scoped received-audio timing; no voice service calls."""
import asyncio
import base64
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_agent_audio as audio


class Connection:
    def __init__(self, events):
        self.events = iter(events)
        self.session = SimpleNamespace(update=AsyncMock())
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=AsyncMock()))
        self.response = SimpleNamespace(create=AsyncMock())

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration


async def exercise(events):
    connection = Connection(events)

    @asynccontextmanager
    async def connect(**kwargs):
        assert kwargs["agent_name"] == "isolated-agent"
        assert kwargs["agent_version"] == "1"
        yield connection

    with patch.object(audio, "connect", connect):
        result = await audio.audio_turn(
            "https://example.invalid", "project", "isolated-agent", "1",
            "catalogue", "test question", object(), "en-US-AvaMultilingualNeural", "2026-04-10",
        )
    return result, connection


def event(kind, **kwargs):
    return SimpleNamespace(type=kind, **kwargs)


def done(status="completed"):
    return event("response.done", response=SimpleNamespace(as_dict=lambda: {"status": status}))


def main():
    events = [
        event("session.updated"),
        event("response.audio.delta", delta=base64.b64encode(b"\x00\x00\x01\x00").decode()),
        event("response.audio_transcript.delta", delta="Source-based answer."),
        done(),
    ]
    result, connection = asyncio.run(exercise(events))
    assert result["status"] == "completed"
    assert result["audio_bytes"] == 4
    assert result["text_submit_to_first_received_audio_s"] >= 0
    assert result["transcript"] == "Source-based answer."
    assert result["playback_latency_measured"] is False
    assert result["speech_input_used"] is False
    assert connection.conversation.item.create.await_count == 2
    assert connection.response.create.await_count == 1
    events[1] = event("response.audio.delta", delta=b"\x00\x00\x01\x00")
    binary_result, _ = asyncio.run(exercise(events))
    assert binary_result["audio_bytes"] == 4
    print("PASS measures received audio without claiming microphone or playback latency")

    for bad in (
        [event("session.updated"), done()],
        [event("session.updated"), done("failed")],
        [event("session.updated"), event("error", as_dict=lambda: {"error": "test"})],
        [event("session.updated")],
    ):
        try:
            asyncio.run(exercise(bad))
        except RuntimeError:
            pass
        else:
            raise AssertionError("No-audio, failed or truncated response passed")
    print("PASS missing audio and failed/incomplete streams are explicit failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
