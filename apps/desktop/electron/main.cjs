const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const net = require("node:net");
const fs = require("node:fs");
const { spawn } = require("node:child_process");
const { app, BrowserWindow, desktopCapturer, session, shell } = require("electron");

const WINDOW_TITLE = "Sage";
const DEFAULT_API_PORT = 8000;
const FALLBACK_API_PORTS = [8000, 8001];
const API_START_TIMEOUT_MS = 15000;
const REQUIRED_REALTIME_PROTOCOL = "realtime-text-events-v2";

const writableRoot = path.join(os.tmpdir(), "interview-copilot-electron");
let apiProcess = null;
let apiPort = DEFAULT_API_PORT;

function configuredApiPort() {
  const configuredUrl = process.env.INTERVIEW_API_BASE_URL || process.env.VITE_API_BASE_URL || "";
  const match = configuredUrl.match(/127\.0\.0\.1:(\d+)|localhost:(\d+)/);
  if (match) {
    return Number(match[1] || match[2]);
  }
  const configuredPort = Number(process.env.INTERVIEW_API_PORT || "");
  return Number.isFinite(configuredPort) && configuredPort > 0 ? configuredPort : null;
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
  const localPython = path.join(serverDir, ".venv", "Scripts", "python.exe");
  const pythonCommand = process.env.INTERVIEW_PYTHON || (fs.existsSync(localPython) ? localPython : "python");

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

  ses.setPermissionCheckHandler((_webContents, permission) => {
    return ["media", "display-capture", "microphone"].includes(permission);
  });

  ses.setPermissionRequestHandler((_webContents, permission, callback) => {
    if (["media", "display-capture", "microphone"].includes(permission)) {
      callback(true);
      return;
    }
    callback(false);
  });

  ses.setDisplayMediaRequestHandler(
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

  const mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 560,
    autoHideMenuBar: true,
    title: WINDOW_TITLE,
    backgroundColor: "#f6f3ed",
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
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

app.whenReady().then(async () => {
  await ensureApiServer();
  await configureSession();
  await createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      void createMainWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (apiProcess) {
    apiProcess.kill();
    apiProcess = null;
  }

  if (process.platform !== "darwin") {
    app.quit();
  }
});
