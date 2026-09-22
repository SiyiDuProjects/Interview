const fs = require("node:fs");
const path = require("node:path");
const { parseEnv } = require("node:util");

function loadDesktopEnvironment(
  envPath = path.resolve(__dirname, "../../..", ".env"),
  environment = process.env,
) {
  let source;
  try {
    source = fs.readFileSync(envPath, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw new Error("无法读取桌面端本地配置，请检查 .env 文件权限。");
  }
  const values = parseEnv(source.replace(/^\uFEFF/, ""));
  for (const key of ["INTERVIEW_API_BASE_URL", "INTERVIEW_ACCESS_TOKEN"]) {
    if (environment[key] === undefined && values[key] !== undefined) {
      environment[key] = values[key];
    }
  }
}

module.exports = { loadDesktopEnvironment };
