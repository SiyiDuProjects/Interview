import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "@heroui/react";
import { AnswerMarkdown, CopyTextButton } from "./AnswerMarkdown";
import { codeDiff, draftFromWorkspace, reconcileCodeDraft, selectionAfterEdit } from "./codeWorkspaceState";
import type { CodeWorkspace, OperationRecord } from "./types";
import { operationIsPending } from "./interviewUiState";

export function CodePanel({ workspace, enabled, operations, dispatch, importedCode, clearImport }: {
  workspace: CodeWorkspace;
  enabled: boolean;
  operations: OperationRecord[];
  dispatch: (payload: Record<string, unknown>) => string | null;
  importedCode?: { code: string; language: string } | null;
  clearImport?: () => void;
}) {
  const [draft, setDraft] = useState(() => draftFromWorkspace(workspace));
  const previous = useRef(workspace);
  const [instruction, setInstruction] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);
  const restore = useRef<{ start: number; end: number; top: number; left: number } | null>(null);
  useLayoutEffect(() => {
    if (previous.current === workspace) return;
    const prior = previous.current;
    const next = reconcileCodeDraft(draft, prior, workspace);
    if (next !== draft) {
      const element = editor.current;
      if (element && next.code !== draft.code) restore.current = {
        start: selectionAfterEdit(draft.code, next.code, element.selectionStart),
        end: selectionAfterEdit(draft.code, next.code, element.selectionEnd),
        top: element.scrollTop, left: element.scrollLeft,
      };
      setDraft(next);
    }
    previous.current = workspace;
  }, [workspace, draft]);
  useLayoutEffect(() => {
    if (restore.current && editor.current) {
      editor.current.setSelectionRange(restore.current.start, restore.current.end);
      editor.current.scrollTop = restore.current.top;
      editor.current.scrollLeft = restore.current.left;
      restore.current = null;
    }
  }, [draft.code]);
  const dirty = draft.code !== workspace.code || draft.language !== workspace.language;
  const conflict = draft.documentId !== workspace.document_id || draft.baseRevision !== workspace.revision;
  const mutating = operations.some((item) => item.kind === "code_action" && item.action !== "generate" && operationIsPending(item));
  const generating = Boolean(workspace.run_id) || operations.some((item) => item.kind === "code_action" && item.action === "generate" && operationIsPending(item));
  const proposal = workspace.proposal;
  const diff = useMemo(() => proposal ? codeDiff(proposal.base_code, proposal.code) : [], [proposal]);
  const change = workspace.last_change;
  const appliedDiff = useMemo(() => change ? codeDiff(change.base_code, change.code) : [], [change]);
  function act(action: string, extra: Record<string, unknown> = {}) {
    return dispatch({ type: "code_action", action, document_id: workspace.document_id, base_revision: workspace.revision, ...extra });
  }
  return <section className="code-panel" aria-labelledby="code-title">
    {importedCode && <div className="code-import">
      <p>已选中回答里的代码。载入为草稿后，可继续修改。</p>
      <pre className="code-diff" tabIndex={0} aria-label="待载入的代码">{importedCode.code}</pre>
      <Button size="sm" variant="secondary" isDisabled={!enabled || dirty || conflict || mutating}
        onPress={() => { setDraft({ ...draft, ...importedCode }); clearImport?.(); }}>载入为草稿</Button>
      <Button size="sm" variant="ghost" onPress={clearImport}>取消载入</Button>
      {dirty && <p>先保存当前草稿，再载入这份代码。</p>}
    </div>}
    <div className="code-heading">
      <h2 id="code-title">当前代码</h2>
      <span>{dirty ? "草稿未保存" : `已保存 · v${workspace.revision}`} · {draft.code ? draft.code.split("\n").length : 0} 行</span>
      <CopyTextButton text={draft.code} label="复制代码" />
    </div>
    <div className="code-meta">
      <label htmlFor="code-language">语言</label>
      <input id="code-language" value={draft.language} maxLength={64} disabled={!enabled}
        onChange={(event) => setDraft({ ...draft, language: event.target.value })} />
      <span id="code-help">AI 随问答自动写入、修改代码。也可手动编辑并保存。</span>
    </div>
    <label className="sr-only" htmlFor="current-code">编辑当前代码</label>
    <textarea ref={editor} id="current-code" className="code-editor" value={draft.code} spellCheck={false} autoCapitalize="off" autoCorrect="off"
      wrap="off" aria-describedby="code-help" readOnly={!enabled}
      onChange={(event) => setDraft({ ...draft, code: event.target.value })} placeholder="需要写代码时，AI 会自动在这里生成。也可粘贴现有代码。" />
    {conflict && <div className="code-warning" role="status">其他操作更新了代码，你的草稿已保留。复制草稿备份后，可载入最新版本再合并。
      <Button size="sm" variant="secondary" onPress={() => setDraft(draftFromWorkspace(workspace))}>载入最新版本</Button>
    </div>}
    <div className="code-actions">
      <Button size="sm" variant="secondary" isDisabled={!enabled || !dirty || conflict || mutating || !draft.language.trim()}
        onPress={() => act("save", { code: draft.code, language: draft.language })}>保存修改</Button>
      <Button size="sm" variant="ghost" isDisabled={!enabled || dirty || conflict || mutating || !workspace.can_undo}
        onPress={() => act("undo")}>撤销上次修改</Button>
      <Button size="sm" variant="ghost" isDisabled={!enabled || dirty || conflict || mutating}
        onPress={() => setConfirmReset(!confirmReset)}>换一道题</Button>
    </div>
    {confirmReset && <div className="code-warning">开始新题会清空代码区和撤销记录，旧代码仍作为本场上下文保留。
      <Button size="sm" variant="secondary" isDisabled={!enabled || dirty || conflict || mutating} onPress={() => { act("reset"); setConfirmReset(false); setInstruction(""); }}>开始新题</Button>
      <Button size="sm" variant="ghost" onPress={() => setConfirmReset(false)}>取消</Button>
    </div>}
    {generating && <div className="code-actions">
      <span role="status">AI 正在处理代码…</span>
      <Button size="sm" variant="secondary" isDisabled={!enabled || mutating || !workspace.run_id}
        onPress={() => act("stop", { run_id: workspace.run_id })}>停止</Button>
    </div>}
    {change && <details className="code-proposal">
      <summary>{change.code === change.base_code ? "本次无需修改" : `已自动更新 · v${change.revision} · 查看改动`}</summary>
      <AnswerMarkdown text={change.explanation} />
      {change.code !== change.base_code && <pre className="code-diff" tabIndex={0} aria-label="已应用的代码改动，减号为删除，加号为新增">{appliedDiff.map((line, index) =>
        <span key={index} className={`diff-${line.kind}`}><span className="diff-number">{line.before ?? ""}</span><span className="diff-number">{line.after ?? ""}</span>{line.kind === "added" ? "+ " : line.kind === "removed" ? "− " : "  "}{line.text}{"\n"}</span>)}</pre>}
    </details>}
    <details className="code-request">
      <summary>手动请求修改建议</summary>
    <form onSubmit={(event) => { event.preventDefault(); if (enabled && !dirty && !conflict && !mutating && !generating) act("generate", { instruction }); }}>
      <label htmlFor="code-instruction">修改要求 <span>可留空，预览后采用</span></label>
      <div>
        <input id="code-instruction" value={instruction} maxLength={12_000} disabled={!enabled || generating}
          onChange={(event) => setInstruction(event.target.value)} placeholder="例如：先写朴素解法 / 处理重复元素 / 优化复杂度" />
        <Button type="submit" isDisabled={!enabled || dirty || conflict || mutating || generating}>生成建议</Button>
      </div>
      {dirty && <p>先保存当前修改，再让 AI 接着写。</p>}
    </form>
    </details>
    {proposal && <section className="code-proposal" aria-labelledby="proposal-title">
      <h3 id="proposal-title">{proposal.code === proposal.base_code ? "这一步无需改代码" : "建议修改"}</h3>
      <AnswerMarkdown text={proposal.explanation} />
      {proposal.code !== proposal.base_code && <pre className="code-diff" tabIndex={0} aria-label="代码修改对比，减号为删除，加号为新增">{diff.map((line, index) =>
        <span key={index} className={`diff-${line.kind}`}><span className="diff-number">{line.before ?? ""}</span><span className="diff-number">{line.after ?? ""}</span>{line.kind === "added" ? "+ " : line.kind === "removed" ? "− " : "  "}{line.text}{"\n"}</span>)}</pre>}
      {proposal.code_changed && <p className="code-warning">代码已有新版本，请基于当前代码重新生成建议。</p>}
      {proposal.context_changed && <p className="code-warning">生成后有新增对话或截图。这份建议未包含全部新增内容，请先核对。</p>}
      <div className="code-actions">
        <Button size="sm" isDisabled={!enabled || dirty || conflict || mutating || proposal.code_changed}
          onPress={() => act("apply", { proposal_id: proposal.proposal_id, accept_context_change: proposal.context_changed, reviewed_context_version: workspace.context_version })}>
          {proposal.context_changed ? "核对后仍采用" : proposal.code === proposal.base_code ? "知道了" : "采用修改"}</Button>
        <Button size="sm" variant="ghost" isDisabled={!enabled || mutating}
          onPress={() => act("discard", { proposal_id: proposal.proposal_id })}>收起建议</Button>
      </div>
    </section>}
  </section>;
}
