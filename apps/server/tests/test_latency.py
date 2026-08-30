from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.services.context_store import ContextStore


class LatencyTests(unittest.TestCase):
    def test_context_store_searches_an_immutable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context_file = root / "project.md"
            context_file.write_text("Canvas assignment tracker forecast", encoding="utf-8")
            store = ContextStore(root)
            context_file.write_text("changed after session creation", encoding="utf-8")

            started = time.perf_counter()
            for _ in range(500):
                result = store.search("Canvas assignment tracker")
            elapsed = time.perf_counter() - started

        self.assertEqual(result[0].source, "project.md")
        self.assertIn("forecast", result[0].text)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
