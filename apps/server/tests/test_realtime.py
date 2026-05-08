from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.models import CandidateContext
from app.services.openai_realtime import (
    _extract_response_text,
    _forward_candidate_events,
    _forward_interviewer_transcription_events,
    _forward_main_events,
    _handle_tool_call,
    _response_status_detail,
    _send_session_update,
    _send_transcription_session_update,
)
from app.services.realtime_context import RealtimeContextConfig, lookup_candidate_context


class FakeUpstream:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


class FakeEventStream:
    def __init__(self, events: list[dict]) -> None:
        self.events = [json.dumps(event) for event in events]

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


class RealtimeTests(unittest.TestCase):
    def test_session_update_contains_text_only_tools_and_instructions(self) -> None:
        upstream = FakeUpstream()
        config = RealtimeContextConfig(
            context=CandidateContext(
                target_role="Backend Engineer",
                resume="Built Redis and SQL optimization projects.",
                custom_notes="Prefer concise answers.",
            ),
            answer_scope="canvasbot",
            project_context_label="CanvasBot",
        )

        asyncio.run(
            _send_session_update(
                upstream,
                config=config,
                create_response=True,
                transcription=False,
            )
        )

        message = upstream.messages[0]
        session = message["session"]
        self.assertEqual(message["type"], "session.update")
        self.assertEqual(session["type"], "realtime")
        self.assertEqual(session["model"], "gpt-realtime-2")
        self.assertEqual(session["output_modalities"], ["text"])
        self.assertIn("Backend Engineer", session["instructions"])
        self.assertEqual([tool["name"] for tool in session["tools"]], ["lookup_candidate_context", "capture_current_screen", "solve_code_question"])
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(session["audio"]["input"]["turn_detection"]["type"], "semantic_vad")
        self.assertTrue(session["audio"]["input"]["turn_detection"]["create_response"])
        self.assertEqual(session["reasoning"]["effort"], "low")

    def test_realtime_transcription_session_uses_whisper_model(self) -> None:
        upstream = FakeUpstream()

        with patch.dict(
            "os.environ",
            {
                "OPENAI_REALTIME_TRANSCRIPTION_MODEL": "gpt-realtime-whisper",
                "TRANSCRIPTION_LANGUAGE": "zh",
            },
        ):
            asyncio.run(_send_transcription_session_update(upstream))

        self.assertEqual(upstream.messages[0]["type"], "session.update")
        session = upstream.messages[0]["session"]
        transcription = session["audio"]["input"]["transcription"]
        self.assertEqual(session["type"], "transcription")
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertNotIn("turn_detection", session["audio"]["input"])
        self.assertEqual(transcription["model"], "gpt-realtime-whisper")
        self.assertEqual(transcription["language"], "zh")

    def test_lookup_candidate_context_returns_scope_project(self) -> None:
        result = lookup_candidate_context(
            "Canvas assignment tracker forecast",
            "canvasbot",
            CandidateContext(resume="Worked on course tools."),
        )

        self.assertIn("Canvas", result)

    def test_lookup_candidate_context_general_searches_all_projects(self) -> None:
        result = lookup_candidate_context(
            "Canvas assignment tracker forecast",
            "general",
            CandidateContext(resume="Worked on course tools."),
        )

        self.assertIn("Canvas", result)

    def test_tool_call_outputs_context_item_and_response_create(self) -> None:
        upstream = FakeUpstream()
        websocket = FakeWebSocket()
        payload = {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "lookup_candidate_context",
            "arguments": json.dumps({"query": "Redis persistence", "scope": "general"}),
        }

        with patch("app.services.openai_realtime._hub.current_context", return_value=(CandidateContext(resume="Redis AOF and RDB work."), "general")):
            asyncio.run(_handle_tool_call(websocket, upstream, payload, {}))

        self.assertEqual(upstream.messages[0]["type"], "conversation.item.create")
        self.assertEqual(upstream.messages[0]["item"]["type"], "function_call_output")
        self.assertIn("Redis", upstream.messages[0]["item"]["output"])
        self.assertEqual(upstream.messages[1]["type"], "response.create")
        self.assertEqual(upstream.messages[1]["response"], {"output_modalities": ["text"]})
        self.assertEqual(websocket.messages[0]["type"], "answer_status")

    def test_screen_capture_tool_requests_frontend_snapshot(self) -> None:
        async def run() -> tuple[FakeWebSocket, FakeUpstream]:
            websocket = FakeWebSocket()
            upstream = FakeUpstream()
            pending: dict[str, asyncio.Future[str]] = {}
            task = asyncio.create_task(
                _handle_tool_call(
                    websocket,
                    upstream,
                    {
                        "type": "response.function_call_arguments.done",
                        "call_id": "call_screen",
                        "name": "capture_current_screen",
                        "arguments": json.dumps({"reason": "看刚刚写的代码"}),
                    },
                    pending,
                )
            )
            await asyncio.sleep(0)
            request_id = websocket.messages[1]["request_id"]
            pending[request_id].set_result("data:image/jpeg;base64,abc")
            await task
            return websocket, upstream

        websocket, upstream = asyncio.run(run())

        self.assertEqual(websocket.messages[1]["type"], "screen_capture_request")
        self.assertEqual(upstream.messages[0]["type"], "conversation.item.create")
        self.assertEqual(upstream.messages[0]["item"]["content"][1]["type"], "input_image")
        self.assertEqual(upstream.messages[1]["item"]["type"], "function_call_output")
        self.assertEqual(upstream.messages[2]["type"], "response.create")

    def test_code_tool_outputs_deep_answer_and_response_create(self) -> None:
        upstream = FakeUpstream()
        websocket = FakeWebSocket()
        payload = {
            "type": "response.function_call_arguments.done",
            "call_id": "call_code",
            "name": "solve_code_question",
            "arguments": json.dumps({"question": "实现 LRU Cache", "problem_type": "algorithm"}),
        }

        with patch("app.services.openai_realtime._solve_code_question", new=AsyncMock(return_value="用哈希表加双向链表实现。")):
            asyncio.run(_handle_tool_call(websocket, upstream, payload, {}))

        self.assertEqual(websocket.messages[0]["type"], "answer_status")
        self.assertEqual(upstream.messages[0]["item"]["type"], "function_call_output")
        self.assertIn("双向链表", upstream.messages[0]["item"]["output"])
        self.assertEqual(upstream.messages[1]["type"], "response.create")

    def test_response_status_detail_surfaces_incomplete_reason(self) -> None:
        detail = _response_status_detail({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})

        self.assertEqual(detail, "incomplete: max_output_tokens")

    def test_extract_response_text_from_done_payload(self) -> None:
        text = _extract_response_text(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "text", "text": "SQL 索引可以先讲 B+ 树。"},
                        ],
                    }
                ]
            }
        )

        self.assertIn("SQL 索引", text)

    def test_forward_main_events_sends_output_text_done(self) -> None:
        websocket = FakeWebSocket()
        upstream = FakeEventStream(
            [
                {
                    "type": "response.output_text.done",
                    "text": "SQL 索引常见实现是 B+ 树。",
                }
            ]
        )

        asyncio.run(_forward_main_events(websocket, upstream))  # type: ignore[arg-type]

        self.assertEqual(websocket.messages, [{"type": "answer_done", "text": "SQL 索引常见实现是 B+ 树。"}])

    def test_interviewer_transcription_final_only_emits_transcript(self) -> None:
        websocket = FakeWebSocket()
        transcription_upstream = FakeEventStream(
            [
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "delta": "数据库",
                },
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "数据库为什么用 B+ 树？",
                },
            ]
        )

        asyncio.run(_forward_interviewer_transcription_events(websocket, transcription_upstream))  # type: ignore[arg-type]

        self.assertEqual(websocket.messages[0], {"type": "transcript", "speaker": "interviewer", "text": "数据库", "is_final": False})
        self.assertEqual(
            websocket.messages[1],
            {
                "type": "transcript",
                "speaker": "interviewer",
                "text": "数据库为什么用 B+ 树？",
                "is_final": True,
                "speech_final": True,
            },
        )

    def test_candidate_transcription_final_updates_context(self) -> None:
        websocket = FakeWebSocket()
        upstream = FakeEventStream(
            [
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "我会先讲结论。",
                },
            ]
        )

        with patch("app.services.openai_realtime._hub.append_context", new=AsyncMock()) as append_context:
            asyncio.run(_forward_candidate_events(websocket, upstream))  # type: ignore[arg-type]

        append_context.assert_awaited_once()
        self.assertEqual(
            websocket.messages,
            [{"type": "transcript", "speaker": "candidate", "text": "我会先讲结论。", "is_final": True, "speech_final": True}],
        )

    def test_forward_main_events_sends_realtime_text_delta_and_done(self) -> None:
        websocket = FakeWebSocket()
        upstream = FakeEventStream(
            [
                {
                    "type": "response.text.delta",
                    "delta": "SQL 索引",
                },
                {
                    "type": "response.text.done",
                    "text": "SQL 索引通常基于 B+ 树。",
                },
            ]
        )

        asyncio.run(_forward_main_events(websocket, upstream))  # type: ignore[arg-type]

        self.assertEqual(
            websocket.messages,
            [
                {"type": "answer_delta", "delta": "SQL 索引"},
                {"type": "answer_done", "text": "SQL 索引通常基于 B+ 树。"},
            ],
        )

    def test_forward_main_events_sends_content_part_done_text(self) -> None:
        websocket = FakeWebSocket()
        upstream = FakeEventStream(
            [
                {
                    "type": "response.content_part.done",
                    "part": {"type": "text", "text": "可以从数据结构、查询效率和代价讲。"},
                }
            ]
        )

        asyncio.run(_forward_main_events(websocket, upstream))  # type: ignore[arg-type]

        self.assertEqual(websocket.messages, [{"type": "answer_done", "text": "可以从数据结构、查询效率和代价讲。"}])

    def test_forward_main_events_sends_output_item_done_text(self) -> None:
        websocket = FakeWebSocket()
        upstream = FakeEventStream(
            [
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "索引能减少扫描行数，但会增加写入维护成本。"}],
                    },
                }
            ]
        )

        asyncio.run(_forward_main_events(websocket, upstream))  # type: ignore[arg-type]

        self.assertEqual(websocket.messages, [{"type": "answer_done", "text": "索引能减少扫描行数，但会增加写入维护成本。"}])

    def test_forward_main_events_extracts_text_from_response_done(self) -> None:
        websocket = FakeWebSocket()
        upstream = FakeEventStream(
            [
                {
                    "type": "response.done",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "面试里我会先解释 B+ 树，再讲最左前缀和回表。"}],
                            }
                        ],
                    },
                }
            ]
        )

        asyncio.run(_forward_main_events(websocket, upstream))  # type: ignore[arg-type]

        self.assertEqual(websocket.messages[0], {"type": "answer_done", "text": "面试里我会先解释 B+ 树，再讲最左前缀和回表。"})
        self.assertEqual(websocket.messages[1], {"type": "response_done", "detail": "completed"})

    def test_screenshot_shape_can_be_forwarded(self) -> None:
        from app.services.openai_realtime import _send_image_item

        upstream = FakeUpstream()
        asyncio.run(
            _send_image_item(
                upstream,
                image_url="data:image/png;base64,abc",
                prompt="请看截图",
                create_response=True,
            )
        )

        content = upstream.messages[0]["item"]["content"]
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["image_url"], "data:image/png;base64,abc")
        self.assertEqual(upstream.messages[1]["type"], "response.create")


if __name__ == "__main__":
    unittest.main()
