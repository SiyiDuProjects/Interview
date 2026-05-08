from __future__ import annotations

import time
import unittest

from app.models import CandidateContext
from app.services.realtime_context import lookup_candidate_context


class LatencyTests(unittest.TestCase):
    def test_context_lookup_is_lightweight(self) -> None:
        started = time.perf_counter()
        result = ""
        for _ in range(200):
            result = lookup_candidate_context(
                "Canvas assignment tracker forecast",
                "general",
                CandidateContext(resume="Worked on course tools."),
            )
        elapsed = time.perf_counter() - started

        self.assertIn("Canvas", result)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
