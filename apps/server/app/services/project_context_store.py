from __future__ import annotations

from pathlib import Path

from app.models import AnswerScope


PROJECT_CONTEXT_FILES: dict[AnswerScope, tuple[str, str]] = {
    "innovation_ai": ("Innovation AI", "innovation-ai.md"),
    "canvasbot": ("AI Canvas Tracker", "canvasbot.md"),
    "discordbot": ("UC Berkeley Course Knowledge & Enrollment Platform", "discordbot.md"),
    "general": ("", ""),
}


def resolve_project_context(answer_scope: AnswerScope) -> tuple[str, str]:
    label, filename = PROJECT_CONTEXT_FILES.get(answer_scope, ("", ""))
    if not filename:
        return "", ""

    path = _project_context_root() / filename
    if not path.exists():
        return label, ""

    return label, path.read_text(encoding="utf-8").strip()


def _project_context_root() -> Path:
    return Path(__file__).resolve().parents[4] / "docs" / "project-contexts"
