const MAX_SCREENSHOT_BYTES = 4_500_000;
const MAX_SCREENSHOT_DIMENSION = 2560;

function createScreenCaptureService(desktopCapturer, screen) {
  let selectedSourceId = null;

  async function getSources(thumbnailSize, types = ["screen", "window"]) {
    return desktopCapturer.getSources({ types, thumbnailSize, fetchWindowIcons: false });
  }

  async function listSources() {
    const sources = await getSources({ width: 320, height: 200 });
    if (!sources.some((source) => source.id === selectedSourceId)) selectedSourceId = null;
    return sources.map((source) => ({
      id: source.id,
      name: source.name,
      displayId: source.display_id || "",
      thumbnailDataUrl: source.thumbnail.isEmpty() ? "" : source.thumbnail.toDataURL(),
      selected: source.id === selectedSourceId,
    }));
  }

  async function selectSource(sourceId) {
    if (typeof sourceId !== "string" || !sourceId || sourceId.length > 1024) {
      throw new Error("请选择有效的屏幕或窗口。");
    }
    const sources = await getSources({ width: 0, height: 0 });
    const source = sources.find((candidate) => candidate.id === sourceId);
    if (!source) throw new Error("选择的屏幕或窗口已不可用，请重新选择。");
    selectedSourceId = source.id;
    return { id: source.id, name: source.name };
  }

  async function captureSnapshot() {
    // Selection is explicit and never silently falls back to a different screen.
    const sourceId = selectedSourceId;
    if (!sourceId) throw new Error("请先在设备详情中选择要看的屏幕或窗口。");
    const sources = await getSources({ width: MAX_SCREENSHOT_DIMENSION, height: MAX_SCREENSHOT_DIMENSION });
    if (selectedSourceId !== sourceId) throw new Error("截图来源已改变，请重新看题。");
    const source = sources.find((candidate) => candidate.id === sourceId);
    if (!source || source.thumbnail.isEmpty()) {
      throw new Error("选择的屏幕或窗口无法截图，请检查窗口是否仍然可见并重新选择。");
    }
    const capturedAt = new Date().toISOString();
    for (const quality of [85, 72, 60, 48]) {
      const jpeg = source.thumbnail.toJPEG(quality);
      if (jpeg.length > 0 && jpeg.length <= MAX_SCREENSHOT_BYTES) {
        return {
          image_data: `data:image/jpeg;base64,${jpeg.toString("base64")}`,
          source_id: source.id,
          captured_at: capturedAt,
        };
      }
    }
    throw new Error("截图过大，请选择题目所在的单个窗口后重试。");
  }

  async function getAudioCaptureSource() {
    // System loopback is independent of the user-selected screenshot window.
    const sources = await getSources({ width: 0, height: 0 }, ["screen"]);
    const primaryId = String(screen.getPrimaryDisplay().id);
    const source = sources.find((candidate) => String(candidate.display_id) === primaryId);
    if (source) return source;
    if (sources.length === 1) return sources[0];
    throw new Error("无法确定系统音频采集使用的显示器，请检查系统屏幕权限。");
  }

  return { listSources, selectSource, captureSnapshot, getAudioCaptureSource };
}

module.exports = { createScreenCaptureService };
