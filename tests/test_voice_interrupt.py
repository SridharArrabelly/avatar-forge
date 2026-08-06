"""Regression tests for manual Voice Live response interruption."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.models import ServerEventType

from backend.voice.event_handlers import handle_event
from backend.voice.handler import VoiceSessionHandler


class AsyncEventConnection:
    def __init__(self, *events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as error:
            raise StopAsyncIteration from error


class VoiceInterruptTests(unittest.IsolatedAsyncioTestCase):
    def make_handler(self):
        send_message = AsyncMock()
        handler = VoiceSessionHandler(
            client_id="test",
            endpoint="https://example.invalid",
            credential=Mock(),
            send_message=send_message,
            config={"avatarEnabled": True},
        )
        handler.connection = SimpleNamespace(
            response=SimpleNamespace(cancel=AsyncMock()),
            output_audio_buffer=SimpleNamespace(clear=AsyncMock()),
        )
        return handler

    async def test_interrupt_cancels_an_active_response_only_once(self):
        handler = self.make_handler()
        handler._response_active = True

        await asyncio.gather(handler.interrupt(), handler.interrupt())

        handler.connection.response.cancel.assert_awaited_once_with()
        self.assertFalse(handler._response_active)

    async def test_interrupt_skips_cancel_but_clears_buffer_after_response_done(self):
        handler = self.make_handler()

        await handler.interrupt()

        handler.connection.response.cancel.assert_not_awaited()
        handler.connection.output_audio_buffer.clear.assert_awaited_once_with()

    async def test_response_events_update_active_state(self):
        handler = self.make_handler()
        created = SimpleNamespace(
            type=ServerEventType.RESPONSE_CREATED,
            response=SimpleNamespace(id="response-1"),
        )
        done = SimpleNamespace(
            type=ServerEventType.RESPONSE_DONE,
            response=SimpleNamespace(
                id="response-1",
                status="completed",
                status_details=None,
                output=[SimpleNamespace(type="message")],
            ),
        )

        await handle_event(handler, created, handler.connection)
        self.assertTrue(handler._response_active)
        await handle_event(handler, done, handler.connection)
        self.assertFalse(handler._response_active)

    async def test_inactive_cancel_error_is_not_surfaced(self):
        handler = self.make_handler()
        event = SimpleNamespace(
            type=ServerEventType.ERROR,
            error={"code": "response_cancel_not_active"},
        )

        await handle_event(handler, event, handler.connection)

        handler.send_message.assert_not_awaited()

    async def test_direct_wait_for_response_done_clears_active_state(self):
        handler = self.make_handler()
        handler._response_active = True
        done = SimpleNamespace(type=ServerEventType.RESPONSE_DONE)

        result = await handler._wait_for_event(
            AsyncEventConnection(done),
            {ServerEventType.RESPONSE_DONE},
        )

        self.assertIs(result, done)
        self.assertFalse(handler._response_active)


if __name__ == "__main__":
    unittest.main()