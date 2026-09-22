import type { CodeWorkspace } from "./types";

export interface CodeDraft {
  documentId: string;
  baseRevision: number;
  code: string;
  language: string;
}

export function draftFromWorkspace(state: CodeWorkspace): CodeDraft {
  return { documentId: state.document_id, baseRevision: state.revision, code: state.code, language: state.language };
}

export function reconcileCodeDraft(draft: CodeDraft, previous: CodeWorkspace, incoming: CodeWorkspace): CodeDraft {
  const unchanged = draft.documentId === previous.document_id && draft.baseRevision === previous.revision
    && draft.code === previous.code && draft.language === previous.language;
  const acknowledged = draft.documentId === incoming.document_id && draft.code === incoming.code && draft.language === incoming.language;
  return unchanged || acknowledged ? draftFromWorkspace(incoming) : draft;
}

export type DiffLine = { kind: "same" | "added" | "removed"; text: string; before?: number; after?: number };

export function selectionAfterEdit(before: string, after: string, position: number) {
  let prefix = 0, suffix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix++;
  while (suffix < before.length - prefix && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]) suffix++;
  if (position <= prefix) return position;
  if (position >= before.length - suffix) return Math.max(prefix, position + after.length - before.length);
  return Math.min(position, after.length - suffix);
}

export function codeDiff(before: string, after: string): DiffLine[] {
  const a = before ? before.split("\n") : [];
  const b = after ? after.split("\n") : [];
  // Typical documents are a few dozen lines. Bound work for a large paste
  // without dropping any content from the displayed comparison.
  if (a.length * b.length > 250_000) return [
    ...a.map((text, i): DiffLine => ({ kind: "removed", text, before: i + 1 })),
    ...b.map((text, i): DiffLine => ({ kind: "added", text, after: i + 1 })),
  ];
  const lengths = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) {
    lengths[i][j] = a[i] === b[j] ? lengths[i + 1][j + 1] + 1 : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
  }
  const lines: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      lines.push({ kind: "same", text: a[i], before: ++i, after: ++j });
    } else if (i < a.length && (j === b.length || lengths[i + 1][j] >= lengths[i][j + 1])) {
      lines.push({ kind: "removed", text: a[i], before: ++i });
    } else {
      lines.push({ kind: "added", text: b[j], after: ++j });
    }
  }
  return lines;
}
