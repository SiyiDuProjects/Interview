const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createScreenCaptureService } = require("./screen-capture.cjs");
const { createRendererRecovery } = require("./renderer-recovery.cjs");

function source(id, displayId = "") {
  return { id, display_id: displayId, name: `Source ${id}`, thumbnail: {
    isEmpty: () => false, toDataURL: () => `data:image/png;base64,${id}`,
    toJPEG: () => Buffer.from(`synthetic-${id}`),
  } };
}

function fixture() {
  const state = { sources: [source("screen:second", "2"), source("screen:primary", "1"), source("window:question")] };
  const service = createScreenCaptureService({ getSources: async ({ types }) => state.sources.filter((item) => types.some((type) => item.id.startsWith(type))) }, {
    getPrimaryDisplay: () => ({ id: 1 }),
  });
  return { state, service };
}

test("screenshots require explicit source selection and audio remains on its independent source", async () => {
  const { service } = fixture();
  const choices = await service.listSources();
  assert(choices.every((item) => !item.selected));
  await assert.rejects(service.captureSnapshot(), /选择/);
  await service.selectSource("window:question");
  const before = Date.now();
  const image = await service.captureSnapshot();
  assert.equal(image.source_id, "window:question");
  assert(Date.parse(image.captured_at) >= before);
  assert(Date.parse(image.captured_at) <= Date.now());
  assert.equal(Buffer.from(image.image_data.split(",")[1], "base64").toString(), "synthetic-window:question");
  assert.equal((await service.getAudioCaptureSource()).id, "screen:primary");
});

test("a vanished selection fails instead of silently sending another screen", async () => {
  const { state, service } = fixture();
  await service.selectSource("window:question");
  state.sources = state.sources.filter((item) => item.id !== "window:question");
  await assert.rejects(service.captureSnapshot(), /无法截图/);
  await assert.rejects(service.selectSource("window:untrusted"), /不可用/);
  assert((await service.listSources()).every((item) => !item.selected));
});

test("a source change while capture is pending invalidates the old image", async () => {
  let finish;
  const sources = [source("screen:a", "1"), source("screen:b", "2")];
  const service = createScreenCaptureService({ getSources: async ({ thumbnailSize }) => {
    if (thumbnailSize.width > 1000) return new Promise((resolve) => { finish = resolve; });
    return sources;
  } }, { getPrimaryDisplay: () => ({ id: 1 }) });
  await service.selectSource("screen:a");
  const pending = service.captureSnapshot();
  await service.selectSource("screen:b");
  finish(sources);
  await assert.rejects(pending, /来源已改变/);
});

test("oversized native images fail before creating an upload payload", async () => {
  const huge = source("window:huge");
  huge.thumbnail.toJPEG = () => Buffer.alloc(4_500_001);
  const service = createScreenCaptureService({ getSources: async () => [huge] }, { getPrimaryDisplay: () => ({ id: 1 }) });
  await service.selectSource("window:huge");
  await assert.rejects(service.captureSnapshot(), /截图过大/);
});

test("renderer recovery is bounded and leaves a user-visible explanation", () => {
  const recovery = createRendererRecovery();
  assert.equal(recovery.recordCrash(1000).retry, true);
  assert.equal(recovery.recordCrash(2000).retry, true);
  assert.equal(recovery.recordCrash(3000).retry, false);
  assert.match(recovery.getNotice(), /停止自动恢复/);
  assert.equal(recovery.recordCrash(63000).retry, true);
  assert.match(recovery.getNotice(), /缺失音频/);
});
