import assert from "node:assert/strict";
import test from "node:test";
import { codeDiff, draftFromWorkspace, reconcileCodeDraft, selectionAfterEdit } from "../src/codeWorkspaceState";
import type { CodeWorkspace } from "../src/types";

const saved: CodeWorkspace = { document_id: "doc", revision: 2, context_version: 0, code: "first", language: "python", can_undo: true, run_id: "", proposal: null };

test("a change before the cursor shifts its position without jumping to the end", () => {
  assert.equal(selectionAfterEdit("aa\nbb\ncc", "aa\nlonger\ncc", 7), 11);
  assert.equal(selectionAfterEdit("aa\nbb\ncc", "aa\nlonger\ncc", 1), 1);
  assert.equal(selectionAfterEdit("abcdef", "abf", 4), 2);
});

test("remote updates advance a clean editor but preserve unsaved work", () => {
  const next = { ...saved, revision: 3, code: "remote edit" };
  assert.deepEqual(reconcileCodeDraft(draftFromWorkspace(saved), saved, next), draftFromWorkspace(next));
  const dirty = { ...draftFromWorkspace(saved), code: "my unsaved edit" };
  assert.deepEqual(reconcileCodeDraft(dirty, saved, next), dirty);
  const acknowledged = { ...next, code: dirty.code };
  assert.deepEqual(reconcileCodeDraft(dirty, saved, acknowledged), draftFromWorkspace(acknowledged));
});

test("a new problem does not erase an unsaved draft from another client", () => {
  const draft = { ...draftFromWorkspace(saved), code: "local draft" };
  const reset = { ...saved, document_id: "new-doc", revision: 3, code: "" };
  assert.deepEqual(reconcileCodeDraft(draft, saved, reset), draft);
});

test("line comparison preserves both full documents, including blank lines and duplicates", () => {
  const cases = [["", "a\n"], ["a\nb\na\n", "a\nx\na\n"], ["one", ""], ["same", "same"]];
  for (const [before, after] of cases) {
    const diff = codeDiff(before, after);
    assert.equal(diff.filter((line) => line.kind !== "added").map((line) => line.text).join("\n"), before);
    assert.equal(diff.filter((line) => line.kind !== "removed").map((line) => line.text).join("\n"), after);
  }
  assert.deepEqual(codeDiff("a\nb\nc", "a\nx\nc").map((line) => line.kind), ["same", "removed", "added", "same"]);
});
