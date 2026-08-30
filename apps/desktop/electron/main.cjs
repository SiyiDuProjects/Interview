const path = require("node:path");
const { pathToFileURL } = require("node:url");
const os = require("node:os");
const http = require("node:http");
const nodeNet = require("node:net");
const fs = require("node:fs");
const { spawn } = require("node:child_process");
const {
  app,
  BrowserWindow,
  desktopCapturer,
  ipcMain,
  Menu,
  net: electronNet,
  nativeImage,
  session,
  shell,
  Tray,
} = require("electron");

const WINDOW_TITLE = "Sage";
const DEFAULT_API_PORT = 8000;
const DEFAULT_REMOTE_API_BASE_URL = "https://interview.reachard.co";
const FALLBACK_API_PORTS = [8000, 8001];
const API_START_TIMEOUT_MS = 15000;
const API_HEALTH_MAX_BYTES = 64 * 1024;
const DEV_RENDERER_URL = "http://127.0.0.1:5173/";
const ALLOWED_MEDIA_PERMISSIONS = new Set(["media", "display-capture", "microphone"]);
const TRAY_ICON_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAALoSURBVFhH1Vc9aFUxFO7o2LH0Vp7g5qRbt5Pm+oMgWoVCQZGCIlIUng7yBJEiguJQHYpCC6KCFGqx1KVdtINi0aUOgkMFUYcnahHpoJ0iX25zSU6S9/IuOvjBN+Xk/CXny71dXf8rst3Un+UkbHKbv4oaUU+vpNOZFAt9uVBRSrGcDVCjRrSN+6iEGlF3ltNYlotfXrD2vIPEuc9kFK0VPwKOk6kTlzTMfbfFVkkjFasOEl3kMaJAcO7AUIwcV2O3J9Tii+fq5ZsVhxPTD9W+Uye9PRZv8lgeNtvuVb5r6Iiaf/ZUpWD100d18MwoD66Ji8xjlsCFCZ05qv69scHjtATs0SnuC8X17qGdPLYGWsQ3gGitja9ra+rBk3k1evWKGjpf15yafaQr5wh1IsvFHI9tqvdaD8LJz/V1XdX4/Xtq+/69no3hscYFnaDB2/erng3odQHiwY2q8sTlS04X+o8OezbQCCcBKFjAqBIxCTZ2HDrg2WS5aJbBoXbcoCoRDCNpgGPjNoblMeBh4YudEEFxEa9NTeq7YuPirXHPvqRRyD5Jg95ihNADBLo791h9+f7NCcaBTvD9NjNJdZ0AxIEvcqJKjFqKHsAGoxs6eycBI89oBV+0ifby1oZg9AFd4j5CxOQVdyAnwRcN4cyeawDJIBA0YXJ2Rh2un/X2pRBvjpmCHr5ouPT6lRM8pbWpxOXfHEQtwx+4Afi52XSC8/WqhOrWiLbYCQTfATuBliPVIb33AKLAjUBbVHAX8DJyG5s4Howpnu6IBBeUNOgkACArbshlFSOGceSvHOy4EEV1QIoVHlsj1oWZxQUniVRgUrivIoFA9QaxV/HcjetJWgCYLkWebfcVDCHLxXRgoz5TTALe+BDwQYKqo2cvxbJz82OAUSwJOxnzNQS2Vb8ieDeP1RLQas9RNeIHpX3lIWiZlmIp4DSF71peuE4ARziW2HejTfw/ljr/L6D/HQaoUfwzOhSdtvoPkf0OHX9hJAwAAAAASUVORK5CYII=";

const writableRoot = path.join(os.tmpdir(), "interview-copilot-electron");
let apiProcess = null;
let apiPort = DEFAULT_API_PORT;
let mainWindow = null;
let tray = null;
let isQuitting = false;
let trustedRendererUrl = "";

function isLoopbackHostname(hostname) {
  return ["127.0.0.1", "localhost", "::1", "[::1]"].includes(hostname);
}

function validatedApiUrl(value) {
  const url = new URL(value);
  if (url.username || url.password) {
    throw new Error("API 地址不能包含用户名或密码。");
  }
  if (isLoopbackHostname(url.hostname)) {
    if (!["http:", "https:"].includes(url.protocol)) {
      throw new Error("本地 API 地址必须使用 http 或 https。");
    }
  } else if (url.protocol !== "https:") {
    throw new Error("远程 API 地址必须使用 https。");
  }
  return url;
}

function configuredApiBaseUrl() {
  const value = (
    process.env.INTERVIEW_API_BASE_URL ||
    process.env.VITE_API_BASE_URL ||
    DEFAULT_REMOTE_API_BASE_URL
  ).trim();
  return validatedApiUrl(value).toString();
}

function resolveApiEndpoint(apiBaseUrl, pathname) {
  const requestedUrl = validatedApiUrl(apiBaseUrl || configuredApiBaseUrl());
  const configuredUrl = validatedApiUrl(configuredApiBaseUrl());
  if (requestedUrl.origin !== configuredUrl.origin) {
    throw new Error("拒绝向未配置的 API 地址转发桌面端凭据。");
  }

  return new URL(pathname, `${requestedUrl.origin}/`).toString();
}

function buildApiHeaders() {
  const headers = { Accept: "application/json" };
  const accessToken = process.env.INTERVIEW_ACCESS_TOKEN?.trim();
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }
  return headers;
}

async function readApiError(response) {
  try {
    const payload = await response.json();
    return payload.detail || payload.error || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function configureIpcHandlers() {
  ipcMain.handle("interview:create", async (event, apiBaseUrl) => {
    assertTrustedIpcSender(event);
    const response = await electronNet.fetch(resolveApiEndpoint(apiBaseUrl, "/api/interviews"), {
      method: "POST",
      headers: {
        ...buildApiHeaders(),
        "Content-Type": "application/json",
      },
      body: "{}",
    });

    if (!response.ok) {
      throw new Error(await readApiError(response));
    }

    const payload = await response.json();
    if (
      typeof payload.interview_id !== "string" ||
      typeof payload.session_token !== "string" ||
      typeof payload.capture_token !== "string"
    ) {
      throw new Error("创建面试返回了无效会话。");
    }
    return {
      interview_id: payload.interview_id,
      session_token: payload.session_token,
      capture_token: payload.capture_token,
    };
  });

  ipcMain.handle("interview:end", async (event, apiBaseUrl, interviewId, sessionToken) => {
    assertTrustedIpcSender(event);
    if (typeof interviewId !== "string" || !interviewId.trim()) {
      throw new Error("缺少面试会话 ID。");
    }
    if (typeof sessionToken !== "string" || !sessionToken.trim()) {
      throw new Error("缺少面试会话令牌。");
    }

    const response = await electronNet.fetch(
      resolveApiEndpoint(apiBaseUrl, `/api/interviews/${encodeURIComponent(interviewId)}`),
      {
        method: "DELETE",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${sessionToken}`,
        },
      },
    );

    if (!response.ok && response.status !== 404) {
      throw new Error(await readApiError(response));
    }
    return { ok: true };
  });

  ipcMain.handle("capture:initialize", async (event) => {
    assertTrustedIpcSender(event);
    await event.senderFrame.executeJavaScript(
      "window.dispatchEvent(new Event('sage:capture-initialize'))",
      true,
    );
  });
}

function configuredApiBaseUrlSource() {
  if (process.env.INTERVIEW_API_BASE_URL?.trim()) {
    return "INTERVIEW_API_BASE_URL";
  }
  if (process.env.VITE_API_BASE_URL?.trim()) {
    return "VITE_API_BASE_URL";
  }
  return "default";
}

function isLocalApiUrl(value) {
  if (!value) {
    return false;
  }
  try {
    const url = validatedApiUrl(value);
    return isLoopbackHostname(url.hostname);
  } catch {
    return false;
  }
}

function isRemoteApiUrl(value) {
  if (!value) {
    return false;
  }
  try {
    const url = validatedApiUrl(value);
    return Boolean(url.hostname && !isLoopbackHostname(url.hostname));
  } catch {
    return false;
  }
}

function configuredApiPort() {
  const configuredUrl = configuredLocalApiBaseUrl() || configuredApiBaseUrl();
  const match = configuredUrl.match(/127\.0\.0\.1:(\d+)|localhost:(\d+)/);
  if (match) {
    return Number(match[1] || match[2]);
  }
  const configuredPort = Number(process.env.INTERVIEW_API_PORT || "");
  return Number.isFinite(configuredPort) && configuredPort > 0 ? configuredPort : null;
}

function configuredLocalApiBaseUrl() {
  const explicitUrl = (process.env.INTERVIEW_API_BASE_URL || "").trim();
  return isLocalApiUrl(explicitUrl) ? explicitUrl : "";
}

app.setPath("userData", writableRoot);
app.commandLine.appendSwitch("disk-cache-dir", path.join(writableRoot, "cache"));
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");

function getRendererUrl() {
  if (app.isPackaged) {
    return "";
  }

  const arg = process.argv.find((value) => value.startsWith("--renderer-url="));
  if (!arg) {
    return "";
  }

  try {
    const candidate = new URL(arg.slice("--renderer-url=".length));
    if (
      candidate.href === DEV_RENDERER_URL &&
      !candidate.username &&
      !candidate.password
    ) {
      return candidate.href;
    }
  } catch {
    // Invalid renderer URLs are rejected below.
  }
  throw new Error(`拒绝加载非受信任的 renderer URL；开发地址必须是 ${DEV_RENDERER_URL}`);
}

function packagedRendererUrl() {
  return pathToFileURL(path.join(__dirname, "..", "dist", "index.html")).href;
}

function isTrustedRendererUrl(value) {
  if (typeof value !== "string" || !value || !trustedRendererUrl) {
    return false;
  }

  try {
    const candidate = new URL(value);
    const trusted = new URL(trustedRendererUrl);
    candidate.hash = "";
    trusted.hash = "";
    return candidate.href === trusted.href;
  } catch {
    return false;
  }
}

function isTrustedMainFrame(webContents, frame, fallbackUrl = "") {
  return Boolean(
    mainWindow &&
      !mainWindow.isDestroyed() &&
      webContents === mainWindow.webContents &&
      frame === mainWindow.webContents.mainFrame &&
      isTrustedRendererUrl(frame?.url || fallbackUrl || webContents?.getURL()),
  );
}

function assertTrustedIpcSender(event) {
  if (!isTrustedMainFrame(event.sender, event.senderFrame)) {
    throw new Error("拒绝来自非受信任页面的桌面端请求。");
  }
}

function readApiHealth(port) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) {
        return;
      }
      settled = true;
      resolve(value);
    };
    const request = http.get(`http://127.0.0.1:${port}/health`, (response) => {
      let body = "";
      let bodyBytes = 0;
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        bodyBytes += Buffer.byteLength(chunk, "utf8");
        if (bodyBytes > API_HEALTH_MAX_BYTES) {
          response.destroy();
          finish(null);
          return;
        }
        body += chunk;
      });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          finish(null);
          return;
        }
        try {
          finish(JSON.parse(body));
        } catch {
          finish(null);
        }
      });
      response.on("error", () => finish(null));
    });
    request.on("error", () => finish(null));
    request.setTimeout(1200, () => {
      request.destroy();
      finish(null);
    });
  });
}

async function isApiCompatible(port) {
  const health = await readApiHealth(port);
  return health?.status === "ok" && health?.realtime_protocol === "realtime-interview-v4";
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = nodeNet.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

async function waitForApiReady(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isApiCompatible(apiPort)) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

async function ensureApiServer() {
  const configuredUrl = configuredApiBaseUrl();
  const configuredSource = configuredApiBaseUrlSource();
  const localApiUrl = configuredLocalApiBaseUrl();
  if (!localApiUrl) {
    process.env.INTERVIEW_API_BASE_URL = isRemoteApiUrl(configuredUrl) ? configuredUrl : DEFAULT_REMOTE_API_BASE_URL;
    process.env.VITE_API_BASE_URL = process.env.INTERVIEW_API_BASE_URL;
    process.env.INTERVIEW_LOCAL_API_ENABLED = "0";
    console.log("[desktop] using remote api", {
      baseUrl: process.env.INTERVIEW_API_BASE_URL,
      source: configuredSource,
    });
    return;
  }

  console.log("[desktop] using local api", {
    baseUrl: localApiUrl,
    source: "INTERVIEW_API_BASE_URL",
  });
  process.env.INTERVIEW_LOCAL_API_ENABLED = "1";

  const candidatePorts = [configuredApiPort(), ...FALLBACK_API_PORTS].filter(
    (port, index, ports) => typeof port === "number" && ports.indexOf(port) === index,
  );

  for (const port of candidatePorts) {
    if (await isApiCompatible(port)) {
      apiPort = port;
      process.env.INTERVIEW_API_BASE_URL = `http://127.0.0.1:${apiPort}`;
      return;
    }
  }

  apiPort = await findFreePort();
  process.env.INTERVIEW_API_BASE_URL = `http://127.0.0.1:${apiPort}`;

  const serverDir = path.join(__dirname, "..", "..", "server");
  const localPython =
    process.platform === "win32"
      ? path.join(serverDir, ".venv", "Scripts", "python.exe")
      : path.join(serverDir, ".venv", "bin", "python");
  const pythonCommand =
    process.env.INTERVIEW_PYTHON ||
    (fs.existsSync(localPython) ? localPython : process.platform === "win32" ? "python" : "python3");

  apiProcess = spawn(
    pythonCommand,
    ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(apiPort)],
    {
      cwd: serverDir,
      stdio: "pipe",
      windowsHide: true,
    },
  );

  apiProcess.stdout.on("data", (chunk) => {
    process.stdout.write(`[desktop-api] ${chunk}`);
  });
  apiProcess.stderr.on("data", (chunk) => {
    process.stderr.write(`[desktop-api] ${chunk}`);
  });
  apiProcess.on("error", (error) => {
    console.error("[desktop] failed to start local api", error);
  });
  apiProcess.on("exit", (code, signal) => {
    console.error("[desktop] local api exited", { code, signal });
    apiProcess = null;
  });

  const ready = await waitForApiReady(API_START_TIMEOUT_MS);
  if (!ready) {
    console.error("[desktop] local api did not become healthy in time");
  }
}

async function configureSession() {
  const ses = session.defaultSession;

  ses.setPermissionCheckHandler((webContents, permission, _requestingOrigin, details) => {
    const frame = webContents?.mainFrame;
    return Boolean(
      ALLOWED_MEDIA_PERMISSIONS.has(permission) &&
        details?.isMainFrame !== false &&
        isTrustedMainFrame(webContents, frame, details?.requestingUrl),
    );
  });

  ses.setPermissionRequestHandler((webContents, permission, callback, details) => {
    const frame = webContents?.mainFrame;
    callback(
      Boolean(
        ALLOWED_MEDIA_PERMISSIONS.has(permission) &&
          details?.isMainFrame !== false &&
          isTrustedMainFrame(webContents, frame, details?.requestingUrl),
      ),
    );
  });

  ses.setDisplayMediaRequestHandler(
    async (request, callback) => {
      if (!isTrustedMainFrame(mainWindow?.webContents, request.frame)) {
        callback({});
        return;
      }

      try {
        const sources = await desktopCapturer.getSources({
          types: ["screen"],
          thumbnailSize: { width: 0, height: 0 },
        });

        if (sources.length === 0) {
          callback({});
          return;
        }

        callback({
          video: request.videoRequested ? sources[0] : undefined,
          audio: request.audioRequested ? "loopback" : undefined,
        });
      } catch {
        callback({});
      }
    },
    { useSystemPicker: false },
  );
}

function stopApiServer() {
  const processToStop = apiProcess;
  apiProcess = null;
  if (processToStop && !processToStop.killed) {
    processToStop.kill();
  }
}

function hideMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.hide();
  }
}

async function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    await createMainWindow();
  }

  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  if (tray) {
    return;
  }

  const icon = nativeImage.createFromDataURL(TRAY_ICON_DATA_URL);
  if (icon.isEmpty()) {
    throw new Error("无法创建系统托盘图标。");
  }

  tray = new Tray(icon.resize({ width: 16, height: 16, quality: "best" }));
  tray.setToolTip(`${WINDOW_TITLE} 面试助手`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: `显示 ${WINDOW_TITLE}`,
        click: () => void showMainWindow(),
      },
      {
        label: `隐藏 ${WINDOW_TITLE}`,
        click: hideMainWindow,
      },
      { type: "separator" },
      {
        label: "退出",
        click: () => app.quit(),
      },
    ]),
  );
  tray.on("click", () => void showMainWindow());
}

async function createMainWindow() {
  const preloadPath = path.join(__dirname, "preload.cjs");
  const rendererUrl = getRendererUrl();
  trustedRendererUrl = rendererUrl || packagedRendererUrl();

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 560,
    autoHideMenuBar: true,
    title: WINDOW_TITLE,
    backgroundColor: "#f6f3ed",
    webPreferences: {
      preload: preloadPath,
      additionalArguments: [`--interview-api-base-url=${configuredApiBaseUrl()}`],
      backgroundThrottling: false,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      if (new URL(url).protocol === "https:") {
        void shell.openExternal(url);
      }
    } catch {
      // Always deny malformed and non-HTTPS external URLs.
    }
    return { action: "deny" };
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isTrustedRendererUrl(url)) {
      event.preventDefault();
    }
  });

  mainWindow.webContents.on("will-redirect", (event, url) => {
    if (!isTrustedRendererUrl(url)) {
      event.preventDefault();
    }
  });

  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    console.error("[desktop] renderer load failed", {
      errorCode,
      errorDescription,
      validatedURL,
    });
  });

  mainWindow.webContents.on("render-process-gone", (_event, details) => {
    console.error("[desktop] renderer process gone", details);
  });

  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    if (level >= 2) {
      console.error("[desktop] renderer console", {
        level,
        message,
        line,
        sourceId,
      });
    }
  });

  if (rendererUrl) {
    await mainWindow.loadURL(rendererUrl);
    return;
  }

  await mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      void showMainWindow();
    }
  });

  app.whenReady().then(async () => {
    await ensureApiServer();
    configureIpcHandlers();
    await configureSession();
    createTray();
    await createMainWindow();

    app.on("activate", () => {
      void showMainWindow();
    });
  });
}

app.on("before-quit", () => {
  isQuitting = true;
  stopApiServer();
});

app.on("window-all-closed", () => {
  // The capture host stays alive in the tray and can recreate its shared UI.
});

app.on("will-quit", () => {
  if (tray) {
    tray.destroy();
    tray = null;
  }
});
