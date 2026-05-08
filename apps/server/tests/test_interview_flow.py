from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


class InterviewApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_health_reports_realtime_models(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["realtime_model"], "gpt-realtime-2")
        self.assertEqual(data["realtime_transcription_model"], "gpt-realtime-whisper")
        self.assertEqual(data["code_model"], "gpt-5.5")

    def test_context_preview_still_summarizes_candidate_context(self) -> None:
        response = self.client.post(
            "/api/context/preview",
            json={
                "name": "Alice",
                "target_role": "Backend Engineer",
                "resume": "Built SQL optimization services.",
                "job_description": "",
                "custom_notes": "Keep answers concise.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["summary"],
            "候选人: Alice | 目标岗位: Backend Engineer | 已加载简历 | 已加载补充笔记",
        )

    def test_removed_legacy_coach_and_transcription_routes_return_404(self) -> None:
        coach_response = self.client.post(
            "/api/" + "coach" + "/" + "respond",
            json={"turn": {"speaker": "interviewer", "text": "Redis persistence?"}},
        )
        transcribe_response = self.client.post(
            "/api/" + "transcribe/chunk",
            data={"speaker": "candidate"},
            files={"file": ("chunk.wav", b"fake-wav", "audio/wav")},
        )

        self.assertEqual(coach_response.status_code, 404)
        self.assertEqual(transcribe_response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
