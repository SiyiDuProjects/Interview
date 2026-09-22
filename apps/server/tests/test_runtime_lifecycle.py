from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import openai_realtime as rt
from app.services.context_store import ContextStore
from tests.test_realtime_recovery import Upstream


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_rejects_new_analysis_while_old_jobs_finish_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = rt.InterviewRuntime(
                interview_id="lifecycle-test",
                session_token="synthetic-session",
                capture_token="synthetic-capture",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                context_store=ContextStore(Path(directory)),
            )
            runtime.active = True
            runtime.main_upstream = Upstream()
            runtime.broadcast_to_clients = AsyncMock()
            await runtime.emit_transcript_final("interviewer", "A question", turn_id="q1")

            started = asyncio.Event()
            cancellation_started = asyncio.Event()
            finish_cancellation = asyncio.Event()

            async def older_job() -> None:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancellation_started.set()
                    await finish_cancellation.wait()

            previous = asyncio.create_task(older_job())
            runtime._jobs["previous"] = previous
            await started.wait()
            closing = asyncio.create_task(runtime.close())
            await asyncio.wait_for(cancellation_started.wait(), timeout=1)
            analysis = AsyncMock(side_effect=lambda *_: "Unexpected analysis")
            try:
                with patch.object(rt, "_analyze_problem", analysis):
                    # A second authenticated UI may still send a queued action
                    # while a cancelled network task is running its cleanup.
                    await runtime.start_operation(
                        {"type": "quick_answer", "action": "deep", "operation_id": "late"},
                        object(),
                    )
                    await asyncio.sleep(0)
                    finish_cancellation.set()
                    await asyncio.wait_for(closing, timeout=1)
                    await asyncio.sleep(0)
                self.assertTrue(runtime.closed)
                self.assertEqual(analysis.await_count, 0)
                self.assertFalse(any(not task.done() for task in runtime._jobs.values()))
            finally:
                finish_cancellation.set()
                pending = [task for task in runtime._jobs.values() if not task.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(closing, *pending, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
