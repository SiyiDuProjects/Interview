from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

SUPPORTED_CONTEXT_SUFFIXES = {".md", ".txt"}


@dataclass(frozen=True)
class ContextDocument:
    source: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "text": self.text}


class ContextStore:
    """Complete, immutable snapshot of interview-owned markdown and text files."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured_root = get_settings().interview_context_dir
        self.root = Path(root or configured_root or _default_context_root()).expanduser().resolve()
        self._snapshot = self._load_documents()

    def documents(self) -> list[ContextDocument]:
        return list(self._snapshot)

    def _load_documents(self) -> tuple[ContextDocument, ...]:
        documents: list[ContextDocument] = []
        if not self.root.is_dir():
            return ()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_CONTEXT_SUFFIXES:
                continue
            source = path.relative_to(self.root).as_posix()
            try:
                with path.open("r", encoding="utf-8", newline="") as document_file:
                    text = document_file.read()
            except (OSError, UnicodeError):
                raise RuntimeError(f"Context document could not be read: {source}") from None
            documents.append(ContextDocument(source=source, text=text))
        return tuple(documents)


def _default_context_root() -> Path:
    return Path(__file__).resolve().parents[2] / "context"
