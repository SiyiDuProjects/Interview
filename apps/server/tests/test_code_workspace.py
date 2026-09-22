from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.services.code_workspace import CodeWorkspaceError
from app.services.openai_realtime import _analyze_problem
from app.services import openai_realtime as rt
from tests.test_realtime import (
    FakeClientWebSocket, FakeEventStream, FakeHTTPClient, FakeSocket,
    FakeUpstream, PNG_DATA_URL, attach_ui, make_runtime,
)


class CodeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = make_runtime()
        self.runtime.active = True
        self.socket = FakeSocket()
        attach_ui(self.runtime, self.socket)
        self.serial = 0

    async def asyncTearDown(self):
        await self.runtime.close()

    def code_payload(self, action, **kwargs):
        doc = self.runtime.code_workspace
        return {"type": "code_action", "action": action, "document_id": doc.document_id,
                "base_revision": doc.revision, **kwargs}

    async def send(self, payload, *, wait=True):
        self.serial += 1
        payload = {"operation_id": f"op-{self.serial}", **payload}
        await self.runtime.start_operation(payload, self.socket)
        task = self.runtime._jobs.get(payload["operation_id"])
        if wait and task:
            await task
        return payload["operation_id"], task

    async def test_manual_preview_has_a_total_deadline_and_releases_busy_state(self):
        client = FakeHTTPClient()
        async def stalled(*args, **kwargs):
            await asyncio.Future()
        client.post = stalled
        settings = replace(rt.get_settings(), openai_api_key="synthetic", openai_code_timeout_seconds=.01)
        with patch.object(rt, "get_settings", return_value=settings), patch.object(rt.httpx, "AsyncClient", return_value=client):
            operation, task = await self.send(self.code_payload("generate"))
        self.assertEqual(self.runtime.operations[operation]["status"], "failed")
        self.assertFalse(self.runtime.code_workspace.run_id)
        self.assertIsNone(self.runtime.code_workspace.proposal)


    async def test_manual_edit_apply_and_undo_use_monotonic_revisions(self):
        doc = self.runtime.code_workspace
        await self.send(self.code_payload("save", code="return 1", language="python"))
        self.assertEqual(doc.revision, 1)
        result = json.dumps({"code": "return 2", "language": "python", "explanation": "Change the return value. 修改返回值。"})
        with patch("app.services.openai_realtime._analyze_problem", new=AsyncMock(return_value=result)):
            await self.send(self.code_payload("generate"))
        self.assertEqual(doc.code, "return 1")
        await self.send(self.code_payload("apply", proposal_id=doc.proposal["proposal_id"]))
        self.assertEqual((doc.code, doc.revision), ("return 2", 2))
        await self.send(self.code_payload("save", code="return 3", language="python"))
        await self.send(self.code_payload("undo"))
        self.assertEqual((doc.code, doc.revision), ("return 2", 4))
        await self.send(self.code_payload("undo"))
        self.assertEqual((doc.code, doc.revision), ("return 1", 5))
        self.assertTrue(all(entry["meaning"].startswith("Current internal") for entry in self.runtime.history.entries if entry["kind"] == "code_document"))

    async def test_concurrent_clients_and_duplicate_operations_cannot_overwrite(self):
        old = self.code_payload("save", code="first", language="sql")
        operation_id, _ = await self.send(old)
        await self.runtime.start_operation({**old, "operation_id": operation_id, "code": "duplicate"}, self.socket)
        failed, _ = await self.send({**old, "code": "stale client"})
        self.assertEqual(self.runtime.code_workspace.code, "first")
        self.assertEqual(self.runtime.code_workspace.revision, 1)
        self.assertEqual(self.runtime.operations[failed]["status"], "failed")
        self.assertIn("草稿仍保留", self.runtime.operations[failed]["detail"])

    async def test_speech_does_not_cancel_generation_but_flags_new_context(self):
        self.runtime.current_question_id = "original-question"
        entered, release = asyncio.Event(), asyncio.Event()
        async def analyze(*args, **kwargs):
            entered.set()
            await release.wait()
            return json.dumps({"code": "answer = 42", "language": "python", "explanation": "A first version."})
        self.runtime.hold_answers = True
        with patch("app.services.openai_realtime._analyze_problem", new=analyze):
            run_id, task = await self.send(self.code_payload("generate"), wait=False)
            await entered.wait()
            await self.runtime.invalidate_work(question_id="follow-up")
            await self.runtime.emit_transcript_final("candidate", "Consider duplicates too.")
            self.assertFalse(task.done())
            release.set()
            await task
        state = self.runtime.code_state()["workspace"]
        self.assertEqual(self.runtime.history.by_id[f"analysis:{run_id}"]["question_id"], "original-question")
        self.assertTrue(state["proposal"]["context_changed"])
        failed, _ = await self.send(self.code_payload("apply", proposal_id=run_id))
        self.assertEqual(self.runtime.operations[failed]["status"], "failed")
        self.assertEqual(self.runtime.code_workspace.code, "")
        await self.send(self.code_payload("apply", proposal_id=run_id, accept_context_change=True, reviewed_context_version=self.runtime.material_revision))
        self.assertEqual(self.runtime.code_workspace.code, "answer = 42")
        self.assertTrue(self.runtime.hold_answers)

    async def test_edit_during_generation_makes_result_unapplicable(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def analyze(*args, **kwargs):
            entered.set()
            await release.wait()
            return json.dumps({"code": "old suggestion", "language": "python", "explanation": "Old."})
        with patch("app.services.openai_realtime._analyze_problem", new=analyze):
            run_id, task = await self.send(self.code_payload("generate"), wait=False)
            await entered.wait()
            await self.send(self.code_payload("save", code="my edit", language="python"))
            release.set()
            await task
        self.assertTrue(self.runtime.code_state()["workspace"]["proposal"]["code_changed"])
        failed, _ = await self.send(self.code_payload("apply", proposal_id=run_id, accept_context_change=True))
        self.assertEqual(self.runtime.operations[failed]["status"], "failed")
        self.assertEqual(self.runtime.code_workspace.code, "my edit")

    async def test_stop_and_new_problem_cancel_the_expected_run_only(self):
        for action in ("stop", "reset"):
            entered = asyncio.Event()
            async def analyze(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            with patch("app.services.openai_realtime._analyze_problem", new=analyze):
                old_document = self.runtime.code_workspace.document_id
                run_id, task = await self.send(self.code_payload("generate"), wait=False)
                await entered.wait()
                await self.send(self.code_payload(action, run_id=run_id))
                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(self.runtime.operations[run_id]["status"], "cancelled")
                self.assertEqual(self.runtime.code_workspace.run_id, "")
                self.assertIsNone(self.runtime.code_workspace.proposal)
                if action == "reset":
                    self.assertNotEqual(old_document, self.runtime.code_workspace.document_id)

    async def test_invalid_partial_or_refused_response_never_changes_document(self):
        for result in ('{"code":', '{"code":"incomplete"}', '{"refusal":"no"}', '{"code":12,"language":"python","explanation":"bad"}'):
            with patch("app.services.openai_realtime._analyze_problem", new=AsyncMock(return_value=result)):
                operation_id, _ = await self.send(self.code_payload("generate"))
            self.assertEqual(self.runtime.operations[operation_id]["status"], "failed")
            self.assertEqual(self.runtime.code_workspace.code, "")
            self.assertIsNone(self.runtime.code_workspace.proposal)
            self.assertEqual(self.runtime.code_workspace.run_id, "")


    async def test_context_acknowledgement_cannot_accept_later_unseen_input(self):
        result = json.dumps({"code": "return 1", "language": "python", "explanation": "One change."})
        with patch("app.services.openai_realtime._analyze_problem", new=AsyncMock(return_value=result)):
            run_id, _ = await self.send(self.code_payload("generate"))
        await self.runtime.emit_transcript_final("candidate", "First addition")
        observed_version = self.runtime.material_revision
        await self.runtime.emit_transcript_final("interviewer", "Second addition")
        failed, _ = await self.send(self.code_payload("apply", proposal_id=run_id,
            accept_context_change=True, reviewed_context_version=observed_version))
        self.assertEqual(self.runtime.operations[failed]["status"], "failed")
        self.assertEqual(self.runtime.code_workspace.code, "")

    async def test_close_cancels_independent_run_and_isolated_room_stays_empty(self):
        entered = asyncio.Event()
        async def analyze(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch("app.services.openai_realtime._analyze_problem", new=analyze):
            operation_id, _ = await self.send(self.code_payload("generate"), wait=False)
            await entered.wait()
            await self.runtime.close()
        self.assertEqual(self.runtime.operations[operation_id]["status"], "cancelled")
        other = make_runtime("other")
        self.assertEqual(other.code_workspace.code, "")
        self.assertNotEqual(other.code_workspace.document_id, self.runtime.code_workspace.document_id)

    async def test_reconnect_snapshot_contains_code_proposal_and_materials(self):
        await self.send(self.code_payload("save", code="SELECT 1;", language="sql"))
        self.runtime.history.add_screen("s1", PNG_DATA_URL, "Page one", question_id="q1")
        self.runtime.collected_screens.append("s1")
        reconnect = FakeClientWebSocket({"type": "authenticate", "token": "token-one"})
        await self.runtime.serve(reconnect, "client")
        code = next(item for item in reconnect.messages if item["type"] == "code_state")
        screens = next(item for item in reconnect.messages if item["type"] == "screen_collection")
        self.assertEqual(code["workspace"]["code"], "SELECT 1;")
        self.assertEqual(screens["screens"][0]["request_id"], "s1")

    async def test_multi_page_capture_survives_speech_and_does_not_start_model(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(*args, **kwargs):
            entered.set()
            await release.wait()
            return "s1", PNG_DATA_URL
        with patch("app.services.openai_realtime._request_current_screen", new=capture), patch.object(self.runtime, "ensure_main", new=AsyncMock()) as ensure:
            operation_id, task = await self.send({"type": "request_screen_capture", "collect_only": True}, wait=False)
            await entered.wait()
            await self.runtime.invalidate_work(question_id="spoken-followup")
            release.set()
            await task
            ensure.assert_not_awaited()
        with patch("app.services.openai_realtime._request_current_screen", new=AsyncMock(return_value=("s2", PNG_DATA_URL))):
            await self.send({"type": "request_screen_capture", "collect_only": True})
        self.assertEqual(self.runtime.collected_screens, ["s1", "s2"])
        self.assertEqual(self.runtime.operations[operation_id]["status"], "completed")
        upstream = FakeUpstream()
        from tests.test_realtime import attach_live
        attach_live(self.runtime, upstream)
        self.runtime.hold_answers = True
        await self.send({"type": "answer_screens", "request_ids": ["s1", "s2"]})
        responses = [item for item in upstream.messages if item["type"] == "response.create"]
        self.assertEqual(len(responses), 1)
        screenshot_question = self.runtime.current_question_id
        self.assertIn("2 张", self.runtime.question_text(screenshot_question))
        self.assertTrue(any(question["question_id"] == screenshot_question for question in self.runtime.history.questions()))
        self.assertFalse(any(turn.get("question_id") == screenshot_question for turn in self.runtime.history.turns))
        self.assertTrue(self.runtime.hold_answers)
        self.assertTrue(self.runtime.live.manual_allow_held)
        self.assertNotIn("response.cancel", [item["type"] for item in upstream.messages])
        await self.send({"type": "clear_screens", "request_ids": ["s1"]})
        self.assertEqual(self.runtime.collected_screens, ["s2"])
        self.assertIn("screen:s1", self.runtime.history.by_id)

    async def test_manual_code_request_uses_existing_analysis_contract(self):
        client = FakeHTTPClient()
        self.runtime.history.add_turn("interviewer", "First full question", question_id="first")
        self.runtime.history.add_screen("page", PNG_DATA_URL, "Uncropped page", question_id="first")
        with patch("app.services.openai_realtime.httpx.AsyncClient", return_value=client), patch.dict("os.environ", {"OPENAI_API_KEY": "test-only-not-real"}):
            await _analyze_problem(self.runtime, "Improve this", code_document={"code": "print(1)", "language": "python"})
        request = client.requests[0]["json"]
        self.assertFalse(request["store"])
        self.assertEqual(request["truncation"], "disabled")
        self.assertEqual(request["max_output_tokens"], 8192)
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])
        content = request["input"][0]["content"]
        self.assertIn("First full question", content[0]["text"])
        self.assertIn("print(1)", content[0]["text"])
        self.assertEqual(content[1]["image_url"], PNG_DATA_URL)
