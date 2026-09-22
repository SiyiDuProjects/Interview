from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.services import openai_realtime as rt
from app.services.live_session import LiveSession, append_context
from app.services.interview_tools import InterviewTool, TOOLS, tool_schema
from app.services.candidate_transcript import CandidateTranscriptRelay
from tests.test_realtime import (
    FakeUpstream, FakeSocket, FakeClientWebSocket, make_runtime, attach_live,
    attach_ui, finish_operations, PNG_DATA_URL,
)


class LiveHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = make_runtime()
        self.runtime.active = True
        self.upstream = FakeUpstream()
        self.live = attach_live(self.runtime, self.upstream)
        self.client = FakeSocket()
        attach_ui(self.runtime, self.client)
        await self.runtime.remember_dialogue("interviewer", "Return duplicate emails.")

    async def asyncTearDown(self):
        await self.runtime.close()

    async def delegate(self, response_id="r", delegation_id="d"):
        await self.live.event({"type": "session.delegation.created", "offset_ms": 10000,
            "delegation": {"id": delegation_id, "type": "delegation", "target": "responses", "response_id": response_id}})
        await self.backend("response.created", response={"id": response_id}, delegation_id=delegation_id)

    async def backend(self, kind, *, delegation_id="d", client_event_id=None, **event):
        await self.live.event({"type": "response.event", "delegation_id": delegation_id,
            "client_event_id": client_event_id, "event": {"type": kind, **event}})

    async def call(self, name, args=None, *, call_id="c", delegation_id="d"):
        item = {"type": "function_call", "call_id": call_id, "name": name,
                "arguments": json.dumps(args or {})}
        await self.backend("response.output_item.added", delegation_id=delegation_id, item=item)
        await self.backend("response.output_item.done", delegation_id=delegation_id, item=item)
        if call_id in self.live.jobs:
            await self.live.jobs[call_id]
        return next(json.loads(m["item"]["output"]) for m in reversed(self.upstream.messages)
                    if m.get("item", {}).get("call_id") == call_id)

    def change(self, code="SELECT 1;"):
        doc = self.runtime.code_workspace
        return {"document_id": doc.document_id, "base_revision": doc.revision,
                "context_version": self.runtime.material_revision, "code": code,
                "language": "sql", "explanation": "Return one. 返回一。"}

    async def caption(self, text, end_ms=100):
        await self.live.event({"type": "session.output_transcript.delta", "delta": text, "end_ms": end_ms})


class LiveTests(LiveHarness):
    async def test_hosted_task_timeout_releases_busy_state_and_rejects_late_code(self):
        with patch("app.services.live_session.BACKEND_TASK_TIMEOUT_SECONDS", .1):
            await self.delegate()
        await self.call("search_context", call_id="read")
        await self.backend("response.output_item.added", item={"name": "update_code"})
        self.assertTrue(self.runtime.code_workspace.run_id)
        # Wait on the actual deadline, not a timing assumption.
        await asyncio.gather(*list(self.live.deadlines.values()))
        self.assertFalse(self.runtime.code_workspace.run_id)
        self.assertFalse((await self.call("update_code", self.change(), call_id="late"))["ok"])
        self.assertEqual(self.runtime.code_workspace.revision, 0)
        self.assertTrue(any(e.get("type") == "tool_error" and "超时" in e.get("detail", "") for e in self.client.messages))

    async def test_explicit_request_without_any_provider_event_eventually_fails(self):
        self.runtime.operations["manual"] = {"operation_id": "manual", "status": "running"}
        with patch("app.services.live_session.BACKEND_TASK_TIMEOUT_SECONDS", .01):
            await self.live.request("Explain", "manual", None, True)
            await asyncio.gather(*list(self.live.deadlines.values()))
        self.assertEqual(self.runtime.operations["manual"]["status"], "failed")
        self.assertFalse(self.live.manual_allow_held)
        request = next(m for m in self.upstream.messages if m["type"] == "response.create")
        await self.backend("response.created", response={"id": "late"}, client_event_id=request["event_id"])
        self.assertFalse(self.live.active(self.live.responses["late"]))

    async def test_completed_backend_does_not_leave_a_timeout_or_late_write_permission(self):
        await self.delegate()
        await self.backend("response.completed", response={"id": "r"})
        self.assertFalse(self.live.deadlines)
        self.assertFalse((await self.call("update_code", self.change(), call_id="after-complete"))["ok"])

    async def test_unused_output_audio_does_not_accumulate_event_ids(self):
        for index in range(2000):
            await self.live.event({"type": "session.output_audio.delta", "event_id": f"audio-{index}", "delta": "AAAA"})
        self.assertEqual(len(self.live.seen), 0)
        self.assertFalse(self.runtime.response_order)

    async def test_failed_tool_result_delivery_closes_socket_but_keeps_committed_code(self):
        await self.delegate()
        await self.call("search_context", call_id="read")
        original_send = self.upstream.send
        async def send(payload):
            message = json.loads(payload)
            if message.get("item", {}).get("call_id") == "write":
                raise OSError("delivery failed")
            await original_send(payload)
        self.upstream.send = send
        item = {"type": "function_call", "name": "update_code", "call_id": "write", "arguments": json.dumps(self.change())}
        await self.backend("response.output_item.done", item=item)
        await self.live.jobs["write"]
        self.assertTrue(self.upstream.closed)
        self.assertEqual(self.runtime.code_workspace.code, "SELECT 1;")
        self.assertEqual(self.runtime.code_workspace.revision, 1)
        self.assertFalse(self.runtime.code_workspace.run_id)

    async def test_changed_candidate_context_can_be_reread_without_reviving_stale_code(self):
        await self.delegate()
        await self.call("search_context", call_id="read-old")
        stale = self.change("obsolete")
        await self.runtime.update_candidate_transcript("c1", "Use constant space", "streaming", delta="Use constant space")
        self.assertFalse((await self.call("update_code", stale, call_id="old-code"))["ok"])
        await self.backend("response.completed", response={"id": "r", "output": []})
        requests = [m for m in self.upstream.messages if m["type"] == "response.create"]
        self.assertEqual(len(requests), 1, "The existing tool loop must be able to read the new context")
        await self.backend("response.created", client_event_id=requests[0]["event_id"], response={"id": "r2"})
        # Merely continuing never grants a stale write permission.
        self.assertFalse((await self.call("update_code", self.change("still unread"), call_id="unread-new"))["ok"])
        read = await self.call("search_context", call_id="read-new")
        self.assertTrue(read["ok"])
        self.assertEqual(read["workspace"]["context_version"], self.runtime.material_revision)
        self.assertEqual(read["transcripts"][-1]["text"], "Use constant space")
        self.assertTrue((await self.call("update_code", self.change("reconsidered"), call_id="new-code"))["ok"])
        self.assertEqual(self.runtime.code_workspace.code, "reconsidered")

    async def test_context_read_cannot_revive_cancelled_or_manually_edited_task(self):
        for reason in ("cancel", "manual-edit", "new-question", "reset"):
            with self.subTest(reason=reason):
                await self.delegate(reason, reason)
                if reason == "cancel":
                    await self.live.cancel()
                elif reason == "manual-edit":
                    self.runtime.code_workspace.commit("user edit", "python")
                elif reason == "new-question":
                    await self.runtime.invalidate_work(question_id="new")
                else:
                    self.runtime.code_workspace.document_id = "new-document"
                result = await self.call("search_context", call_id=reason, delegation_id=reason)
                self.assertFalse(result["ok"])
                await self.backend("response.completed", delegation_id=reason, response={"id": reason})
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))

    async def test_successful_tool_then_new_context_still_allows_a_fresh_read(self):
        await self.delegate()
        await self.call("search_context", call_id="initial")
        await self.runtime.update_candidate_transcript("c1", "Keep duplicates", "completed")
        await self.backend("response.completed", response={"id": "r"})
        self.assertEqual(sum(m["type"] == "response.create" for m in self.upstream.messages), 1)
        self.assertFalse(self.live.current(self.live.tasks["d"]))

    async def test_candidate_delta_reaches_live_and_backend_before_final_and_blocks_old_code(self):
        await self.delegate()
        await self.call("search_context", call_id="read")
        args = self.change()
        relay = CandidateTranscriptRelay(self.runtime)
        try:
            await relay.add("Use a hash map", "a")
            async with asyncio.timeout(1):
                while not any(m["type"] == "session.thinking.append" for m in self.upstream.messages):
                    await asyncio.sleep(0)
            live_update = next(m for m in self.upstream.messages if m["type"] == "session.thinking.append")
            self.assertEqual(json.loads(live_update["content"])["delta"], "Use a hash map")
            backend_update = next(m["item"]["content"][0]["text"] for m in self.upstream.messages
                                  if m.get("item", {}).get("type") == "message")
            self.assertEqual(json.loads(backend_update)["status"], "streaming")
            self.assertEqual(self.runtime.history.turns[-1]["status"], "streaming")
            self.assertFalse((await self.call("update_code", args))["ok"])
            self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))
        finally:
            await relay.close()

    async def test_candidate_reader_native_events_preserve_interleaved_turns(self):
        class Events:
            def __aiter__(inner):
                async def stream():
                    for event in [
                        {"type": "input_audio_buffer.speech_started", "item_id": "a"},
                        {"type": "conversation.item.input_audio_transcription.delta", "item_id": "a", "delta": "First"},
                        {"type": "input_audio_buffer.speech_stopped", "item_id": "a"},
                        {"type": "input_audio_buffer.committed", "item_id": "a", "previous_item_id": None},
                        {"type": "input_audio_buffer.speech_started", "item_id": "b"},
                        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "b", "transcript": "Second"},
                        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "a", "transcript": "First final"},
                    ]:
                        yield json.dumps(event)
                return stream()
        await rt._forward_candidate_events(self.runtime, Events())
        turns = [turn for turn in self.runtime.history.turns if turn["speaker"] == "candidate"]
        self.assertEqual([turn["text"] for turn in turns], ["First final", "Second"])
        self.assertTrue(all(turn["status"] == "completed" for turn in turns))
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))

    async def test_additional_tool_uses_existing_transport_and_deduplicates_calls(self):
        handler = AsyncMock(return_value={"ok": True, "explanation": "A reference-only explanation."})
        extension = InterviewTool("Inspect the current code without changing it.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, handler)
        with patch.dict(TOOLS, {"inspect_code": extension}):
            self.assertEqual(tool_schema()[-1]["name"], "inspect_code")
            await self.delegate()
            result = await self.call("inspect_code")
            await self.call("inspect_code")
        self.assertEqual(result, {"ok": True, "explanation": "A reference-only explanation."})
        handler.assert_awaited_once()
        ctx, args = handler.await_args.args
        self.assertIs(ctx.runtime, self.runtime)
        self.assertEqual(ctx.workspace()["document_id"], self.runtime.code_workspace.document_id)
        self.assertEqual(args, {})
        self.assertEqual(self.runtime.code_workspace.revision, 0)
        self.assertEqual(self.runtime.code_workspace.reveal_id, "")
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))

    async def test_additional_code_tool_keeps_captions_live_and_rejects_stale_work(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(ctx, args):
            entered.set()
            await release.wait()
            ctx.require_current()
            ctx.runtime.code_workspace.commit("late code", "python")
            return {"ok": True}

        extension = InterviewTool("Prepare a code result.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            prepare, opens_code=True)
        with patch.dict(TOOLS, {"prepare_code_result": extension}):
            await self.delegate()
            call = asyncio.create_task(self.call("prepare_code_result"))
            try:
                await entered.wait()
                self.assertEqual(self.runtime.code_workspace.run_id, "live-code:r")
                await self.caption("The explanation continues.", 11000)
                self.assertEqual(self.runtime.response_buffers[self.live.caption_id], "The explanation continues.")
                await self.runtime.emit_transcript_final("candidate", "Use a different approach.")
            finally:
                release.set()
                result = await call
        self.assertFalse(result["ok"])
        self.assertEqual(self.runtime.code_workspace.revision, 0)
        self.assertEqual(self.runtime.code_workspace.run_id, "")

    async def test_additional_tool_cannot_run_for_an_obsolete_task(self):
        handler = AsyncMock(return_value={"ok": True})
        extension = InterviewTool("Inspect code.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, handler)
        with patch.dict(TOOLS, {"inspect_code": extension}):
            await self.delegate()
            await self.runtime.emit_transcript_final("candidate", "New context.")
            result = await self.call("inspect_code")
        self.assertFalse(result["ok"])
        handler.assert_not_awaited()

    async def test_startup_ack_precedes_context_and_audio_and_uses_hosted_tools(self):
        class Gated(FakeUpstream):
            async def send(inner, payload):
                inner.messages.append(json.loads(payload))
        upstream = Gated()
        startup = asyncio.create_task(LiveSession(self.runtime).start(upstream))
        await asyncio.sleep(0)
        self.assertEqual([m["type"] for m in upstream.messages], ["session.start"])
        upstream.queue.put_nowait(json.dumps({"type": "session.started"}))
        await startup
        config = upstream.messages[0]["session"]
        self.assertEqual(config["model"], "gpt-live-1")
        self.assertFalse(config["store"])
        self.assertNotIn("turn_detection", json.dumps(config))
        backend = config["delegation"]["responses"]
        self.assertEqual(backend["model"], "gpt-6-astra")
        self.assertEqual(backend["max_output_tokens"], 8192)
        self.assertEqual(backend["reasoning"]["effort"], "high")
        self.assertEqual([t["name"] for t in backend["tools"]], ["search_context", "capture_current_screen", "update_code"])
        self.assertFalse(any(m["type"] == "response.create" for m in upstream.messages))

    async def test_endpoints_and_audio_wire_keep_sources_separate(self):
        connect = AsyncMock(return_value=self.upstream)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder", "OPENAI_BASE_URL": "https://api.openai.com/v1"}), patch.object(rt.websockets, "connect", connect):
            await rt._connect_openai_realtime(kind="main")
            await rt._connect_openai_realtime(kind="candidate")
        self.assertEqual([c.args[0] for c in connect.call_args_list],
                         ["wss://api.openai.com/v1/live/sessions", "wss://api.openai.com/v1/realtime?intent=transcription"])
        await rt._send_audio_append(self.upstream, b"interviewer", live=True)
        await rt._send_audio_append(self.upstream, b"candidate")
        self.assertEqual([m["type"] for m in self.upstream.messages], ["session.input_audio.append", "input_audio_buffer.append"])

    async def test_continuous_captions_append_without_audio_or_backend_text_leaking(self):
        await self.caption("First explanation.")
        first = self.live.caption_id
        await self.backend("response.output_text.delta", delta="SECRET_CODE")
        await self.live.event({"type": "session.output_audio.delta", "delta": "AUDIO"})
        await self.live.finish_caption()
        await self.caption("Then the refinement.", 200)
        second = self.live.caption_id
        await self.live.finish_caption()
        self.assertNotEqual(first, second)
        self.assertEqual(self.runtime.response_order, [first, second])
        self.assertEqual(self.runtime.response_buffers[first], "First explanation.")
        self.assertNotIn("SECRET_CODE", json.dumps(self.client.messages))
        self.assertNotIn("AUDIO", json.dumps(self.client.messages))

    async def test_input_fragments_have_one_history_entry_and_do_not_manually_trigger_response(self):
        for i, delta in enumerate(["Use a ", "hash map."]):
            event = {"type": "session.input_transcript.delta", "event_id": str(i), "delta": delta, "end_ms": i+1}
            await self.live.event(event)
            await self.live.event(event)
        await self.live.flush_input()
        self.assertEqual(self.runtime.recent_dialogue[-1]["text"], "Use a hash map.")
        self.assertEqual(self.runtime.material_revision, 2)
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))

    async def test_candidate_context_is_silent_complete_and_invalidates_stale_code(self):
        await self.delegate()
        await self.call("search_context", call_id="read")
        args = self.change()
        text = "候选人上下文" * 300
        await self.runtime.emit_transcript_final("candidate", text)
        await self.runtime.append_candidate_context(text)
        chunks = [m["content"] for m in self.upstream.messages if m["type"] == "session.thinking.append"]
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c.encode("utf-8")) <= 400 for c in chunks))
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))
        result = await self.call("update_code", args)
        self.assertFalse(result["ok"])
        self.assertEqual(self.runtime.code_workspace.revision, 0)

    async def test_code_tool_commits_once_preserves_diff_undo_and_snapshot(self):
        await self.delegate()
        read = await self.call("search_context", call_id="read")
        self.assertEqual(read["documents"], [])
        self.assertEqual(read["workspace"]["revision"], 0)
        result = await self.call("update_code", self.change())
        self.assertTrue(result["ok"])
        doc = self.runtime.code_workspace
        self.assertEqual((doc.code, doc.revision), ("SELECT 1;", 1))
        self.assertEqual(doc.last_change["base_code"], "")
        self.assertEqual(doc.reveal_id, "live-code:r")
        await self.call("update_code", self.change("SELECT 2;"))  # duplicate call id
        self.assertEqual(doc.revision, 1)
        client = FakeClientWebSocket({"type": "authenticate", "token": self.runtime.session_token})
        await self.runtime.serve(client, "client")
        state = next(m["workspace"] for m in client.messages if m["type"] == "code_state")
        self.assertEqual(state["code"], doc.code)
        await self.runtime.start_operation({"type": "code_action", "action": "undo",
            "document_id": doc.document_id, "base_revision": 1}, self.client)
        await finish_operations(self.runtime)
        self.assertEqual((doc.code, doc.revision), ("", 2))

    async def test_context_change_manual_edit_reset_and_stop_reject_late_tool(self):
        for reason in ("speech", "save", "reset", "stop", "screen"):
            with self.subTest(reason=reason):
                await self.delegate(reason, reason)
                await self.call("search_context", call_id=f"read-{reason}", delegation_id=reason)
                args = self.change("obsolete")
                doc = self.runtime.code_workspace
                if reason == "speech":
                    await self.runtime.invalidate_work(question_id="new-question")
                elif reason == "save":
                    doc.commit("my manual edit", "python")
                elif reason == "reset":
                    doc.document_id = "new-document"
                elif reason == "stop":
                    doc.run_id = f"live-code:{reason}"
                    from app.services.code_workspace import cancel_code_run
                    cancel_code_run(self.runtime)
                else:
                    await rt._record_screen(self.runtime, None, "new-screen", PNG_DATA_URL, "new-question")
                result = await self.call("update_code", args, call_id=f"edit-{reason}", delegation_id=reason)
                self.assertFalse(result["ok"])
                self.assertNotEqual(doc.code, "obsolete")

    async def test_unknown_task_and_unread_document_never_gain_write_access(self):
        await self.backend("response.created", delegation_id=None, response={"id": "unknown"})
        result = await self.call("update_code", self.change(), delegation_id=None)
        self.assertFalse(result["ok"])
        await self.delegate()
        result = await self.call("update_code", self.change(), call_id="unread")
        self.assertFalse(result["ok"])

    async def test_tools_continue_only_after_terminal_and_all_results(self):
        await self.delegate()
        await self.call("search_context", call_id="read")
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))
        await self.backend("response.completed", response={"id": "r", "output": [], "usage": {"input_tokens": 10}})
        requests = [m for m in self.upstream.messages if m["type"] == "response.create"]
        self.assertEqual(len(requests), 1)
        await self.backend("response.completed", response={"id": "r", "output": []})
        self.assertEqual(self.runtime.metrics["analysis_input_tokens"], 10)
        await self.backend("response.created", delegation_id=None, client_event_id=requests[0]["event_id"], response={"id": "r2"})
        self.assertIs(self.live.responses["r2"], self.live.tasks["d"])

    async def test_failed_or_superseded_backend_does_not_continue(self):
        for kind in ("response.failed", "response.incomplete", "response.cancelled"):
            await self.delegate(kind, kind)
            await self.call("search_context", call_id=kind, delegation_id=kind)
            await self.backend(kind, delegation_id=kind, response={"id": kind})
        self.assertFalse(any(m["type"] == "response.create" for m in self.upstream.messages))

    async def test_explicit_deep_request_uses_hosted_backend_and_selected_answer(self):
        await self.caption("Original answer")
        selected = self.live.caption_id
        await self.live.finish_caption()
        with patch.object(rt, "_analyze_problem", new=AsyncMock()) as http_analysis:
            await self.runtime.start_operation({"type": "quick_answer", "action": "deep",
                "response_id": selected, "operation_id": "deep"}, self.client)
            await finish_operations(self.runtime)
            http_analysis.assert_not_awaited()
        requests = [m for m in self.upstream.messages if m["type"] == "response.create"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(set(requests[0]), {"type", "event_id"})
        self.assertIn("Original answer", json.dumps(self.upstream.messages))
        await self.backend("response.created", delegation_id=None, client_event_id=requests[0]["event_id"], response={"id": "manual"})
        self.assertTrue(self.live.current(self.live.responses["manual"]))
        await self.backend("response.completed", delegation_id=None, response={"id": "manual"})
        self.assertEqual(self.runtime.operations["deep"]["status"], "completed")

    async def test_pause_blocks_native_captions_and_tools_but_explicit_screens_can_run(self):
        self.runtime.hold_answers = True
        await self.delegate()
        self.assertFalse((await self.call("search_context"))["ok"])
        await self.caption("hidden")
        self.assertEqual(self.runtime.response_order, [])
        await self.runtime.request_response(revision=self.runtime.context_revision, allow_held=True, operation_id="screens")
        request = next(m for m in reversed(self.upstream.messages) if m["type"] == "response.create")
        await self.backend("response.created", delegation_id=None, client_event_id=request["event_id"], response={"id": "screens"})
        self.assertTrue(self.live.current(self.live.responses["screens"]))
        await self.caption("Explicit screen explanation", 1000)
        self.assertTrue(self.runtime.response_order)
        # The explicit request's permission cannot leak into native background work.
        await self.delegate("automatic-while-held", "automatic-while-held")
        self.assertFalse((await self.call("search_context", call_id="held-automatic-read", delegation_id="automatic-while-held"))["ok"])

    async def test_provider_errors_do_not_expose_private_payload(self):
        await self.live.event({"type": "error", "error": {"message": "SECRET_PROFILE_AND_KEY"}})
        self.assertNotIn("SECRET_PROFILE_AND_KEY", json.dumps(self.client.messages))

    async def test_manual_tool_items_without_repeated_command_ids_keep_created_response_identity(self):
        await self.live.request("Solve the problem", "manual", None, False)
        request = next(m for m in reversed(self.upstream.messages) if m["type"] == "response.create")
        await self.backend("response.created", delegation_id=None, client_event_id=request["event_id"], response={"id": "manual"})
        self.assertTrue((await self.call("search_context", delegation_id=None, call_id="manual-read"))["ok"])
        self.assertTrue((await self.call("update_code", self.change(), delegation_id=None, call_id="manual-write"))["ok"])

    async def test_ambiguous_uncorrelated_items_are_rejected(self):
        await self.live.request("Solve the problem", "manual", None, False)
        request = next(m for m in reversed(self.upstream.messages) if m["type"] == "response.create")
        await self.backend("response.created", delegation_id=None, client_event_id=request["event_id"], response={"id": "manual"})
        await self.backend("response.created", delegation_id=None, response={"id": "unknown"})
        self.assertFalse((await self.call("search_context", delegation_id=None))["ok"])

    async def test_unrelated_error_keeps_manual_request_but_correlated_error_fails_it(self):
        self.runtime.operations["manual"] = {"operation_id": "manual", "status": "running"}
        await self.live.request("Solve", "manual", None, False)
        request = next(m for m in reversed(self.upstream.messages) if m["type"] == "response.create")
        await self.live.event({"type": "error", "error": {"client_event_id": "unrelated"}})
        self.assertEqual(self.runtime.operations["manual"]["status"], "running")
        await self.live.event({"type": "error", "error": {"client_event_id": request["event_id"]}})
        self.assertEqual(self.runtime.operations["manual"]["status"], "failed")

    async def test_live_usage_remains_cumulative_across_reconnect(self):
        await self.live.event({"type": "session.usage.updated", "usage": {"seconds": 12}})
        next_live = LiveSession(self.runtime)
        await next_live.event({"type": "session.usage.updated", "usage": {"seconds": 5}})
        await next_live.event({"type": "session.usage.updated", "usage": {"seconds": 4}})
        self.assertEqual(self.runtime.metrics["live_session_seconds"], 17)

    async def test_incomplete_function_arguments_clear_busy_state_without_a_commit(self):
        await self.delegate()
        await self.backend("response.output_item.added", item={"type": "function_call", "name": "update_code", "call_id": "partial"})
        self.assertTrue(self.runtime.code_workspace.run_id)
        await self.backend("response.incomplete", response={"id": "r"})
        self.assertFalse(self.runtime.code_workspace.run_id)
        self.assertEqual(self.runtime.code_workspace.revision, 0)

    async def test_duplicate_terminal_does_not_finish_manual_operation_during_tool_continuation(self):
        self.runtime.operations["manual"] = {"operation_id": "manual", "status": "running"}
        await self.delegate()
        self.live.tasks["d"]["operation_id"] = "manual"
        await self.call("search_context")
        for _ in range(2):
            await self.backend("response.completed", response={"id": "r", "output": []})
        self.assertEqual(self.runtime.operations["manual"]["status"], "running")
        self.assertEqual(sum(m["type"] == "response.create" for m in self.upstream.messages), 1)
