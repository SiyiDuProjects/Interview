from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import AnswerScope, CandidateContext
from app.services.project_context_store import resolve_all_project_contexts, resolve_project_context


@dataclass(frozen=True)
class RealtimeContextConfig:
    context: CandidateContext
    answer_scope: AnswerScope = "general"
    project_context_label: str = ""


def build_realtime_instructions(config: RealtimeContextConfig) -> str:
    context = config.context
    return (
        "你是候选人的实时技术面试辅助。你听到的是面试官系统音频；候选人麦克风内容只作为上下文。\n"
        "只在面试官提出完整问题或追问时回答。不要复述问题，不要写免责声明。\n"
        "输出必须只使用简体中文，风格要像候选人现场可以直接说出口的口语回答。\n"
        "默认回答控制在 10 到 20 秒；如果问题需要更完整解释，也保持短句和清晰取舍。\n"
        "如果只是寒暄、噪声、候选人在说话、或问题不完整，不要输出答案。\n"
        "需要候选人项目、简历或岗位事实时，优先调用 lookup_candidate_context，不要编造背景。\n"
        "不需要用户选择项目模式；根据面试官问题自动判断应该使用哪个项目背景。\n"
        "普通概念题、项目题、行为题直接回答，不要为了显得复杂而调用深度工具。\n"
        "如果面试官要求写代码、实现算法、写复杂 SQL、debug、分析复杂度或追问刚刚写的代码，调用 solve_code_question。\n"
        "如果追问依赖当前屏幕、在线 IDE、白板、截图题或“这段代码/这里/刚刚写的”，先调用 capture_current_screen，再结合屏幕上下文回答或调用 solve_code_question。\n"
        f"候选人姓名: {context.name or '未填写'}\n"
        f"目标岗位: {context.target_role or '未填写'}\n"
        f"简历摘要: {_clip(context.resume, 1400) or '未填写'}\n"
        f"岗位描述: {_clip(context.job_description, 900) or '未填写'}\n"
        f"补充偏好: {_clip(context.custom_notes, 700) or '未填写'}"
    )


def lookup_candidate_context(query: str, scope: AnswerScope, context: CandidateContext) -> str:
    blocks: list[tuple[str, str]] = []
    if context.resume.strip():
        blocks.append(("简历", context.resume.strip()))
    if context.job_description.strip():
        blocks.append(("岗位描述", context.job_description.strip()))
    if context.custom_notes.strip():
        blocks.append(("补充备注", context.custom_notes.strip()))

    if scope == "general":
        project_blocks = resolve_all_project_contexts()
    else:
        project_label, project_context = resolve_project_context(scope)
        project_blocks = [(project_label or scope, project_context)] if project_context else []
    blocks.extend(project_blocks)

    keywords = _extract_keywords(query)
    scored: list[tuple[int, str]] = []
    project_labels = {label for label, _ in project_blocks}
    for label, text in blocks:
        for paragraph in _split_paragraphs(text):
            score = _score(paragraph, keywords)
            if label in project_labels:
                score += _project_name_score(label, query)
            if score > 0:
                scored.append((score, f"[{label}] {paragraph}"))

    if not scored:
        return "\n\n".join(f"[{label}] {_clip(text, 900)}" for label, text in blocks[:5] if text).strip()

    scored.sort(key=lambda item: item[0], reverse=True)
    return "\n\n".join(text for _, text in scored[:8])


def _extract_keywords(query: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z0-9_+#.-]{2,}", query.lower()))
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    for chunk in chinese_chunks:
        words.add(chunk)
        for index in range(0, max(0, len(chunk) - 1)):
            words.add(chunk[index : index + 2])
    return words


def _split_paragraphs(text: str) -> list[str]:
    paragraphs = [item.strip(" \t\r\n-") for item in re.split(r"\n\s*\n|\n(?=#)|\n(?=- )", text)]
    return [item for item in paragraphs if item]


def _score(text: str, keywords: set[str]) -> int:
    normalized = text.lower()
    return sum(1 for keyword in keywords if keyword and keyword.lower() in normalized)


def _project_name_score(label: str, query: str) -> int:
    normalized_query = query.lower()
    normalized_label = label.lower()
    score = 0
    for token in re.findall(r"[a-z0-9]+", normalized_label):
        if len(token) >= 3 and token in normalized_query:
            score += 3
    if "canvas" in normalized_label and "canvas" in normalized_query:
        score += 4
    if "discord" in normalized_label and "discord" in normalized_query:
        score += 4
    if "innovation" in normalized_label and "innovation" in normalized_query:
        score += 4
    return score


def _clip(text: str, limit: int) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."
