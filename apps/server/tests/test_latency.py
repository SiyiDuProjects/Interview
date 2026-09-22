from __future__ import annotations

import tempfile
import traceback
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.services.context_store import ContextDocument, ContextStore


class ContextSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        settings_patch = patch("app.services.context_store.get_settings", return_value=Settings())
        self.settings = settings_patch.start()
        self.addCleanup(settings_patch.stop)

    def test_documents_preserve_complete_sections_and_file_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context_file = root / "project.md"
            complete_text = (
                "\r\n# Project Atlas\r\n\r\nThe candidate owns the logging module.\r\n\r\n"
                + "Supporting project detail.\r\n" * 500
                + "\r\n## Final constraint\r\n- The candidate did not design the database.\r\n\r\n"
            )
            context_file.write_bytes(complete_text.encode("utf-8"))
            documents = ContextStore(root).documents()

        self.assertEqual(documents, [ContextDocument(source="project.md", text=complete_text)])
        self.assertEqual(documents[0].as_dict(), {"source": "project.md", "text": complete_text})

    def test_documents_include_all_supported_files_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            (root / "nested" / "c-notes.TXT").write_text("Last document.", encoding="utf-8")
            (root / "b-job.txt").write_text("Complete job context.", encoding="utf-8")
            (root / "a-profile.md").write_text("# Profile\n\nEducation and experience.", encoding="utf-8")
            (root / "d-empty.md").write_bytes(b"\r\n \r\n")
            (root / "ignored.json").write_bytes(b"\xffunsupported invalid UTF-8")
            (root / "ignored.pdf").write_bytes(b"synthetic unsupported content")
            first = ContextStore(root).documents()
            second = ContextStore(root).documents()

        self.assertEqual(
            [document.source for document in first],
            ["a-profile.md", "b-job.txt", "d-empty.md", "nested/c-notes.TXT"],
        )
        self.assertEqual(first, second)
        self.assertEqual(first[2].text, "\r\n \r\n")
        self.assertEqual(first[3].text, "Last document.")

    def test_documents_are_isolated_from_file_and_caller_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context_file = root / "project.md"
            context_file.write_text("Original complete project context.", encoding="utf-8")
            store = ContextStore(root)
            context_file.write_text("changed after session creation", encoding="utf-8")
            (root / "new.txt").write_text("Created after session creation.", encoding="utf-8")

            returned = store.documents()
            with self.assertRaises(FrozenInstanceError):
                returned[0].text = "caller replacement"  # type: ignore[misc]
            exported = returned[0].as_dict()
            exported["text"] = "caller replacement"
            returned.clear()

            original_snapshot = store.documents()
            new_snapshot = ContextStore(root).documents()

        self.assertEqual(
            original_snapshot,
            [ContextDocument(source="project.md", text="Original complete project context.")],
        )
        self.assertEqual([document.source for document in new_snapshot], ["new.txt", "project.md"])
        self.assertEqual(new_snapshot[1].text, "changed after session creation")

    def test_configured_root_is_used_and_explicit_root_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured = root / "configured"
            explicit = root / "explicit"
            configured.mkdir()
            explicit.mkdir()
            (configured / "configured.md").write_text("Configured context.", encoding="utf-8")
            (explicit / "explicit.md").write_text("Explicit context.", encoding="utf-8")
            self.settings.return_value = Settings(interview_context_dir=str(configured))

            configured_documents = ContextStore().documents()
            explicit_documents = ContextStore(explicit).documents()

        self.assertEqual([document.source for document in configured_documents], ["configured.md"])
        self.assertEqual([document.source for document in explicit_documents], ["explicit.md"])

    def test_unreadable_supported_document_fails_without_private_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            blocked_file = root / "nested" / "blocked.md"
            blocked_file.write_text("Synthetic private body.", encoding="utf-8")
            underlying_error = PermissionError(f"Denied {blocked_file}: Synthetic private body.")

            with patch("pathlib.Path.open", side_effect=underlying_error):
                with self.assertRaises(RuntimeError) as caught:
                    ContextStore(root)

        self.assertEqual(str(caught.exception), "Context document could not be read: nested/blocked.md")
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(str(root), rendered)
        self.assertNotIn("Synthetic private body", rendered)
        self.assertNotIn("PermissionError", rendered)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_utf8_supported_document_fails_without_exposing_decoder_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "broken.txt").write_bytes(b"\xffSynthetic private byte payload.")

            with self.assertRaises(RuntimeError) as caught:
                ContextStore(root)

        self.assertEqual(str(caught.exception), "Context document could not be read: broken.txt")
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(str(root), rendered)
        self.assertNotIn("Synthetic private byte payload", rendered)
        self.assertNotIn("0xff", rendered)
        self.assertNotIn("UnicodeDecodeError", rendered)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_and_empty_directories_have_empty_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(ContextStore(root).documents(), [])
            self.assertEqual(ContextStore(root / "not-created").documents(), [])


if __name__ == "__main__":
    unittest.main()
