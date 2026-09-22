const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function compile(relativePath, globals = {}) {
  const filename = path.join(__dirname, "..", relativePath);
  const exports = {};
  const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(output, { exports, Int16Array, ArrayBuffer, AbortController, URL, ...globals }, { filename });
  return exports;
}

function timers() {
  let nextId = 1;
  const tasks = new Map();
  return {
    tasks,
    setTimeout(fn) { const id = nextId++; tasks.set(id, fn); return id; },
    clearTimeout(id) { tasks.delete(id); },
    setInterval(fn) { const id = nextId++; tasks.set(id, fn); return id; },
    clearInterval(id) { tasks.delete(id); },
  };
}

function audioFixture({ addModule = () => Promise.resolve() } = {}) {
  let now = 0;
  class Track extends EventTarget {
    constructor(kind = "audio") { super(); this.kind = kind; this.muted = false; this.readyState = "live"; this.stops = 0; }
    stop() { this.readyState = "ended"; this.stops++; }
    mute(value) { this.muted = value; this.dispatchEvent(new Event(value ? "mute" : "unmute")); }
    end() { this.readyState = "ended"; this.dispatchEvent(new Event("ended")); }
  }
  class Stream {
    constructor(tracks) { this.tracks = tracks; }
    getTracks() { return this.tracks; }
    getAudioTracks() { return this.tracks.filter((track) => track.kind === "audio"); }
  }
  class Context extends EventTarget {
    static all = [];
    constructor() { super(); this.state = "running"; this.sampleRate = 24000; this.audioWorklet = { addModule }; this.processor = null; Context.all.push(this); }
    get currentTime() { return now / 1000; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createGain() { return { gain: {}, connect() {}, disconnect() {} }; }
    resume() { this.setState("running"); return Promise.resolve(); }
    close() { this.state = "closed"; return Promise.resolve(); }
    setState(state) { this.state = state; this.dispatchEvent(new Event("statechange")); }
    frame(age = 0) { this.processor.port.onmessage?.({ data: { pcm: new ArrayBuffer(2048), endTime: this.currentTime - age } }); }
  }
  class WorkletNode {
    constructor(context, name, options) { this.port = { close() {} }; this.options = options; context.processor = this; }
    connect() {}
    disconnect() {}
  }
  const clock = timers();
  const module = compile("src/audioCapture.ts", {
    MediaStream: Stream, AudioContext: Context, AudioWorkletNode: WorkletNode,
    document: { baseURI: "file:///application/dist/index.html" }, window: clock, Date: { now: () => now },
  });
  return { Track, Stream, Context, clock, module, advance: (milliseconds) => { now += milliseconds; } };
}

test("ordinary silence remains healthy; mute/unmute has separate recoverable state", async () => {
  const fixture = audioFixture();
  const track = new fixture.Track();
  const states = [], chunks = [];
  let ended = 0;
  const handle = fixture.module.startLocalAudioCapture({
    stream: new fixture.Stream([track]), onChunk: (chunk) => chunks.push(chunk),
    onHealthChange: (state) => states.push(state), onEnded: () => ended++,
  });
  const context = fixture.Context.all[0];
  assert.equal(handle.getHealth().phase, "interrupted");
  await Promise.resolve();
  for (let index = 0; index < 10; index++) { fixture.advance(500); context.frame(); }
  assert.equal(handle.getHealth().phase, "ready");
  assert.equal(chunks.length, 10);
  assert(new Int16Array(chunks[0]).every((sample) => sample === 0));
  track.mute(true);
  context.frame();
  assert.equal(handle.getHealth().phase, "muted");
  assert.equal(chunks.length, 10);
  assert.equal(ended, 0);
  track.mute(false);
  context.frame();
  assert.equal(handle.getHealth().phase, "ready");
  assert.equal(chunks.length, 11);
  assert.equal(states.filter((state) => state.phase === "error").length, 0);
  handle.stop();
  assert.equal(fixture.clock.tasks.size, 0);
});

test("AudioContext suspension and missing PCM callbacks are visible and can recover", async () => {
  const fixture = audioFixture();
  const track = new fixture.Track();
  const handle = fixture.module.startLocalAudioCapture({ stream: new fixture.Stream([track]), onChunk() {} });
  const context = fixture.Context.all[0];
  await Promise.resolve();
  context.frame();
  context.setState("suspended");
  assert.equal(handle.getHealth().phase, "interrupted");
  context.setState("running");
  assert.equal(handle.getHealth().phase, "ready");
  fixture.advance(4000);
  for (const callback of fixture.clock.tasks.values()) callback();
  assert.equal(handle.getHealth().phase, "interrupted");
  context.frame();
  assert.equal(handle.getHealth().phase, "ready");
  handle.stop();
});

test("one ended media source does not stop an independent source and notifies once", async () => {
  const fixture = audioFixture();
  const first = new fixture.Track(), second = new fixture.Track();
  let ended = 0;
  const a = fixture.module.startLocalAudioCapture({ stream: new fixture.Stream([first]), onChunk() {}, onEnded: () => ended++ });
  const b = fixture.module.startLocalAudioCapture({ stream: new fixture.Stream([second]), onChunk() {} });
  await Promise.resolve();
  fixture.Context.all.forEach((context) => context.frame());
  first.end(); first.end();
  assert.equal(ended, 1);
  assert.equal(a.getHealth().phase, "error");
  assert.equal(b.getHealth().phase, "ready");
  assert.equal(second.stops, 0);
  a.stop(); b.stop();
});

test("worklet startup failure and processor crashes remain visible until capture is replaced", async () => {
  const fixture = audioFixture({ addModule: () => Promise.reject(new Error("missing asset")) });
  const handle = fixture.module.startLocalAudioCapture({ stream: new fixture.Stream([new fixture.Track()]), onChunk() {} });
  await new Promise(setImmediate);
  for (const callback of fixture.clock.tasks.values()) callback();
  assert.equal(handle.getHealth().phase, "error");
  handle.stop();
  const healthy = audioFixture();
  const other = healthy.module.startLocalAudioCapture({ stream: new healthy.Stream([new healthy.Track()]), onChunk() {} });
  await Promise.resolve();
  const context = healthy.Context.all[0];
  context.frame();
  context.processor.onprocessorerror();
  context.frame();
  assert.equal(other.getHealth().phase, "error");
  other.stop();
});

test("stopping during worklet load cannot revive capture; stale queued PCM is discarded", async () => {
  let finish;
  const fixture = audioFixture({ addModule: () => new Promise((resolve) => { finish = resolve; }) });
  const track = new fixture.Track();
  const handle = fixture.module.startLocalAudioCapture({ stream: new fixture.Stream([track]), onChunk() {} });
  handle.stop();
  finish();
  await Promise.resolve();
  assert.equal(fixture.Context.all[0].processor, null);
  assert.equal(track.stops, 1);
  const healthy = audioFixture();
  const chunks = [];
  const other = healthy.module.startLocalAudioCapture({ stream: new healthy.Stream([new healthy.Track()]), onChunk: (chunk) => chunks.push(chunk) });
  await Promise.resolve();
  const context = healthy.Context.all[0];
  context.frame(2);
  assert.equal(other.getHealth().phase, "interrupted");
  assert.equal(chunks.length, 0);
  context.frame();
  assert.equal(chunks.length, 1);
  assert.equal(other.getHealth().phase, "ready");
  assert.equal(context.processor.options.channelCountMode, "explicit");
  assert.equal(context.processor.options.channelCount, 1);
  other.stop();
});

test("native worklet converts and clamps PCM across variable render block sizes", () => {
  let Processor;
  const messages = [];
  const scope = {
    Int16Array, currentTime: 2, sampleRate: 24000,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (data, transfers) => messages.push({ data, transfers }) }; } },
    registerProcessor(name, klass) { assert.equal(name, "interview-pcm"); Processor = klass; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../public/pcm-worklet.js"), "utf8"), scope);
  const processor = new Processor();
  assert.equal(processor.process([]), true);
  processor.process([[Float32Array.from([-2, -1, -0.5, 0, 0.5, 1, 2])]]);
  processor.process([[new Float32Array(1017).fill(0.25)]]);
  assert.equal(messages.length, 1);
  assert.deepEqual(Array.from(new Int16Array(messages[0].data.pcm).slice(0, 7)), [-32768, -32768, -16384, 0, 16383, 32767, 32767]);
  assert.equal(new Int16Array(messages[0].data.pcm)[1023], 8191);
  assert.equal(messages[0].transfers[0], messages[0].data.pcm);
  assert.equal(messages[0].data.endTime, 2 + 1017 / 24000);
  processor.process([[new Float32Array(2048)]]);
  assert.equal(messages.length, 3);
  assert.notEqual(messages[1].data.pcm, messages[2].data.pcm);
});

function adapterFixture({ fetchImpl, captureImpl } = {}) {
  const media = [], errors = [], states = [], ended = [];
  const clock = timers();
  let sessionEnded = 0;
  class Socket {
    static OPEN = 1; static CLOSING = 2; static all = [];
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = []; this.listeners = {}; Socket.all.push(this); }
    addEventListener(name, callback) { (this.listeners[name] ??= []).push(callback); }
    emit(name, event) { for (const callback of this.listeners[name] ?? []) callback(event); }
    open() { this.readyState = 1; this.emit("open", {}); }
    message(payload) { this.emit("message", { data: JSON.stringify(payload) }); }
    send(payload) {
      if (this.readyState !== 1 || (typeof payload !== "string" && this.throwOnBinary)) throw new Error("synthetic send failure");
      this.sent.push(payload);
      this.bufferedAmount += typeof payload === "string" ? payload.length : payload.byteLength;
    }
    close(code = 1000) {
      if (code !== 1000 && (code < 3000 || code > 4999)) throw new Error("Invalid browser close code");
      this.readyState = 3; this.closeCode = code; this.emit("close", { code });
    }
  }
  const client = compile("src/sessionClient.ts", { WebSocket: Socket, window: clock });
  const captureModule = compile("src/captureAdapter.ts", {
    WebSocket: Socket,
    window: { ...clock, interviewDesktop: {
      captureScreenSnapshot: captureImpl ?? (async () => ({
        image_data: "data:image/jpeg;base64," + "A".repeat(400000),
        source_id: "window:synthetic", captured_at: "2026-09-07T12:00:00.000Z",
      })),
    } },
    fetch: fetchImpl ?? (async () => ({ ok: true, status: 200 })),
    require: (name) => name === "./sessionClient" ? client : {
      getCaptureLabel: (speaker) => speaker,
      startLocalAudioCapture: (options) => {
        if (options.stream.fail) throw new Error("synthetic invalid stream");
        const item = { options, stops: 0, health: { phase: "ready", detail: "ready" } };
        media.push(item);
        return { getHealth: () => item.health, stop: () => item.stops++ };
      },
    },
  });
  const streams = { interviewer: { getTracks: () => [] }, candidate: { getTracks: () => [] } };
  const adapter = new captureModule.CaptureAdapter("https://example.test", streams, {
    onError: (message) => errors.push(message), onChannelChange: (speaker, state) => states.push({ speaker, ...state }),
    onMediaEnded: (speaker) => ended.push(speaker), onSessionEnded: () => sessionEnded++,
  });
  return { adapter, media, errors, states, ended, Socket, clock, sessionEnded: () => sessionEnded };
}

async function connect(fixture, suffix = "a") {
  const oldCount = fixture.Socket.all.length;
  const promise = fixture.adapter.connect({ interview_id: `test-${suffix}`, capture_token: "synthetic-capture-token" });
  const sockets = fixture.Socket.all.slice(oldCount);
  for (const socket of sockets) {
    socket.open();
    socket.message({ type: "session_ready", realtime_protocol: "realtime-interview-v5" });
    socket.message({ type: "capture_start" });
    socket.bufferedAmount = 0;
  }
  await promise;
  return sockets;
}

test("capture refuses old or missing protocol before transmitting audio", async () => {
  for (const protocol of [undefined, "realtime-interview-v4"]) {
    const fixture = adapterFixture();
    const connecting = fixture.adapter.connect({ interview_id: "old", capture_token: "synthetic-token" });
    const rejected = assert.rejects(connecting, /不匹配/);
    const socket = fixture.Socket.all[0];
    socket.open();
    socket.message({ type: "session_ready", realtime_protocol: protocol });
    socket.message({ type: "capture_start" });
    fixture.media[0].options.onChunk(new ArrayBuffer(2048));
    await rejected;
    assert.equal(socket.sent.filter((item) => typeof item !== "string").length, 0);
    assert.equal(fixture.clock.tasks.size, 0);
    assert(fixture.errors.some((error) => error.includes("不匹配")));
    fixture.adapter.dispose();
  }
});

test("a protocol change during reconnect stops retries and closes both channels", async () => {
  const fixture = adapterFixture();
  const [socket, other] = await connect(fixture);
  socket.close(4011);
  // Run the reconnect timer (the openChannel handshake installs a new timeout).
  const [id, retry] = Array.from(fixture.clock.tasks.entries()).at(-1);
  fixture.clock.tasks.delete(id);
  retry();
  const replacement = fixture.Socket.all.at(-1);
  replacement.open();
  replacement.message({ type: "session_ready", realtime_protocol: "realtime-interview-v4" });
  await new Promise(setImmediate);
  assert.equal(other.readyState, 3);
  assert.equal(fixture.clock.tasks.size, 0);
  fixture.adapter.dispose();
});

test("UI control client refuses mismatched protocol and never sends Start", async () => {
  const fixture = adapterFixture();
  const clientModule = compile("src/sessionClient.ts", { WebSocket: fixture.Socket, window: fixture.clock });
  const errors = [];
  const client = new clientModule.SessionClient("https://example.test", { interview_id: "old", session_token: "synthetic-token" }, {
    onEvent() {}, onConnectionChange() {}, onSessionUnavailable() {}, onError: (error) => errors.push(error),
  });
  const starting = client.start();
  const rejected = assert.rejects(starting, /不匹配/);
  const socket = fixture.Socket.all[0];
  socket.open();
  socket.message({ type: "session_ready", realtime_protocol: "realtime-interview-v4" });
  await rejected;
  assert.equal(client.send({ type: "start_interview" }), false);
  assert.equal(socket.sent.length, 1); // Authentication is the only allowed frame.
  assert.equal(fixture.clock.tasks.size, 0);
  assert(errors.some((error) => error.includes("不匹配")));
  client.stop(); fixture.adapter.dispose();
});

test("a half-open socket is closed after an unanswered heartbeat, healthy traffic keeps it alive", async () => {
  const fixture = adapterFixture();
  const clientModule = compile("src/sessionClient.ts", { WebSocket: fixture.Socket, window: fixture.clock });
  const socket = new fixture.Socket("https://example.test");
  socket.open();
  clientModule.monitorSocket(socket);
  const tick = Array.from(fixture.clock.tasks.values())[0];
  tick();
  assert.equal(JSON.parse(socket.sent.at(-1)).type, "ping");
  socket.message({ type: "pong" });
  tick();
  assert.equal(socket.readyState, 1);
  tick();
  assert.equal(socket.closeCode, 4000);
  assert.equal(fixture.clock.tasks.size, 0);
  fixture.adapter.dispose();
});

test("capture health preserves mute and interruption without a later ready overwrite", async () => {
  const fixture = adapterFixture();
  const [interviewer, candidate] = await connect(fixture);
  const controls = () => interviewer.sent.filter((item) => typeof item === "string").map(JSON.parse);
  const lastState = () => fixture.states.filter((state) => state.speaker === "interviewer").at(-1);
  fixture.media[0].options.onHealthChange({ phase: "muted", detail: "source unavailable" });
  assert.equal(lastState().phase, "muted");
  assert.equal(controls().at(-1).phase, "muted");
  fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  fixture.media[1].options.onChunk(new ArrayBuffer(2048));
  assert.equal(interviewer.sent.filter((item) => typeof item !== "string").length, 0);
  assert.equal(candidate.sent.filter((item) => typeof item !== "string").length, 1);
  fixture.media[0].options.onHealthChange({ phase: "interrupted", detail: "processing suspended" });
  assert.equal(lastState().phase, "interrupted");
  assert.equal(controls().at(-1).phase, "interrupted");
  fixture.media[0].options.onHealthChange({ phase: "ready", detail: "restored" });
  assert.equal(lastState().phase, "listening");
  assert.equal(controls().at(-1).phase, "ready");
  interviewer.message({ type: "capture_stop" });
  assert.equal(lastState().phase, "ready");
  assert(!controls().some((item) => item.type === "capture_ready"));
  fixture.adapter.dispose();
});

test("a pending HTTP screenshot upload never puts image bytes on either audio socket", async () => {
  let request, finishUpload;
  const fixture = adapterFixture({ fetchImpl: (url, options) => {
    request = { url, options };
    return new Promise((resolve) => { finishUpload = resolve; });
  } });
  const [interviewer, candidate] = await connect(fixture);
  interviewer.message({ type: "screen_capture_request", request_id: "screen-a" });
  await new Promise(setImmediate);
  assert.equal(request.url, "https://example.test/api/interviews/test-a/screenshots");
  assert.equal(request.options.headers.Authorization, "Bearer synthetic-capture-token");
  assert.equal(request.options.redirect, "error");
  assert.equal(JSON.parse(request.options.body).source_id, "window:synthetic");
  for (let index = 0; index < 5; index++) {
    fixture.media[0].options.onChunk(new ArrayBuffer(2048));
    fixture.media[1].options.onChunk(new ArrayBuffer(2048));
  }
  assert.equal(interviewer.sent.filter((item) => typeof item !== "string").length, 5);
  assert.equal(candidate.sent.filter((item) => typeof item !== "string").length, 5);
  assert(!interviewer.sent.some((item) => typeof item === "string" && item.includes("image_data")));
  assert.equal(fixture.errors.length, 0);
  finishUpload({ ok: true, status: 200 });
  await new Promise(setImmediate);
  fixture.adapter.dispose();
});

test("HTTP screenshot failures return a small correlated error without image bytes", async () => {
  const fixture = adapterFixture({ fetchImpl: async () => ({ ok: false, status: 413 }) });
  const [socket] = await connect(fixture);
  socket.message({ type: "screen_capture_request", request_id: "screen-error" });
  await new Promise(setImmediate);
  const payload = socket.sent.filter((item) => typeof item === "string").map(JSON.parse).find((item) => item.type === "screen_snapshot");
  assert.equal(payload.request_id, "screen-error");
  assert.match(payload.error, /413/);
  assert(!("image_data" in payload));
  assert.equal(fixture.errors.length, 1);
  fixture.adapter.dispose();
});

test("backpressure is visible once, preserves the other channel, and recovers even after capture_stop", async () => {
  const fixture = adapterFixture();
  const [interviewer, candidate] = await connect(fixture);
  interviewer.bufferedAmount = 300000;
  for (let index = 0; index < 5; index++) fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  fixture.media[1].options.onChunk(new ArrayBuffer(2048));
  assert.equal(fixture.errors.length, 1);
  assert.equal(interviewer.sent.filter((item) => typeof item !== "string").length, 0);
  assert.equal(candidate.sent.filter((item) => typeof item !== "string").length, 1);
  assert(interviewer.sent.some((item) => typeof item === "string" && JSON.parse(item).phase === "interrupted"));
  interviewer.message({ type: "capture_stop" });
  interviewer.bufferedAmount = 0;
  fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  const ready = interviewer.sent.filter((item) => typeof item === "string").map(JSON.parse).filter((item) => item.type === "capture_status").at(-1);
  assert.equal(ready.phase, "ready");
  interviewer.message({ type: "capture_start" });
  fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  assert.equal(interviewer.sent.filter((item) => typeof item !== "string").length, 1);
  fixture.adapter.dispose();
});

test("a binary send failure reports the loss and reconnects that socket", async () => {
  const fixture = adapterFixture();
  const [socket, other] = await connect(fixture);
  socket.throwOnBinary = true;
  fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  assert.equal(socket.closeCode, 4011);
  assert.equal(other.readyState, fixture.Socket.OPEN);
  assert.equal(fixture.errors.length, 1);
  assert.equal(fixture.states.filter((state) => state.speaker === "interviewer").at(-1).phase, "reconnecting");
  fixture.adapter.dispose();
});

test("replacing an ended channel keeps its socket, the other source, and the interview alive", async () => {
  const fixture = adapterFixture();
  const [socket, other] = await connect(fixture);
  fixture.media[0].options.onEnded();
  assert.deepEqual(fixture.ended, ["interviewer"]);
  assert.equal(fixture.media[1].stops, 0);
  assert.equal(socket.readyState, fixture.Socket.OPEN);
  fixture.adapter.replaceChannel("interviewer", { getTracks: () => [] });
  const oldCount = socket.sent.filter((item) => typeof item !== "string").length;
  fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  assert.equal(socket.sent.filter((item) => typeof item !== "string").length, oldCount);
  fixture.media[2].options.onChunk(new ArrayBuffer(2048));
  assert.equal(socket.sent.filter((item) => typeof item !== "string").length, oldCount + 1);
  assert.equal(fixture.media[1].stops, 0);
  assert.equal(other.readyState, fixture.Socket.OPEN);
  assert.equal(fixture.sessionEnded(), 0);
  fixture.adapter.dispose();
});

test("events from replaced sockets cannot end or mutate a new session", async () => {
  const fixture = adapterFixture();
  const [old] = await connect(fixture, "a");
  const [current] = await connect(fixture, "b");
  old.message({ type: "session_ended" });
  old.message({ type: "capture_stop" });
  fixture.media[0].options.onChunk(new ArrayBuffer(2048));
  assert.equal(fixture.sessionEnded(), 0);
  assert.equal(current.sent.filter((item) => typeof item !== "string").length, 1);
  fixture.adapter.dispose();
});

test("ending a session aborts an in-flight screenshot without reporting a new error", async () => {
  let signal;
  const fixture = adapterFixture({ fetchImpl: (_url, options) => {
    signal = options.signal;
    return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted"))));
  } });
  const [socket] = await connect(fixture);
  socket.message({ type: "screen_capture_request", request_id: "cancelled" });
  await new Promise(setImmediate);
  fixture.adapter.disconnectSession();
  await new Promise(setImmediate);
  assert.equal(signal.aborted, true);
  assert.equal(fixture.errors.length, 0);
  fixture.adapter.dispose();
});

test("a stalled native screenshot times out without opening an HTTP upload", async () => {
  let requests = 0;
  const fixture = adapterFixture({
    captureImpl: () => new Promise(() => {}),
    fetchImpl: async () => { requests++; return { ok: true, status: 200 }; },
  });
  const [socket] = await connect(fixture);
  socket.message({ type: "screen_capture_request", request_id: "native-timeout" });
  for (const callback of Array.from(fixture.clock.tasks.values())) callback();
  await new Promise(setImmediate);
  assert.equal(requests, 0);
  assert.equal(fixture.errors.length, 1);
  assert.match(fixture.errors[0], /超时/);
  fixture.adapter.dispose();
  assert.equal(fixture.clock.tasks.size, 0);
});
