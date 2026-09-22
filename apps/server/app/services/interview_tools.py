"""Application tools, independent of Live event routing and provider lifecycle.

Add a tool definition and handler here; the transport advertises and dispatches
the same table. Keep feature state in the interview runtime, never in this table.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.services.code_workspace import CodeWorkspaceError, record_code_change, validate_document


@dataclass(frozen=True)
class ToolContext:
    runtime: Any
    socket: Any
    task: dict[str, Any]
    response_id: str
    call_id: str
    current: Callable[[dict[str, Any]], bool]
    active: Callable[[dict[str, Any]], bool]
    workspace: Callable[[], dict[str, Any]]

    def require_current(self) -> None:
        if not self.current(self.task):
            raise CodeWorkspaceError("Input changed or task identity is unknown; discard this obsolete action.")


@dataclass(frozen=True)
class InterviewTool:
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]
    opens_code: bool = False
    refreshes_context: bool = False

    def schema(self, name: str) -> dict[str, Any]:
        return {"type": "function", "name": name, "strict": True,
                "description": self.description, "parameters": deepcopy(self.parameters)}


async def search_context(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if args:
        raise CodeWorkspaceError("search_context takes no arguments.")
    # A reference read may refresh changing speech, but cancellation, another
    # question, a reset, and manual edits still invalidate the entire task.
    ctx.task.update(ctx.workspace())
    ctx.task["read"] = True
    return {"ok": True, "documents": [doc.as_dict() for doc in ctx.runtime.context_store.documents()],
            "workspace": ctx.workspace(), "transcripts": ctx.runtime.history.transcript_snapshot()}


async def capture_current_screen(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if args:
        raise CodeWorkspaceError("capture_current_screen takes no arguments.")
    from app.services.openai_realtime import _request_current_screen, _record_screen

    request_id, image = await _request_current_screen(
        ctx.runtime, reason="Read the current interviewer problem or code.")
    ctx.require_current()
    expected_material = ctx.runtime.material_revision + 1
    await _record_screen(ctx.runtime, ctx.socket, request_id, image, ctx.task["question_id"])
    ctx.task["context_version"] = expected_material
    ctx.require_current()
    return {"ok": True, "request_id": request_id}


async def update_code(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rt, task = ctx.runtime, ctx.task
    doc = rt.code_workspace
    if doc.run_id and doc.run_id != f"live-code:{ctx.response_id}":
        raise CodeWorkspaceError("Another code operation is already running.")
    if not task["read"]:
        raise CodeWorkspaceError("Read search_context before editing code.")
    doc.check_base(args)
    if type(args.get("context_version")) is not int or args["context_version"] != rt.material_revision:
        raise CodeWorkspaceError("New speech or screenshots superseded this code change.")
    if doc.document_id != task["document_id"] or doc.revision != task["revision"]:
        raise CodeWorkspaceError("The document changed during this task.")
    validate_document(args.get("code"), args.get("language"))
    explanation = args.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise CodeWorkspaceError("A change explanation is required.")
    before = doc.code
    # No await between version checks and commit. New asynchronous handlers
    # must call require_current again after waiting and before side effects.
    changed = doc.commit(args["code"], args["language"])
    doc.reveal_id = f"live-code:{ctx.response_id}"
    doc.last_change = {"base_code": before, "code": doc.code, "language": doc.language,
                       "explanation": explanation, "revision": doc.revision}
    doc.proposal = None
    if changed:
        record_code_change(rt, "automatic", question_id=task["question_id"],
                           call_id=ctx.call_id, explanation=explanation)
    task["revision"] = doc.revision
    await rt.broadcast_to_clients(rt.code_state())
    return {"ok": True, "status": "updated" if changed else "unchanged",
            "revision": doc.revision, "explanation": explanation,
            "meaning": "Internal code pane updated; not externally typed or executed."}


# Ordinary, statically defined application functions, not a runtime plugin API.
# Provider configuration and dispatch must both use this single definition table.
TOOLS = {
    "search_context": InterviewTool(
        "Read complete saved background, observed transcripts and exact current task/code state, without filtering.",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        search_context, refreshes_context=True,
    ),
    "capture_current_screen": InterviewTool(
        "Capture the selected screen and add the image to this backend conversation.",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        capture_current_screen,
    ),
    "update_code": InterviewTool(
        "Commit code or SQL to the single code pane, using the versions from search_context. Never changes an external editor.",
        {"type": "object", "additionalProperties": False,
         "properties": {"document_id": {"type": "string"}, "base_revision": {"type": "integer"},
                        "context_version": {"type": "integer"}, "code": {"type": "string"},
                        "language": {"type": "string"}, "explanation": {"type": "string"}},
         "required": ["document_id", "base_revision", "context_version", "code", "language", "explanation"]},
        update_code, opens_code=True,
    ),
}


def tool_schema() -> list[dict[str, Any]]:
    return [tool.schema(name) for name, tool in TOOLS.items()]


def opens_code(name: str) -> bool:
    tool = TOOLS.get(name)
    return bool(tool and tool.opens_code)


async def execute_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if tool is None:
        raise CodeWorkspaceError("Unknown backend tool.")
    if tool.refreshes_context:
        if not ctx.active(ctx.task):
            raise CodeWorkspaceError("This task was cancelled or its document changed.")
    else:
        ctx.require_current()
    return await tool.handler(ctx, args)
