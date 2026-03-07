from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models import AnswerVariant
from tests.scenarios import COMPLEX_DIALOGUE


class InterviewFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["OPENAI_API_KEY"] = ""
        cls.client = TestClient(app)

    @staticmethod
    def _fake_ai_answer(*args, **kwargs) -> AnswerVariant:
        label = kwargs.get("label", "详细回答")
        return AnswerVariant(
            label=label,
            short_answer=f"{label}：先回答结论，再补充原理和取舍。",
            talking_points=["先给结论。", "再讲原理。", "最后补场景或取舍。"],
            source="OpenAI mock",
            ready=True,
        )

    def test_hybrid_mode_returns_ai_unavailable_without_api_key(self) -> None:
        history = [
            {"speaker": item["speaker"], "text": item["text"]}
            for item in COMPLEX_DIALOGUE[:4]
        ]
        payload = {
            "turn": {"speaker": "interviewer", "text": "Why do databases usually use B plus trees instead of hash indexes?"},
            "history": history,
            "context": {
                "name": "Alice",
                "target_role": "Backend Engineer",
                "resume": "Worked on SQL optimization and caching.",
                "job_description": "Strong SQL and Redis expected.",
                "custom_notes": "Prefer concise tradeoff based answers.",
            },
            "generation_mode": "hybrid",
        }

        started = time.perf_counter()
        response = self.client.post("/api/coach/respond", json=payload)
        elapsed = time.perf_counter() - started

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["topic"], "SQL 索引")
        self.assertTrue(data["deep_answer"]["ready"])
        self.assertIsNone(data["detail_job_id"])
        self.assertEqual(data["fast_answer"]["source"], "AI 不可用")
        self.assertLess(elapsed, 0.5)

    def test_detail_job_finishes_for_long_dialogue(self) -> None:
        history = []
        last_job_id = None
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("app.services.openai_service._request_openai_structured_answer", side_effect=self._fake_ai_answer):
                for item in COMPLEX_DIALOGUE:
                    if item["speaker"] == "candidate":
                        history.append(item)
                        continue

                    response = self.client.post(
                        "/api/coach/respond",
                        json={
                            "turn": item,
                            "history": history,
                            "context": {
                                "name": "Alice",
                                "target_role": "Backend Engineer",
                                "resume": "Worked on SQL optimization, Redis reliability, and API performance.",
                                "job_description": "Need strong SQL, Redis, and distributed systems fundamentals.",
                                "custom_notes": "Tie answers back to query optimization and cache stability.",
                            },
                            "generation_mode": "hybrid",
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertIn(data["topic"], ["SQL 索引", "Redis 持久化", "项目经历"])
                    last_job_id = data["detail_job_id"]
                    self.assertEqual(data["fast_answer"]["source"], "OpenAI mock")
                    history.append(item)

                self.assertIsNotNone(last_job_id)

                deadline = time.time() + 5
                detail = None
                while time.time() < deadline:
                    detail_response = self.client.get(f"/api/coach/detail/{last_job_id}")
                    self.assertEqual(detail_response.status_code, 200)
                    detail = detail_response.json()
                    if detail["ready"]:
                        break
                    time.sleep(0.1)

        self.assertIsNotNone(detail)
        self.assertTrue(detail["ready"])
        self.assertIn("answer", detail)
        self.assertTrue(detail["answer"]["short_answer"])
        self.assertGreaterEqual(len(detail["answer"]["talking_points"]), 1)

    def test_detail_stream_emits_progress_and_completion(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("app.services.openai_service._request_openai_structured_answer", side_effect=self._fake_ai_answer):
                response = self.client.post(
                    "/api/coach/respond",
                    json={
                        "turn": {"speaker": "interviewer", "text": "How does Redis persistence work?"},
                        "history": [],
                        "context": {},
                        "generation_mode": "hybrid",
                    },
                )
                self.assertEqual(response.status_code, 200)
                job_id = response.json()["detail_job_id"]
                self.assertTrue(job_id)

                payloads = []
                with self.client.stream("GET", f"/api/coach/detail-stream/{job_id}") as stream_response:
                    self.assertEqual(stream_response.status_code, 200)
                    for line in stream_response.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        payloads.append(line[6:])

        self.assertGreaterEqual(len(payloads), 2)
        self.assertIn('"ready": true', payloads[-1])

    def test_api_only_mode_still_uses_ai_pipeline(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("app.services.openai_service._request_openai_structured_answer", side_effect=self._fake_ai_answer):
                response = self.client.post(
                    "/api/coach/respond",
                    json={
                        "turn": {"speaker": "interviewer", "text": "How does Redis persistence work?"},
                        "history": [],
                        "context": {},
                        "generation_mode": "api_only",
                    },
                )
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertTrue(bool(data["detail_job_id"]))
                self.assertFalse(data["deep_answer"]["ready"])
                self.assertEqual(data["fast_answer"]["source"], "OpenAI mock")
                self.assertEqual(data["topic"], "Redis 持久化")

    def test_transcription_endpoint_uses_openai_stt(self) -> None:
        with patch("app.main.transcribe_audio_chunk", return_value="数据库 索引 测试"), patch(
            "app.main.get_transcription_source",
            return_value="openai-stt:gpt-4o-mini-transcribe",
        ):
            response = self.client.post(
                "/api/transcribe/chunk",
                data={"speaker": "candidate"},
                files={"file": ("chunk.wav", b"fake-wav", "audio/wav")},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["speaker"], "candidate")
        self.assertEqual(data["text"], "数据库 索引 测试")
        self.assertEqual(data["source"], "openai-stt:gpt-4o-mini-transcribe")


if __name__ == "__main__":
    unittest.main()
