from __future__ import annotations

import os
import time
import unittest

from app.models import CandidateContext, CoachRequest, TranscriptTurn
from app.services.interview_coach import build_coaching_plan


class LatencyTests(unittest.TestCase):
    def test_fast_path_is_lightweight(self) -> None:
        os.environ.pop("OPENAI_API_KEY", None)
        request = CoachRequest(
            turn=TranscriptTurn(speaker="interviewer", text="Why do databases usually use B plus trees instead of hash indexes?"),
            history=[
                TranscriptTurn(speaker="candidate", text="I would explain the B plus tree structure first."),
                TranscriptTurn(speaker="interviewer", text="How are SQL indexes implemented?"),
            ],
            context=CandidateContext(
                name="Alice",
                target_role="Backend Engineer",
                resume="Worked on SQL optimization and caching.",
                job_description="Need strong SQL and Redis.",
                custom_notes="Use concise answers.",
            ),
            generation_mode="hybrid",
        )

        started = time.perf_counter()
        for _ in range(200):
            plan = build_coaching_plan(request)
        elapsed = time.perf_counter() - started

        self.assertEqual(plan.topic, "SQL 索引")
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
