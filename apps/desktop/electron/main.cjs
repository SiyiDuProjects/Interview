const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { app, BrowserWindow, desktopCapturer, session, shell } = require("electron");

const WINDOW_TITLE = "Sage";
const API_PORT = 8000;
const API_HEALTH_URL = `http://127.0.0.1:${API_PORT}/health`;
const API_START_TIMEOUT_MS = 15000;

const writableRoot = path.join(os.tmpdir(), "interview-copilot-electron");
let apiProcess = null;

app.setPath("userData", writableRoot);
app.commandLine.appendSwitch("disk-cache-dir", path.join(writableRoot, "cache"));
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");

function getRendererUrl() {
  const arg = process.argv.find((value) => value.startsWith("--renderer-url="));
  return arg ? arg.slice("--renderer-url=".length) : "";
}

function isApiHealthy() {
  return new Promise((resolve) => {
    const request = http.get(API_HEALTH_URL, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("error", () => resolve(false));
    request.setTimeout(1200, () => {
      request.destroy();
      resolve(false);
    });
  });
}

async function waitForApiReady(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isApiHealthy()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

async function ensureApiServer() {
  if (await isApiHealthy()) {
    return;
  }

  const pythonCommand = process.env.INTERVIEW_PYTHON || "python";
  const serverDir = path.join(__dirname, "..", "..", "server");

  apiProcess = spawn(
    pythonCommand,
    ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(API_PORT)],
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
    mainWindow.webContents.openDevTools({ mode: "detach" });
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
