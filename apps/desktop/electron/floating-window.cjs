const FLOATING_WINDOW_OPTIONS = {
  width: 620,
  height: 440,
  frame: false,
  transparent: true,
  backgroundColor: "#00000000",
  resizable: false,
  maximizable: false,
  fullscreenable: false,
  hasShadow: false,
  show: false,
  alwaysOnTop: true,
};

function createFloatingControls(window, screen) {
  let collapsed = false;
  let codeExpanded = false;
  const expanded = { width: FLOATING_WINDOW_OPTIONS.width, height: FLOATING_WINDOW_OPTIONS.height };
  return {
    getState() { return { collapsed, codeExpanded, pinned: window.isAlwaysOnTop() }; },
    setCodeExpanded(value) {
      if (typeof value !== "boolean") throw new TypeError("codeExpanded must be boolean");
      if (value === codeExpanded) return codeExpanded;
      codeExpanded = value;
      expanded.width = value ? 1060 : FLOATING_WINDOW_OPTIONS.width;
      expanded.height = value ? 740 : FLOATING_WINDOW_OPTIONS.height;
      if (!collapsed) {
        const bounds = window.getBounds();
        const area = screen.getDisplayMatching(bounds).workArea;
        const width = Math.min(expanded.width, area.width);
        const height = Math.min(expanded.height, area.height);
        window.setBounds({
          x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - width)),
          y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - height)), width, height,
        });
      }
      return codeExpanded;
    },
    setCollapsed(value) {
      if (typeof value !== "boolean") throw new TypeError("collapsed must be boolean");
      if (value === collapsed) return collapsed;
      const bounds = window.getBounds();
      if (value) {
        expanded.width = bounds.width;
        expanded.height = bounds.height;
      }
      const area = screen.getDisplayMatching(bounds).workArea;
      const width = Math.min(value ? 390 : expanded.width, area.width);
      const height = Math.min(value ? 58 : expanded.height, area.height);
      window.setBounds({
        x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - width)),
        y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - height)),
        width,
        height,
      });
      collapsed = value;
      return collapsed;
    },
    setPinned(value) {
      if (typeof value !== "boolean") throw new TypeError("pinned must be boolean");
      window.setAlwaysOnTop(value);
      return window.isAlwaysOnTop();
    },
    hide() { window.hide(); },
  };
}

module.exports = { FLOATING_WINDOW_OPTIONS, createFloatingControls };
