"""Live captions and hosted backend progress are independent streams."""
import asyncio
import unittest
from unittest.mock import patch

from app.services import openai_realtime as rt
from tests.test_live_session import LiveHarness


class ConcurrentStreamsTests(LiveHarness):
    async def test_slow_capture_does_not_block_captions_or_new_question(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(*args, **kwargs):
            entered.set()
            await release.wait()
            from tests.test_realtime import PNG_DATA_URL
            return "slow-screen", PNG_DATA_URL
        await self.delegate()
        with patch.object(rt, "_request_current_screen", new=capture):
            call = asyncio.create_task(self.call("capture_current_screen"))
            await entered.wait()
            await self.caption("I can explain the initial approach while checking the details.")
            old_caption = self.live.caption_id
            self.assertTrue(self.runtime.response_buffers[old_caption])
            await self.live.event({"type": "session.input_transcript.delta", "delta": "Actually, use SQL.", "end_ms": 2000})
            self.assertEqual(self.runtime.response_status[old_caption], "interrupted")
            release.set()
            result = await call
        self.assertFalse(result["ok"])
        self.assertFalse(any(e["kind"] == "screen" for e in self.runtime.history.entries))

    async def test_backend_completion_does_not_finalize_or_replace_visible_caption(self):
        await self.delegate()
        await self.caption("First part. ")
        caption_id = self.live.caption_id
        await self.backend("response.completed", response={"id": "r", "output": []})
        self.assertEqual(self.runtime.response_status[caption_id], "streaming")
        await self.caption("Second part.", 200)
        await self.live.finish_caption()
        self.assertEqual(self.runtime.response_buffers[caption_id], "First part. Second part.")
        self.assertEqual(self.runtime.response_status[caption_id], "completed")

    async def test_tool_result_after_terminal_continues_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(*args, **kwargs):
            entered.set()
            await release.wait()
            from tests.test_realtime import PNG_DATA_URL
            return "screen", PNG_DATA_URL
        await self.delegate()
        with patch.object(rt, "_request_current_screen", new=capture):
            call = asyncio.create_task(self.call("capture_current_screen"))
            await entered.wait()
            await self.backend("response.completed", response={"id": "r", "output": []})
            self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))
            release.set()
            self.assertTrue((await call)["ok"])
        self.assertEqual(sum(m["type"] == "response.create" for m in self.upstream.messages), 1)
