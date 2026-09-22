function createRendererRecovery({ maxAttempts = 2, windowMs = 60_000 } = {}) {
  let attempts = [];
  let notice = "";
  return {
    recordCrash(now = Date.now()) {
      attempts = attempts.filter((at) => now - at < windowMs);
      if (attempts.length >= maxAttempts) {
        notice = "界面多次异常退出，已停止自动恢复。请退出 Sage 后重新打开，并确认采集状态。";
        return { retry: false, notice };
      }
      attempts.push(now);
      notice = "界面刚刚自动恢复，期间可能缺失音频。请确认两路采集状态，并补充遗漏内容。";
      return { retry: true, notice };
    },
    getNotice() { return notice; },
  };
}

module.exports = { createRendererRecovery };
