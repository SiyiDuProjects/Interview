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

    return _read_project_context(label, filename)


def resolve_all_project_contexts() -> list[tuple[str, str]]:
    contexts: list[tuple[str, str]] = []
    for scope, (label, filename) in PROJECT_CONTEXT_FILES.items():
        if scope == "general" or not filename:
            continue
        resolved_label, text = _read_project_context(label, filename)
        if text:
            contexts.append((resolved_label, text))
    return contexts


def _read_project_context(label: str, filename: str) -> tuple[str, str]:
    path = _project_context_root() / filename
    if not path.exists():
        return label, ""

    return label, path.read_text(encoding="utf-8").strip()


def _project_context_root() -> Path:
    return Path(__file__).resolve().parents[4] / "docs" / "project-contexts"
