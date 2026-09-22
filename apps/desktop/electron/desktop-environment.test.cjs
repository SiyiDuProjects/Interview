const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { loadDesktopEnvironment } = require("./desktop-environment.cjs");

function fixture(t, source) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "interview-env-test-"));
  const file = path.join(directory, ".env");
  fs.writeFileSync(file, source);
  t.after(() => {
    fs.unlinkSync(file);
    fs.rmdirSync(directory);
  });
  return file;
}

test("loads private desktop settings without importing backend secrets", (t) => {
  const file = fixture(t, '\uFEFFINTERVIEW_ACCESS_TOKEN="test-only-token"\nINTERVIEW_API_BASE_URL=https://example.test\nOPENAI_API_KEY=test-only-backend-secret\nNODE_OPTIONS=untrusted\n');
  const environment = {};
  loadDesktopEnvironment(file, environment);
  assert.deepEqual(environment, {
    INTERVIEW_ACCESS_TOKEN: "test-only-token",
    INTERVIEW_API_BASE_URL: "https://example.test",
  });
});

test("explicit process settings win, including an empty local access token", (t) => {
  const file = fixture(t, "INTERVIEW_ACCESS_TOKEN=test-only-token\nINTERVIEW_API_BASE_URL=https://example.test\n");
  const environment = { INTERVIEW_ACCESS_TOKEN: "", INTERVIEW_API_BASE_URL: "http://127.0.0.1:8000" };
  loadDesktopEnvironment(file, environment);
  assert.equal(environment.INTERVIEW_ACCESS_TOKEN, "");
  assert.equal(environment.INTERVIEW_API_BASE_URL, "http://127.0.0.1:8000");
});

test("a missing local file is optional", (t) => {
  const file = fixture(t, "");
  const environment = {};
  loadDesktopEnvironment(path.join(path.dirname(file), "missing.env"), environment);
  assert.deepEqual(environment, {});
});
