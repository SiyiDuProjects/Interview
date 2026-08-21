from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
SUPPORTED_CONTEXT_SUFFIXES = {".md", ".txt"}


@dataclass(frozen=True)
class ContextMatch:
    source: str
    text: str
    score: int

    def as_dict(self) -> dict[str, str | int]:
        return {"source": self.source, "text": self.text, "score": self.score}


class ContextStore:
    """Small filesystem search over interview-owned markdown and text files."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured_root = get_settings().interview_context_dir
        self.root = Path(root or configured_root or _default_context_root()).expanduser().resolve()
        self._snapshot = self._load_documents()

    def search(
        self,
        query: str,
        *,
        limit: int = 6,
        max_chars: int = 6000,
    ) -> list[ContextMatch]:
        keywords = _keywords(query)
        matches: list[ContextMatch] = []
        for source, text in self._snapshot:
            for paragraph in _paragraphs(text):
                score = _score(paragraph, keywords)
                if score > 0:
                    matches.append(ContextMatch(source=source, text=_clip(paragraph, 1400), score=score))

        matches.sort(key=lambda item: (-item.score, item.source, item.text))
        selected: list[ContextMatch] = []
        used_chars = 0
        for match in matches:
            if len(selected) >= max(1, limit):
                break
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            text = _clip(match.text, remaining)
            if not text:
                break
            selected.append(ContextMatch(source=match.source, text=text, score=match.score))
            used_chars += len(text)
        return selected

    def _load_documents(self) -> tuple[tuple[str, str], ...]:
        documents: list[tuple[str, str]] = []
        if not self.root.is_dir():
            return ()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_CONTEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if text:
                documents.append((path.relative_to(self.root).as_posix(), text))
        return tuple(documents)


def _default_context_root() -> Path:
    return Path(__file__).resolve().parents[2] / "context"


def _keywords(query: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z0-9_+#.-]{2,}", query.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        words.add(chunk)
        words.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return words


def _paragraphs(text: str) -> list[str]:
    chunks = [chunk.strip(" \t\r\n-#") for chunk in re.split(r"\n\s*\n|\n(?=#)|\n(?=- )", text)]
    return [chunk for chunk in chunks if chunk]


def _score(text: str, keywords: set[str]) -> int:
    normalized = text.lower()
    return sum(1 for keyword in keywords if keyword in normalized)


def _clip(text: str, limit: int) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(0, limit - 1)]}…"
