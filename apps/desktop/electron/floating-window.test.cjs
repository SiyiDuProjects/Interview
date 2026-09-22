const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createFloatingControls, FLOATING_WINDOW_OPTIONS } = require("./floating-window.cjs");

function fixture(workArea = { x: 0, y: 0, width: 1920, height: 1080 }) {
  const window = {
    bounds: { x: 1200, y: 600, width: 620, height: 440 },
    pinned: true,
    hidden: false,
    getBounds() { return { ...this.bounds }; },
    setBounds(bounds) { this.bounds = bounds; },
    setAlwaysOnTop(value) { this.pinned = value; },
    isAlwaysOnTop() { return this.pinned; },
    hide() { this.hidden = true; },
  };
  return { window, controls: createFloatingControls(window, { getDisplayMatching: () => ({ workArea }) }) };
}

test("collapse changes native bounds and expanding restores size after moving", () => {
  const { window, controls } = fixture();
  controls.setCollapsed(true);
  assert.deepEqual(window.bounds, { x: 1200, y: 600, width: 390, height: 58 });
  controls.setCollapsed(true);
  window.bounds.x = 1500;
  window.bounds.y = 950;
  controls.setCollapsed(false);
  assert.deepEqual(window.bounds, { x: 1300, y: 640, width: 620, height: 440 });
});

test("restore remains on a monitor with negative coordinates", () => {
  const { window, controls } = fixture({ x: -1280, y: -200, width: 1280, height: 720 });
  window.bounds = { x: -200, y: 450, width: 620, height: 440 };
  controls.setCollapsed(true);
  controls.setCollapsed(false);
  assert.deepEqual(window.bounds, { x: -620, y: 80, width: 620, height: 440 });
});

test("pin and hide are window operations and never end the interview", () => {
  const { window, controls } = fixture();
  assert.equal(controls.setPinned(false), false);
  assert.deepEqual(controls.getState(), { collapsed: false, codeExpanded: false, pinned: false });
  controls.hide();
  assert.equal(window.hidden, true);
  assert.throws(() => controls.setPinned("false"), TypeError);
  assert.throws(() => controls.setCollapsed({}), TypeError);
});

test("native options retain transparent frameless behavior", () => {
  assert.equal(FLOATING_WINDOW_OPTIONS.transparent, true);
  assert.equal(FLOATING_WINDOW_OPTIONS.frame, false);
  assert.equal(FLOATING_WINDOW_OPTIONS.backgroundColor, "#00000000");
  assert.equal(FLOATING_WINDOW_OPTIONS.resizable, false);
});

test("code workspace expands, survives collapse, and restores compact bounds", () => {
  const { window, controls } = fixture();
  controls.setCodeExpanded(true);
  assert.deepEqual(window.bounds, { x: 860, y: 340, width: 1060, height: 740 });
  controls.setCollapsed(true);
  controls.setCollapsed(false);
  assert.equal(window.bounds.width, 1060);
  controls.setCodeExpanded(false);
  assert.equal(window.bounds.width, 620);
  assert.equal(window.bounds.height, 440);
  assert.throws(() => controls.setCodeExpanded("true"), TypeError);
});

test("code workspace stays in a small display with negative coordinates", () => {
  const { window, controls } = fixture({ x: -800, y: -100, width: 800, height: 600 });
  controls.setCodeExpanded(true);
  assert.deepEqual(window.bounds, { x: -800, y: -100, width: 800, height: 600 });
  controls.setCollapsed(true);
  controls.setCodeExpanded(false);
  controls.setCollapsed(false);
  assert.equal(window.bounds.width, 620);
  assert.equal(window.bounds.height, 440);
});
