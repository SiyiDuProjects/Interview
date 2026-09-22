from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.context_store import ContextStore
from app.services import openai_realtime as rt


class Upstream:
    def __init__(self):
        self.messages = []
        self.events = asyncio.Queue()
        self.closed = False

    async def send(self, data):
        event = json.loads(data)
        self.messages.append(event)
        if event["type"] == "session.start":
            self.events.put_nowait({"type": "session.started"})
        elif event["type"] == "session.update":
            self.events.put_nowait({"type": "session.updated"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.events.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def close(self):
        self.closed = True
        self.events.put_nowait(None)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        Path(self.directory.name, "background.md").write_text("COMPLETE_BACKGROUND", encoding="utf-8")
        self.runtime = rt.InterviewRuntime(interview_id="test", session_token="session", capture_token="capture",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), context_store=ContextStore(Path(self.directory.name)))
        self.runtime.active = True
        self.upstream = Upstream()
        from tests.test_realtime import attach_live
        attach_live(self.runtime, self.upstream)
        self.client_events = []
        self.runtime.broadcast_to_clients = AsyncMock(side_effect=self.client_events.append)
        self.reader = asyncio.create_task(rt._forward_main_events(self.runtime, self.upstream))
        self.runtime._main_reader_task = self.reader

    async def asyncTearDown(self):
        await self.runtime.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        self.directory.cleanup()

    def event(self, **event):
        self.upstream.events.put_nowait(event)

    async def test_capture_disconnect_closes_only_its_upstream_and_keeps_history(self):
        from tests.test_realtime import FakeClientWebSocket
        candidate = Upstream()
        self.runtime.candidate_upstream = candidate
        await self.runtime.remember_dialogue("interviewer", "Keep this question")
        socket = FakeClientWebSocket({"type": "authenticate", "token": "capture"})
        await self.runtime.serve(socket, "candidate")
        self.assertTrue(candidate.closed)
        self.assertFalse(self.upstream.closed)
        self.assertTrue(self.runtime.active)
        self.assertEqual(self.runtime.question_text(), "Keep this question")
        self.assertIsNone(self.runtime.candidate_upstream)

    async def test_replacement_capture_prevents_old_disconnect_cleanup_from_closing_upstream(self):
        self.runtime._capture_clients["interviewer"] = object()
        await self.runtime._release_upstream("main", self.upstream, retry=False, without_capture="interviewer")
        self.assertFalse(self.upstream.closed)
        self.assertIs(self.runtime.main_upstream, self.upstream)
        self.runtime._capture_clients.clear()

    async def test_terminal_track_failure_releases_model_without_ending_room(self):
        from tests.test_realtime import FakeSocket
        capture = FakeSocket()
        self.runtime._capture_clients["interviewer"] = capture
        await self.runtime.mark_capture_status("interviewer", capture, {"phase": "error", "detail": "track ended"})
        self.assertTrue(self.upstream.closed)
        self.assertIsNone(self.runtime.main_upstream)
        self.assertTrue(self.runtime.active)
        self.assertIs(self.runtime._capture_clients["interviewer"], capture)
        self.runtime._capture_clients.clear()

    async def test_slow_audio_provider_does_not_queue_old_speech_or_block_capture_controls(self):
        from tests.test_realtime import FakeSocket
        class Capture(FakeSocket):
            def __init__(self):
                super().__init__()
                self.incoming = asyncio.Queue()
            async def receive(self):
                return await self.incoming.get()
        capture = Capture()
        started, unblock = asyncio.Event(), asyncio.Event()
        delivered = []
        async def slow_send(upstream, data, **kwargs):
            delivered.append(data)
            if data == b"first":
                started.set()
                await unblock.wait()
        mark = AsyncMock()
        with patch.object(rt, "_send_audio_append", slow_send), patch.object(self.runtime, "mark_capture_status", mark):
            receiver = asyncio.create_task(rt._forward_capture_controls(self.runtime, capture, "interviewer"))
            try:
                capture.incoming.put_nowait({"type": "websocket.receive", "bytes": b"first"})
                await asyncio.wait_for(started.wait(), 1)
                for _ in range(100):
                    capture.incoming.put_nowait({"type": "websocket.receive", "bytes": b"obsolete"})
                capture.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "capture_status", "phase": "ready"})})
                await until(lambda: mark.await_count == 1)
                self.assertEqual(delivered, [b"first"])
                self.assertEqual(self.runtime.metrics["audio_gaps"], 100)
                unblock.set()
                await asyncio.sleep(0)
                capture.incoming.put_nowait({"type": "websocket.receive", "bytes": b"fresh"})
                await until(lambda: len(delivered) == 2)
                self.assertEqual(delivered, [b"first", b"fresh"])
                capture.incoming.put_nowait({"type": "websocket.disconnect"})
                await receiver
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    async def test_frame_held_during_long_provider_startup_is_not_replayed(self):
        from tests.test_realtime import FakeSocket
        incoming = asyncio.Queue()
        capture = FakeSocket()
        capture.receive = incoming.get
        entered, connected = asyncio.Event(), asyncio.Event()
        async def connect():
            entered.set()
            await connected.wait()
            return self.upstream
        send_audio = AsyncMock()
        now = [100.0]
        with patch.object(self.runtime, "ensure_main", connect), patch.object(rt, "_send_audio_append", send_audio), \
                patch.object(rt, "time", SimpleNamespace(monotonic=lambda: now[0])):
            receiver = asyncio.create_task(rt._forward_capture_controls(self.runtime, capture, "interviewer"))
            try:
                incoming.put_nowait({"type": "websocket.receive", "bytes": b"old"})
                await entered.wait()
                now[0] = 101
                connected.set()
                await asyncio.sleep(0)
                send_audio.assert_not_awaited()
                self.assertEqual(self.runtime.metrics["audio_gaps"], 1)
                incoming.put_nowait({"type": "websocket.disconnect"})
                await receiver
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    async def test_end_cancels_inflight_audio_connection_without_waiting_for_handshake(self):
        from tests.test_realtime import FakeSocket
        capture = FakeSocket()
        incoming = asyncio.Queue()
        capture.receive = incoming.get
        entered = asyncio.Event()
        async def connect():
            entered.set()
            await asyncio.Future()
        with patch.object(self.runtime, "ensure_candidate", connect):
            receiver = asyncio.create_task(rt._forward_capture_controls(self.runtime, capture, "candidate"))
            try:
                incoming.put_nowait({"type": "websocket.receive", "bytes": b"pending"})
                await entered.wait()
                await asyncio.wait_for(self.runtime.close(), 1)
                self.assertFalse(self.runtime._audio_tasks)
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    async def test_long_interview_reconnect_preserves_order_all_answers_code_and_screens(self):
        from tests.test_realtime import PNG_DATA_URL, FakeSocket
        from app.services.code_workspace import record_code_change
        from app.services.live_session import LiveSession
        for index in range(200):
            question_id = f"q-{index}"
            await self.runtime.emit_transcript_final("interviewer", f"Question {index}", turn_id=question_id)
            await self.runtime.update_candidate_transcript(f"c-{index}", f"Answer choice {index}", "completed")
            await rt._begin_response(self.runtime, f"r-{index}", {"question_id": question_id})
            await rt._emit_answer_delta(self.runtime, f"r-{index}", f"Explanation {index}. " * 20)
            await rt._emit_terminal(self.runtime, response_id=f"r-{index}", event_type="answer_completed", text=None, detail="")
            if index % 20 == 0:
                self.runtime.history.add_screen(f"s-{index}", PNG_DATA_URL, "Synthetic page", question_id=question_id)
                self.runtime.code_workspace.commit(f"value = {index}", "python")
                record_code_change(self.runtime, "automatic", question_id=question_id)
        before = self.runtime.history.snapshot(self.runtime.response_buffers, self.runtime.response_status)
        next_socket = Upstream()
        await LiveSession(self.runtime).start(next_socket)
        histories = [event["item"]["content"] for event in next_socket.messages if event.get("item", {}).get("type") == "message"
            and "Complete observed interview history" in event["item"]["content"][0].get("text", "")]
        self.assertEqual(len(histories), 1)
        records = json.loads(histories[0][0]["text"].split("\n", 1)[1])["records"]
        self.assertEqual(records, before[0])
        self.assertEqual(len(histories[0]) - 1, 10)
        restored = FakeSocket()
        await self.runtime._send_answer_snapshots_locked(restored)
        self.assertEqual([e["response_id"] for e in restored.messages], [f"r-{i}" for i in range(200)])
        self.assertTrue(all(e["status"] == "completed" for e in restored.messages))
        self.assertEqual(self.runtime.code_workspace.code, "value = 180")

    async def test_transcription_handshake_timeout_closes_only_candidate(self):
        candidate = Upstream()
        candidate.send = AsyncMock()
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=candidate)), \
                patch.object(rt, "TRANSCRIPTION_START_TIMEOUT_SECONDS", .01):
            with self.assertRaises(TimeoutError):
                await self.runtime.ensure_candidate()
        self.assertTrue(candidate.closed)
        self.assertIsNone(self.runtime.candidate_upstream)
        self.assertIs(await self.runtime.ensure_main(), self.upstream)

    async def test_ending_interview_during_transcription_handshake_cannot_publish_a_new_connection(self):
        candidate = Upstream()
        candidate.send = AsyncMock()
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=candidate)):
            connecting = asyncio.create_task(self.runtime.ensure_candidate())
            await until(lambda: candidate.send.await_count == 1)
            closing = asyncio.create_task(self.runtime.close())
            try:
                await until(lambda: self.runtime.closed)
                candidate.events.put_nowait({"type": "session.updated"})
                with self.assertRaises(rt.OpenAIRealtimeError):
                    await asyncio.wait_for(connecting, 1)
                await asyncio.wait_for(closing, 1)
                self.assertTrue(candidate.closed)
                self.assertIsNone(self.runtime.candidate_upstream)
                self.assertIsNone(self.runtime._candidate_reader_task)
            finally:
                connecting.cancel()
                closing.cancel()
                await asyncio.gather(connecting, closing, return_exceptions=True)

    async def test_transcription_waits_for_configuration_acceptance_before_audio(self):
        candidate = Upstream()
        candidate.send = AsyncMock()
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=candidate)):
            connecting = asyncio.create_task(self.runtime.ensure_candidate())
            try:
                await until(lambda: candidate.send.await_count == 1)
                self.assertFalse(connecting.done())
                self.assertIsNone(self.runtime.candidate_upstream)
                candidate.events.put_nowait({"type": "session.created"})
                candidate.events.put_nowait({"type": "session.updated"})
                self.assertIs(await asyncio.wait_for(connecting, 1), candidate)
                self.assertEqual(self.runtime._model_channels["candidate"]["status"], "ready")
            finally:
                connecting.cancel()
                await asyncio.gather(connecting, return_exceptions=True)

    async def test_transcription_rejection_is_closed_and_retry_is_delayed(self):
        candidate = Upstream()
        candidate.send = AsyncMock()
        candidate.events.put_nowait({"type": "error", "error": {"message": "SYNTHETIC_PRIVATE_ERROR"}})
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=candidate)) as connect:
            with self.assertRaises(rt.OpenAIRealtimeError) as caught:
                await self.runtime.ensure_candidate()
            self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", str(caught.exception))
            self.assertTrue(candidate.closed)
            self.assertIsNone(self.runtime.candidate_upstream)
            with self.assertRaises(rt.OpenAIRealtimeError):
                await self.runtime.ensure_candidate()
            self.assertEqual(connect.await_count, 1)

    async def test_repeated_immediate_disconnects_back_off_instead_of_reconnecting_per_audio_frame(self):
        for failure in range(3):
            candidate = Upstream()
            self.runtime._candidate_retry_after = 0
            with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=candidate)):
                await self.runtime.ensure_candidate()
            before = time.monotonic()
            await self.runtime._release_upstream("candidate", candidate)
            self.assertEqual(self.runtime._candidate_failures, failure + 1)
            self.assertGreaterEqual(self.runtime._candidate_retry_after - before, 2 ** failure)
            with patch.object(rt, "_connect_openai_realtime", AsyncMock()) as connect:
                with self.assertRaises(rt.OpenAIRealtimeError):
                    await self.runtime.ensure_candidate()
                connect.assert_not_awaited()

    async def test_candidate_forwarding_timeout_retains_text_and_recovers_main(self):
        from app.services import live_session
        async def stuck_send(data):
            await asyncio.Future()
        self.upstream.send = stuck_send
        with patch.object(live_session, "SEND_TIMEOUT_SECONDS", .01):
            await asyncio.wait_for(self.runtime.append_candidate_context("RETAINED TEXT"), 1)
        self.assertEqual(list(self.runtime.pending_candidate_context), ["RETAINED TEXT"])
        self.assertTrue(self.upstream.closed)
        self.assertIsNone(self.runtime.main_upstream)

    async def test_candidate_connect_does_not_block_healthy_main_audio(self):
        entered, release = asyncio.Event(), asyncio.Event()
        candidate = Upstream()
        async def connect(**kwargs):
            entered.set()
            await release.wait()
            return candidate
        with patch.object(rt, "_connect_openai_realtime", connect):
            connecting = asyncio.create_task(self.runtime.ensure_candidate())
            try:
                await entered.wait()
                main = await asyncio.wait_for(self.runtime.ensure_main(), .1)
                await rt._send_audio_append(main, b"audio", live=True)
                self.assertEqual(main.messages[-1]["type"], "session.input_audio.append")
            finally:
                release.set()
                await connecting

    async def test_slow_main_reconnect_does_not_block_candidate_transcription_connection(self):
        await self.runtime.reset_main("test")
        entered, release = asyncio.Event(), asyncio.Event()
        main, candidate = Upstream(), Upstream()
        async def connect(*, kind):
            if kind == "main":
                entered.set()
                await release.wait()
                return main
            return candidate
        with patch.object(rt, "_connect_openai_realtime", connect):
            connecting = asyncio.create_task(self.runtime.ensure_main())
            try:
                await entered.wait()
                self.assertIs(await asyncio.wait_for(self.runtime.ensure_candidate(), .1), candidate)
            finally:
                release.set()
                await connecting

    async def test_dropped_ui_connection_is_closed_so_it_can_reconnect(self):
        from tests.test_realtime import FakeClientWebSocket, attach_ui
        stale, healthy = FakeClientWebSocket({}), FakeClientWebSocket({})
        stale.send_json = AsyncMock(side_effect=TimeoutError())
        attach_ui(self.runtime, stale, "stale")
        attach_ui(self.runtime, healthy, "healthy")
        await self.runtime._broadcast_clients_locked({"type": "test"})
        self.assertEqual(stale.closed_codes, [1013])
        self.assertEqual(healthy.messages[-1], {"type": "test"})
        self.assertIn(healthy, self.runtime._ui_clients.values())


    async def test_reconnect_replays_all_recorded_context_and_draft_provenance(self):
        await self.runtime.emit_transcript_final("interviewer", "EARLY_QUESTION", turn_id="q1")
        await self.runtime.emit_transcript_final("candidate", "I chose B", turn_id="c1")
        await rt._begin_response(self.runtime, "a1")
        await rt._emit_answer_delta(self.runtime, "a1", "SUGGEST_A")
        await rt._emit_terminal(self.runtime, response_id="a1", event_type="answer_completed", text=None, detail="completed")
        self.runtime.history.add_analysis("deep1", "q1", "EARLY_QUESTION", "PRIOR_CODE")
        self.runtime.history.add_screen("screen1", "data:image/png;base64,AAAA", "old frame", question_id="q1", source_id="screen:2")
        await self.runtime.reset_main("test reconnect")
        replacement = Upstream()
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=replacement)):
            await self.runtime.ensure_main()
        wire = json.dumps(replacement.messages)
        for expected in ["COMPLETE_BACKGROUND", "EARLY_QUESTION", "I chose B", "SUGGEST_A", "PRIOR_CODE", "screen:2", "not evidence", "AAAA"]:
            self.assertIn(expected, wire)
        self.assertTrue(self.upstream.closed)
        self.assertEqual(self.runtime.response_buffers["a1"], "SUGGEST_A")

    async def test_candidate_manual_context_does_not_create_or_cancel_answer(self):
        await self.runtime.start_operation({"type": "manual_text", "kind": "candidate_context", "text": "Actually I chose B", "operation_id": "context"}, object())
        await until(lambda: self.runtime.operations["context"]["status"] == "completed")
        self.assertFalse(any(m["type"] in {"response.create", "response.cancel"} for m in self.upstream.messages))
        self.assertEqual(self.runtime.history.turns[-1]["speaker"], "candidate")


    async def test_candidate_injection_failure_keeps_queue_and_allows_next_transcript(self):
        self.upstream.send = AsyncMock(side_effect=OSError("network down"))
        await self.runtime.append_candidate_context("FIRST")
        await self.runtime.append_candidate_context("SECOND")
        self.assertEqual(list(self.runtime.pending_candidate_context), ["FIRST", "SECOND"])
        self.assertIsNone(self.runtime.main_upstream)
        replacement = Upstream()
        self.runtime._main_retry_after = 0
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=replacement)):
            await self.runtime.ensure_main()
        wire = json.dumps(replacement.messages)
        self.assertIn("FIRST", wire)
        self.assertIn("SECOND", wire)
        self.assertEqual(list(self.runtime.pending_candidate_context), [])


    async def test_paused_correction_is_preserved_with_explicit_history_relationship(self):
        await self.runtime.emit_transcript_final("interviewer", "O(n squared)", turn_id="question")
        self.runtime.hold_answers = True
        await self.runtime.start_operation({"type": "manual_text", "kind": "correction", "question_id": "question", "text": "O(n log n)", "operation_id": "fix"}, object())
        await until(lambda: self.runtime.operations["fix"]["status"] in rt.OPERATION_TERMINAL_STATUSES)
        self.assertEqual(self.runtime.operations["fix"]["status"], "completed")
        self.assertEqual(self.runtime.history.turns[-1]["corrects_turn_id"], "question")
        self.assertIn("O(n log n)", json.dumps(self.upstream.messages))
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))

    async def test_muted_channel_remains_ready_and_preserves_phase_in_public_snapshot(self):
        socket = object()
        self.runtime._capture_clients["candidate"] = socket
        self.runtime._capture_ready.add("candidate")
        await self.runtime.mark_capture_status("candidate", socket, {"phase": "muted", "detail": "source muted"})
        state = await self.runtime.public_state()
        self.assertTrue(state["device_status"]["channels"]["candidate"])
        self.assertEqual(state["device_status"]["channel_details"]["candidate"]["phase"], "muted")
        self.runtime._capture_clients.clear()


    async def test_candidate_correction_during_astra_discards_outdated_analysis(self):
        requested = asyncio.Event()
        finish = asyncio.Event()
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"status": "completed", "output_text": "Candidate owned pricing", "usage": {"input_tokens": 10, "output_tokens": 20}}
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, *args, **kwargs):
                requested.set()
                await finish.wait()
                return Response()
        await self.runtime.emit_transcript_final("candidate", "I owned pricing", turn_id="c1")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-only"}), patch.object(rt.httpx, "AsyncClient", return_value=Client()):
            analysis = asyncio.create_task(rt._analyze_problem(self.runtime, "Explain ownership"))
            await requested.wait()
            await self.runtime.emit_transcript_final("candidate", "I did not own pricing", turn_id="c1", corrects_turn_id="c1")
            await self.runtime.append_candidate_context("[ASR correction, not a change of decision] I did not own pricing")
            finish.set()
            with self.assertRaisesRegex(rt.OpenAIRealtimeError, "outdated analysis was discarded"):
                await analysis
        self.assertFalse(any(entry["kind"] == "analysis" for entry in self.runtime.history.entries))
        self.assertEqual(self.runtime.metrics["analysis_output_tokens"], 20)
        self.assertFalse(any(message["type"] == "response.create" for message in self.upstream.messages))

    async def test_main_recovery_does_not_hide_failed_candidate_connection(self):
        await self.runtime.update_model_status("candidate", "recovering", "Candidate context unavailable")
        await self.runtime.update_model_status("main", "ready", "Main connected")
        self.assertEqual(self.runtime._model_status["status"], "recovering")
        self.assertIn("Candidate context unavailable", self.runtime._model_status["detail"])
        await self.runtime.update_model_status("candidate", "ready", "Candidate connected")
        self.assertEqual(self.runtime._model_status["status"], "ready")

    async def test_replacement_waits_for_physical_close_and_rejects_old_code_task(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_close = self.upstream.close
        async def close():
            entered.set()
            await release.wait()
            await original_close()
        self.upstream.close = close
        old_live = self.runtime.live
        old_task = old_live.snapshot()
        resetting = asyncio.create_task(self.runtime.reset_main("reconnect"))
        await entered.wait()
        replacement = Upstream()
        with patch.object(rt, "_connect_openai_realtime", AsyncMock(return_value=replacement)) as connect:
            connecting = asyncio.create_task(self.runtime.ensure_main())
            await asyncio.sleep(0)
            connect.assert_not_awaited()
            release.set()
            await resetting
            await connecting
        self.assertTrue(self.upstream.closed)
        self.assertFalse(old_live.current(old_task))
        self.assertIs(self.runtime.main_upstream, replacement)


if __name__ == "__main__":
    unittest.main()
