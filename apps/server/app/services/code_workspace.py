from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from app.services.realtime_history import observed_at


# A transport limit, not a line limit or a context truncation policy.
MAX_CODE_CHARS = 80_000


class CodeWorkspaceError(ValueError):
    """A safe, user-facing validation error without provider or code contents."""


CODE_FORMAT = {
    "type": "json_schema", "name": "code_proposal", "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": "string"} for key in ("code", "language", "explanation")},
        "required": ["code", "language", "explanation"],
    },
}
CODE_INSTRUCTIONS = (
    "Work on the single current code document in this interview. Return the complete proposed code, "
    "its language, and a short bilingual explanation of exactly what changes and why. "
    "The code field must contain raw source without Markdown fences. "
    "For the first version prefer a simple correct approach and disclose its limitations. "
    "For each next step make one meaningful logical improvement or requested correction, preserving "
    "unaffected code and names. Do not plant bugs or fabricate a debugging history. "
    "If the request only needs an explanation, information is missing, or no change is needed, return "
    "the current code unchanged and explain. Do not claim execution or test results without evidence. "
    "The current document is authoritative; older code and assistant suggestions in history are references. "
    "This is a proposal, not evidence that the candidate typed, spoke, or adopted it externally."
)


class CodeWorkspace:
    """One server-owned document; synchronous mutations are atomic on the event loop."""

    def __init__(self) -> None:
        self.document_id = str(uuid.uuid4())
        self.revision = 0
        self.code = ""
        self.language = "python"
        self.undo_stack: list[tuple[str, str]] = []
        self.proposal: dict[str, Any] | None = None
        self.run_id = ""
        self.reveal_id = ""
        self.last_change: dict[str, Any] | None = None

    def snapshot(self, context_version: int) -> dict[str, Any]:
        proposal = dict(self.proposal) if self.proposal else None
        if proposal:
            proposal["context_changed"] = proposal["context_version"] != context_version
            proposal["code_changed"] = proposal["base_revision"] != self.revision
        return {
            "document_id": self.document_id, "revision": self.revision,
            "code": self.code, "language": self.language, "can_undo": bool(self.undo_stack),
            "proposal": proposal, "run_id": self.run_id, "context_version": context_version,
            "reveal_id": self.reveal_id, "last_change": self.last_change,
        }

    def check_base(self, payload: dict[str, Any]) -> None:
        if (payload.get("document_id") != self.document_id
                or type(payload.get("base_revision")) is not int
                or payload["base_revision"] != self.revision):
            raise CodeWorkspaceError("代码已在其他操作中更新。请核对最新版本，你的草稿仍保留。")

    def commit(self, code: str, language: str) -> bool:
        if code == self.code and language == self.language:
            return False
        self.undo_stack.append((self.code, self.language))
        self.code, self.language = code, language
        self.revision += 1
        self.last_change = None
        return True


def validate_document(code: Any, language: Any) -> None:
    if not isinstance(code, str) or len(code) > MAX_CODE_CHARS:
        raise CodeWorkspaceError("代码内容无效或超过单次传输上限。")
    if not isinstance(language, str) or not language.strip() or len(language) > 64:
        raise CodeWorkspaceError("请填写有效的代码语言。")


def parse_code_result(result: str) -> dict[str, str]:
    try:
        proposal = json.loads(result)
    except (ValueError, TypeError) as exc:
        raise CodeWorkspaceError("代码建议格式无效，请重试。") from exc
    if not isinstance(proposal, dict) or set(proposal) != {"code", "language", "explanation"}:
        raise CodeWorkspaceError("代码建议格式无效，请重试。")
    validate_document(proposal["code"], proposal["language"])
    if not isinstance(proposal["explanation"], str) or not proposal["explanation"].strip():
        raise CodeWorkspaceError("代码建议缺少修改说明。")
    return proposal


def record_code_change(runtime: Any, action: str, **details: Any) -> dict[str, Any]:
    doc = runtime.code_workspace
    record = {"kind": "code_document", "action": action, "document_id": doc.document_id,
              "revision": doc.revision, "code": doc.code, "language": doc.language,
              "created_at": observed_at(),
              "meaning": "Current internal code document; not evidence of external typing or speech.",
              **details}
    runtime.history.entries.append(record)
    return record


def cancel_code_run(runtime: Any) -> None:
    run_id = runtime.code_workspace.run_id
    if run_id.startswith("live-code:") and runtime.live:
        runtime.live.cancel_code(run_id)
        task = None
    else:
        task = runtime._jobs.get(run_id)
    runtime.code_workspace.run_id = ""
    if task and task is not asyncio.current_task():
        task.cancel()


async def run_code_operation(runtime: Any, payload: dict[str, Any], operation_id: str) -> None:
    from app.services.openai_realtime import _analyze_problem, _send_user_text

    doc = runtime.code_workspace
    action = payload.get("action")
    doc.check_base(payload)
    if action == "stop":
        # Stop only the run that this client actually saw, never a later run.
        if payload.get("run_id") != doc.run_id:
            raise CodeWorkspaceError("分析任务已改变，请刷新状态后重试。")
        cancel_code_run(runtime)
        await runtime.broadcast_to_clients(runtime.code_state())
        await runtime.operation_status(operation_id, "completed", detail="代码分析已停止。")
        return

    if action == "generate":
        if doc.run_id:
            raise CodeWorkspaceError("已有代码分析正在进行，可先停止。")
        instruction = payload.get("instruction", "")
        if not isinstance(instruction, str) or len(instruction) > 12_000:
            raise CodeWorkspaceError("修改要求过长或无效。")
        document_id, base_revision = doc.document_id, doc.revision
        context_version = runtime.material_revision
        current = {"document_id": document_id, "revision": base_revision,
                   "code": doc.code, "language": doc.language, "question_id": runtime.current_question_id}
        doc.run_id = operation_id
        doc.proposal = None
        await runtime.broadcast_to_clients(runtime.code_state())
        await runtime.operation_status(operation_id, "running", detail="正在分析当前代码；可以继续说话或编辑。")
        runtime.metrics["tool_calls"] += 1
        try:
            result = await _analyze_problem(
                runtime, instruction.strip() or "Take the next useful step for the current coding problem.",
                code_document=current,
            )
            proposal = parse_code_result(result)
            if runtime.closed or not runtime.active or doc.run_id != operation_id or doc.document_id != document_id:
                raise asyncio.CancelledError()
            doc.proposal = {
                **proposal, "proposal_id": operation_id, "base_revision": base_revision,
                "base_code": current["code"], "context_version": context_version,
            }
            runtime.history.add_analysis(operation_id, current["question_id"], instruction,
                "[Unapplied code proposal; context may have changed; not candidate speech.]\n" + result)
            await runtime.operation_status(operation_id, "completed", detail="修改建议已就绪，请核对后采用。")
        except Exception:
            runtime.metrics["tool_failures"] += 1
            raise
        finally:
            if doc.run_id == operation_id:
                doc.run_id = ""
            await runtime.broadcast_to_clients(runtime.code_state())
        return

    changed = False
    if action == "save":
        validate_document(payload.get("code"), payload.get("language"))
        changed = doc.commit(payload["code"], payload["language"].strip())
    elif action == "apply":
        proposal = doc.proposal
        if not proposal or payload.get("proposal_id") != proposal["proposal_id"]:
            raise CodeWorkspaceError("这份修改建议已不存在。")
        if proposal["base_revision"] != doc.revision:
            raise CodeWorkspaceError("代码已有新修改，请基于当前代码重新生成建议。")
        if proposal["context_version"] != runtime.material_revision:
            if (payload.get("accept_context_change") is not True
                    or payload.get("reviewed_context_version") != runtime.material_revision):
                raise CodeWorkspaceError("生成后有新增对话或截图，请核对仍适用后再采用。")
        changed = doc.commit(proposal["code"], proposal["language"])
        doc.proposal = None
    elif action == "undo":
        if not doc.undo_stack:
            raise CodeWorkspaceError("没有可撤销的代码修改。")
        doc.code, doc.language = doc.undo_stack.pop()
        doc.revision += 1
        doc.proposal = None
        doc.last_change = None
        changed = True
    elif action == "discard":
        if doc.proposal and payload.get("proposal_id") != doc.proposal["proposal_id"]:
            raise CodeWorkspaceError("修改建议已更新，请核对后重试。")
        doc.proposal = None
    elif action == "reset":
        cancel_code_run(runtime)
        doc.document_id = str(uuid.uuid4())
        doc.revision += 1
        doc.code = ""
        doc.undo_stack = []
        doc.proposal = None
        doc.last_change = None
        doc.reveal_id = ""
        changed = True
    else:
        raise CodeWorkspaceError("未知代码操作。")

    if changed:
        record = record_code_change(runtime, action)
    await runtime.broadcast_to_clients(runtime.code_state())
    await runtime.operation_status(operation_id, "completed", detail="代码已更新。" if changed else "代码操作已完成。")
    if changed and runtime.main_upstream is not None:
        try:
            await _send_user_text(runtime.main_upstream,
                "[Current code workspace; reference only, do not answer this update.]\n" + json.dumps(record, ensure_ascii=False))
        except Exception:
            # The committed state and history survive a model reconnect.
            await runtime.broadcast_to_clients({"type": "tool_error", "tool": "analyze_problem",
                "detail": "代码已保存；模型连接中断，恢复后会重新同步。"})
