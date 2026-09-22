import assert from "node:assert/strict";
import React from "react";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { AnswerMarkdown } from "../src/AnswerMarkdown";
import {
  answerActionPayload,
  applyChannelHealth,
  listeningStatus,
  manualDraftKey,
  mergeAnswerEvent,
  mergeOperation,
  mergeTranscriptTurn,
  transcriptForSpeaker,
  newerAnswerCount,
  reconcileReadingAnswer,
  reconcileReadingSelection,
  visibleAnswerOrder,
} from "../src/interviewUiState";
import { parseServerEvent, SessionClient } from "../src/sessionClient";
import type { AnswerRecord, AnswerStore, ServerEvent, TranscriptTurn } from "../src/types";

test("native candidate finals can arrive out of order without replacing newer speech", () => {
  const first: TranscriptTurn = { turn_id: "a", speaker: "candidate", text: "First partial", status: "streaming" };
  const second: TranscriptTurn = { turn_id: "b", speaker: "candidate", text: "Second partial", status: "streaming" };
  const original = [first, second];
  const revised = mergeTranscriptTurn(original, { ...first, text: "First final", status: "completed" });
  assert.deepEqual(transcriptForSpeaker(revised, "candidate"), { final: "First final", partial: "Second partial" });
  assert.equal(original[0].text, "First partial");
  let completed = mergeTranscriptTurn(original, { ...second, text: "Second final", status: "completed" });
  completed = mergeTranscriptTurn(completed, { ...first, text: "First final", status: "completed" });
  assert.deepEqual(completed.map((turn) => turn.turn_id), ["a", "b"]);
  assert.deepEqual(transcriptForSpeaker(completed, "candidate"), { final: "Second final", partial: "" });
  assert.equal(mergeTranscriptTurn(completed, first), completed);
});

test("candidate snapshot restores partial items and final correction can clear spurious text", () => {
  const snapshot: TranscriptTurn[] = [
    { turn_id: "a", speaker: "candidate", text: "Older", status: "completed" },
    { turn_id: "b", speaker: "candidate", text: "Still speaking", status: "streaming" },
  ];
  assert.deepEqual(transcriptForSpeaker(snapshot, "candidate"), { final: "Older", partial: "Still speaking" });
  const cleared = mergeTranscriptTurn(snapshot, { ...snapshot[1], text: "", status: "completed" });
  assert.deepEqual(transcriptForSpeaker(cleared, "candidate"), { final: "Older", partial: "" });
  const interrupted = mergeTranscriptTurn(snapshot, { ...snapshot[1], status: "interrupted" });
  assert.equal(interrupted[1].status, "interrupted");
  assert.equal(transcriptForSpeaker(interrupted, "candidate").partial, "");
});

test("correction drafts are isolated by the question they belong to", () => {
  assert.notEqual(manualDraftKey("misheard", "q1"), manualDraftKey("misheard", "q2"));
  assert.notEqual(manualDraftKey("misheard", "q1"), manualDraftKey("conditions", "q1"));
  assert.equal(manualDraftKey("candidate", "q1"), manualDraftKey("candidate", "q2"));
});

const oldAnswer: AnswerRecord = {
  responseId: "answer-old",
  questionId: "question-old",
  text: "An earlier solution.",
  status: "completed",
  createdAt: "2026-09-07T12:00:00Z",
};

test("new answers preserve the answer being read and expose an unread count", () => {
  assert.equal(reconcileReadingAnswer("a", ["a", "b", "c"]), "a");
  assert.equal(newerAnswerCount("a", ["a", "b", "c"]), 2);
  assert.equal(reconcileReadingAnswer(null, ["a", "b", "c"]), "c");
  assert.equal(newerAnswerCount("c", ["a", "b", "c"]), 0);
});

function answerStore(...records: AnswerRecord[]): AnswerStore {
  return { order: records.map((answer) => answer.responseId), byId: Object.fromEntries(records.map((answer) => [answer.responseId, answer])) };
}

test("terminal tool classification hides a preamble without rewriting its raw completed text", () => {
  const preamble: AnswerRecord = { ...oldAnswer, responseId: "tool-phase", text: "Let me analyze the problem." };
  const classified = mergeAnswerEvent(preamble, {
    type: "answer_completed", response_id: preamble.responseId, text: "late replacement", intermediate: true,
  }, "completed", false);
  const final: AnswerRecord = { ...oldAnswer, responseId: "final", text: "The complete solution." };
  const store = answerStore(oldAnswer, classified, final);
  assert.deepEqual(store.order, [oldAnswer.responseId, preamble.responseId, final.responseId]);
  assert.equal(store.byId[preamble.responseId].text, preamble.text);
  assert.equal(store.byId[preamble.responseId].status, "completed");
  assert.deepEqual(visibleAnswerOrder(store), [oldAnswer.responseId, final.responseId]);
  assert.equal(preamble.intermediate, undefined);
});

test("snapshot and delta metadata agree while old messages remain normal answers", () => {
  const base: AnswerRecord = { ...oldAnswer, responseId: "tool-phase", text: "", status: "streaming" };
  const parsed = parseServerEvent(JSON.stringify({ type: "answer_delta", response_id: base.responseId, delta: "Checking.", intermediate: true }));
  assert.ok(parsed);
  const streamed = mergeAnswerEvent(base, parsed, "streaming", false);
  assert.equal(streamed.text, "Checking.");
  assert.equal(streamed.intermediate, true);
  const completed = mergeAnswerEvent(streamed, { type: "answer_completed", text: "Checking." }, "completed", false);
  assert.equal(completed.intermediate, true);
  const lateDelta = mergeAnswerEvent(completed, { type: "answer_delta", delta: "late text", intermediate: true }, "streaming", false);
  assert.equal(lateDelta.text, "Checking.");
  assert.equal(lateDelta.status, "completed");
  const snapshot = mergeAnswerEvent(base, { type: "answer_snapshot", text: "Checking.", intermediate: true }, "completed", true);
  assert.equal(snapshot.intermediate, true);
  assert.deepEqual(visibleAnswerOrder(answerStore(snapshot, oldAnswer)), [oldAnswer.responseId]);
  const legacy = mergeAnswerEvent({ ...base, responseId: "legacy" }, { type: "answer_completed", text: "Normal answer." }, "completed", false);
  assert.equal(legacy.intermediate, undefined);
  assert.deepEqual(visibleAnswerOrder(answerStore(legacy)), [legacy.responseId]);
});

test("a reader on a newly hidden preamble follows its final answer and skips it in counts and actions", () => {
  const preamble: AnswerRecord = { ...oldAnswer, responseId: "tool-phase", status: "streaming", text: "Let me inspect." };
  let store = answerStore(oldAnswer, preamble);
  let visible = visibleAnswerOrder(store);
  let selected = reconcileReadingSelection(null, visible, store.order);
  assert.equal(selected, preamble.responseId);
  store = answerStore(oldAnswer, mergeAnswerEvent(preamble, { type: "answer_completed", intermediate: true }, "completed", false));
  visible = visibleAnswerOrder(store);
  selected = reconcileReadingSelection(selected, visible, store.order);
  assert.equal(reconcileReadingAnswer(selected, visible), oldAnswer.responseId);
  assert.equal(newerAnswerCount(oldAnswer.responseId, visible), 0);
  const final: AnswerRecord = { ...oldAnswer, responseId: "final", questionId: "question-final", text: "The full result." };
  store = answerStore(oldAnswer, store.byId[preamble.responseId], final);
  visible = visibleAnswerOrder(store);
  selected = reconcileReadingSelection(selected, visible, store.order);
  const reading = reconcileReadingAnswer(selected, visible);
  assert.equal(reading, final.responseId);
  assert.equal(visible.length, 2);
  assert.equal(newerAnswerCount(oldAnswer.responseId, visible), 1);
  assert.equal(answerActionPayload("expand", "op-final", store.byId[reading!], "other-question").response_id, final.responseId);
  assert.equal(reconcileReadingSelection(oldAnswer.responseId, visible, store.order), oldAnswer.responseId);
});

test("snapshot selection retains an explicit real answer or waits past a hidden latest phase", () => {
  const hidden: AnswerRecord = { ...oldAnswer, responseId: "tool-phase", text: "Checking.", intermediate: true };
  const snapshot = answerStore(oldAnswer, hidden);
  const visible = visibleAnswerOrder(snapshot);
  const waiting = reconcileReadingSelection(null, visible, snapshot.order);
  assert.equal(reconcileReadingAnswer(waiting, visible), oldAnswer.responseId);
  assert.equal(reconcileReadingSelection(oldAnswer.responseId, visible, snapshot.order), oldAnswer.responseId);
  const final: AnswerRecord = { ...oldAnswer, responseId: "final", text: "Finished." };
  const recovered = answerStore(oldAnswer, hidden, final);
  const recoveredVisible = visibleAnswerOrder(recovered);
  assert.equal(reconcileReadingSelection(waiting, recoveredVisible, recovered.order), final.responseId);
  assert.equal(reconcileReadingSelection(oldAnswer.responseId, recoveredVisible, recovered.order), oldAnswer.responseId);
  const onlyHidden = answerStore(hidden);
  const emptyVisible = visibleAnswerOrder(onlyHidden);
  assert.equal(reconcileReadingAnswer(reconcileReadingSelection(null, emptyVisible, onlyHidden.order), emptyVisible), null);
  assert.equal(newerAnswerCount(null, emptyVisible), 0);
  assert.throws(() => answerActionPayload("rephrase", "op-empty", undefined, "question"));
});

test("rewrites and deep analysis retain the chosen answer and question, not the latest question", () => {
  for (const action of ["shorten", "expand", "deep"] as const) {
    assert.deepEqual(answerActionPayload(action, "op-1", oldAnswer, "question-new"), {
      type: "quick_answer", action, operation_id: "op-1", response_id: "answer-old", question_id: "question-old",
    });
  }
  assert.throws(() => answerActionPayload("expand", "op-2", undefined, "question-new"));
});

test("an active interview cannot appear to be listening while the client is disconnected", () => {
  const result = listeningStatus({ connected: false, reconnecting: true, active: true, deviceStatus: "ready",
    channels: { interviewer: { phase: "listening", message: "" }, candidate: { phase: "listening", message: "" } }, held: false, answering: false });
  assert.equal(result.live, false);
  assert.equal(result.label, "重新连接中");
  assert.match(result.detail, /等待恢复/);
});

test("muted and interrupted channels remain unhealthy despite a ready transport", () => {
  for (const phase of ["muted", "interrupted"] as const) {
    const interviewer = applyChannelHealth(true, "ready", true, { phase });
    assert.equal(interviewer.phase, phase);
    assert.equal(listeningStatus({ connected: true, reconnecting: false, active: true, deviceStatus: "ready",
      channels: { interviewer, candidate: { phase: "listening", message: "" } }, held: false, answering: false }).live, false);
  }
});

test("server channel_details preserves the specific channel failure", () => {
  const event = parseServerEvent(JSON.stringify({ type: "device_status", status: "error", channels: { interviewer: true, candidate: false }, channel_details: { candidate: { phase: "interrupted", detail: "Microphone disconnected" } } }));
  assert.ok(event);
  const candidate = applyChannelHealth(event.channels?.candidate === true, "error", true, event.channel_details?.candidate);
  assert.equal(candidate.phase, "interrupted");
  assert.equal(candidate.message, "Microphone disconnected");
});

test("held answers explicitly continue transcription rather than claim audio stopped", () => {
  const result = listeningStatus({ connected: true, reconnecting: false, active: true, deviceStatus: "ready",
    channels: { interviewer: { phase: "listening", message: "" }, candidate: { phase: "listening", message: "" } }, held: true, answering: false });
  assert.equal(result.label, "只听不答");
  assert.match(result.detail, /继续接收和转写/);
});

test("operation feedback only becomes complete after a terminal server event", () => {
  let operations = mergeOperation([], { operation_id: "op", kind: "quick_answer", action: "deep", status: "sent" });
  operations = mergeOperation(operations, { operation_id: "op", kind: "quick_answer", status: "running" });
  assert.equal(operations[0].status, "running");
  operations = mergeOperation(operations, { operation_id: "op", kind: "quick_answer", status: "completed" });
  assert.equal(mergeOperation(operations, { operation_id: "op", kind: "quick_answer", status: "accepted" }), operations);
  assert.equal(operations[0].action, "deep");
});

test("Markdown preserves code, lists and tables and offers code-only copy", () => {
  const markup = renderToStaticMarkup(<AnswerMarkdown text={'1. Check the boundary\n2. Return the result\n\n```python\ndef solve(x):\n    return x < 3\n```\n\n| Input | Output |\n| --- | --- |\n| 2 | true |'} />);
  assert.match(markup, /<ol>/);
  assert.match(markup, /<table>/);
  assert.match(markup, /return x &lt; 3/);
  assert.match(markup, /aria-label="复制代码"/);
});

test("Markdown never executes HTML, embeds remote images, or enables unsafe links", () => {
  const markup = renderToStaticMarkup(<AnswerMarkdown text={'<script>alert(1)</script>\n\n<img src="https://example.invalid/private">\n\n![tracking](https://example.invalid/tracker)\n\n[bad](javascript:alert(1)) [local](file:///secret.txt) [safe](https://example.com/docs)'} />);
  assert.doesNotMatch(markup, /<script|<img|javascript:|file:\/\/\/|tracker|private/);
  assert.match(markup, /href="https:\/\/example.com\/docs"/);
  assert.match(markup, /rel="noreferrer noopener"/);
});

test("event parsing rejects arrays and untyped objects", () => {
  assert.equal(parseServerEvent("[]"), null);
  assert.equal(parseServerEvent('{"text":"untyped"}'), null);
  assert.deepEqual(parseServerEvent('{"type":"question_state","hold_answers":true}'), { type: "question_state", hold_answers: true });
});

test("late messages from a stopped socket cannot revive the client or change answers", async () => {
  const oldWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const oldWebSocket = Object.getOwnPropertyDescriptor(globalThis, "WebSocket");
  const connections: FakeWebSocket[] = [];
  class FakeWebSocket {
    static OPEN = 1;
    static CLOSING = 2;
    readyState = 0;
    listeners = new Map<string, Array<(event: any) => void>>();
    constructor(_url: string) { connections.push(this); }
    addEventListener(type: string, listener: (event: any) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
    }
    emit(type: string, event: any = {}) { this.listeners.get(type)?.forEach((listener) => listener(event)); }
    send(_payload: string) {}
    close(code = 1000) { this.readyState = 3; this.emit("close", { code }); }
  }
  Object.defineProperty(globalThis, "window", { configurable: true, value: { setTimeout, clearTimeout, setInterval, clearInterval } });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: FakeWebSocket });
  const received: ServerEvent[] = [];
  const states: string[] = [];
  const client = new SessionClient("http://127.0.0.1:8000", { interview_id: "synthetic", session_token: "synthetic-token" }, {
    onEvent: (event) => received.push(event), onConnectionChange: (state) => states.push(state), onError: () => {}, onSessionUnavailable: () => {},
  });
  try {
    const ready = client.start();
    const socket = connections[0];
    socket.readyState = 1;
    socket.emit("open");
    socket.emit("message", { data: JSON.stringify({ type: "session_ready", realtime_protocol: "realtime-interview-v5" }) });
    await ready;
    client.stop();
    socket.emit("message", { data: JSON.stringify({ type: "session_ready", realtime_protocol: "realtime-interview-v5" }) });
    socket.emit("message", { data: JSON.stringify({ type: "answer_delta", response_id: "stale", delta: "stale" }) });
    assert.equal(client.isReady(), false);
    assert.equal(received.length, 0);
    assert.equal(states.at(-1), "disconnected");
  } finally {
    client.stop();
    if (oldWindow) Object.defineProperty(globalThis, "window", oldWindow); else Reflect.deleteProperty(globalThis, "window");
    if (oldWebSocket) Object.defineProperty(globalThis, "WebSocket", oldWebSocket); else Reflect.deleteProperty(globalThis, "WebSocket");
  }
});
