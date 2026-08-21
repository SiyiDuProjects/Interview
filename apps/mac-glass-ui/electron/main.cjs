const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const net = require("node:net");
const fs = require("node:fs");
const { spawn } = require("node:child_process");
const { app, BrowserWindow, desktopCapturer, globalShortcut, ipcMain, nativeTheme, session, shell } = require("electron");

const WINDOW_TITLE = "Sage Glass";
const DEFAULT_API_PORT = 8000;
const DEFAULT_REMOTE_API_BASE_URL = "https://interview.reachard.co";
const FALLBACK_API_PORTS = [8000, 8001];
const API_START_TIMEOUT_MS = 15000;
const REQUIRED_REALTIME_PROTOCOL = "realtime-text-events-v2";

const writableRoot = path.join(os.tmpdir(), "interview-mac-glass-ui");
let mainWindow = null;
let apiProcess = null;
let apiPort = DEFAULT_API_PORT;
let privacyModeEnabled = false;
let privacyModeUpdate = Promise.resolve();

function configuredApiBaseUrl() {
  return (process.env.INTERVIEW_API_BASE_URL || process.env.VITE_API_BASE_URL || DEFAULT_REMOTE_API_BASE_URL).trim();
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

function configuredLocalApiBaseUrl() {
  const explicitUrl = (process.env.INTERVIEW_API_BASE_URL || "").trim();
  return isLocalApiUrl(explicitUrl) ? explicitUrl : "";
}

function isLocalApiUrl(value) {
  if (!value) {
    return false;
  }
  try {
    const url = new URL(value);
    return ["127.0.0.1", "localhost"].includes(url.hostname);
  } catch {
    return false;
  }
}

function isRemoteApiUrl(value) {
  if (!value) {
    return false;
  }
  try {
    const url = new URL(value);
    return Boolean(url.protocol && url.hostname && !isLocalApiUrl(value));
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

function stopApiProcess() {
  if (!apiProcess) {
    return;
  }
  apiProcess.kill();
  apiProcess = null;
}

app.setPath("userData", writableRoot);
app.commandLine.appendSwitch("disk-cache-dir", path.join(writableRoot, "cache"));
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");

function getRendererUrl() {
  const arg = process.argv.find((value) => value.startsWith("--renderer-url="));
  return arg ? arg.slice("--renderer-url=".length) : "";
}

function readApiHealth(port) {
  return new Promise((resolve) => {
    const request = http.get(`http://127.0.0.1:${port}/health`, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          resolve(null);
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch {
          resolve(null);
        }
      });
    });
    request.on("error", () => resolve(null));
    request.setTimeout(1200, () => {
      request.destroy();
      resolve(null);
    });
  });
}

async function isApiCompatible(port) {
  const health = await readApiHealth(port);
  return Boolean(health?.status === "ok" && health?.realtime_protocol === REQUIRED_REALTIME_PROTOCOL);
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
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
    console.log("[glass] using remote api", {
      baseUrl: process.env.INTERVIEW_API_BASE_URL,
      source: configuredSource,
    });
    return;
  }

  console.log("[glass] using local api", {
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
    process.stdout.write(`[glass-api] ${chunk}`);
  });
  apiProcess.stderr.on("data", (chunk) => {
    process.stderr.write(`[glass-api] ${chunk}`);
  });
  apiProcess.on("error", (error) => {
    console.error("[glass] failed to start local api", error);
  });
  apiProcess.on("exit", (code, signal) => {
    console.error("[glass] local api exited", { code, signal });
    apiProcess = null;
  });

  const ready = await waitForApiReady(API_START_TIMEOUT_MS);
  if (!ready) {
    console.error("[glass] local api did not become healthy in time");
  }
}

async function configureSession() {
  const defaultSession = session.defaultSession;

  defaultSession.setPermissionCheckHandler((_webContents, permission) => {
    return ["media", "display-capture", "microphone"].includes(permission);
  });

  defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    if (["media", "display-capture", "microphone"].includes(permission)) {
      callback(true);
      return;
    }
    callback(false);
  });

  defaultSession.setDisplayMediaRequestHandler(
    async (request, callback) => {
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

async function createMainWindow() {
  const preloadPath = path.join(__dirname, "preload.cjs");
  const rendererUrl = getRendererUrl();

  mainWindow = new BrowserWindow({
    width: 740,
    height: 620,
    minWidth: 560,
    minHeight: 420,
    autoHideMenuBar: true,
    frame: false,
    fullscreenable: false,
    hasShadow: false,
    roundedCorners: false,
    show: false,
    skipTaskbar: false,
    title: WINDOW_TITLE,
    transparent: true,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.setAlwaysOnTop(true, process.platform === "darwin" ? "modal-panel" : "normal");
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  mainWindow.once("ready-to-show", () => {
    if (!mainWindow?.isDestroyed()) {
      mainWindow.show();
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    console.error("[glass] renderer load failed", {
      errorCode,
      errorDescription,
      validatedURL,
    });
  });

  mainWindow.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    if (level >= 2) {
      console.error("[glass] renderer console", {
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

async function setDockVisible(visible) {
  if (process.platform !== "darwin" || !app.dock) {
    return;
  }
  if (visible) {
    await app.dock.show();
    return;
  }
  app.dock.hide();
}

async function applyPrivacyMode(enabled) {
  const nextEnabled = Boolean(enabled);
  privacyModeEnabled = nextEnabled;
  for (const window of BrowserWindow.getAllWindows()) {
    window.setContentProtection(nextEnabled);
  }
  await setDockVisible(!nextEnabled);
  return { enabled: privacyModeEnabled };
}

function registerShortcuts() {
  globalShortcut.register("CommandOrControl+\\", () => {
    if (!mainWindow) {
      return;
    }
    mainWindow.isVisible() ? mainWindow.hide() : mainWindow.show();
  });
  globalShortcut.register("CommandOrControl+Enter", () => {
    mainWindow?.webContents.send("glass-command", "submit");
  });
  globalShortcut.register("CommandOrControl+Shift+\\", () => {
    mainWindow?.webContents.send("glass-command", "toggle-session");
  });
  globalShortcut.register("CommandOrControl+R", () => {
    mainWindow?.webContents.send("glass-command", "new-chat");
  });
}

ipcMain.handle("glass:get-privacy-mode", () => {
  return { enabled: privacyModeEnabled };
});

ipcMain.handle("glass:set-privacy-mode", (_event, enabled) => {
  const update = privacyModeUpdate.then(() => applyPrivacyMode(enabled));
  privacyModeUpdate = update.catch(() => undefined);
  return update;
});

ipcMain.handle("glass:set-window-expanded", (_event, expanded) => {
  if (!mainWindow) {
    return { expanded: false };
  }
  const nextExpanded = Boolean(expanded);
  const bounds = mainWindow.getBounds();
  mainWindow.setBounds({
    ...bounds,
    height: nextExpanded ? 620 : 158,
  });
  return { expanded: nextExpanded };
});

ipcMain.handle("glass:close", () => {
  app.quit();
});

app.whenReady().then(async () => {
  await applyPrivacyMode(false);
  await ensureApiServer();
  await configureSession();
  await createMainWindow();
  registerShortcuts();

  nativeTheme.on("updated", () => {
    mainWindow?.webContents.send("glass-command", "theme-updated");
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      void createMainWindow();
    } else {
      mainWindow?.show();
    }
  });
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  stopApiProcess();
});

app.on("window-all-closed", () => {
  stopApiProcess();

  if (process.platform !== "darwin") {
    app.quit();
  }
});
