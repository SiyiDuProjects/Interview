const { contextBridge, ipcRenderer } = require("electron");

const apiBaseArgument = process.argv.find((value) =>
  value.startsWith("--interview-api-base-url="),
);
const apiBaseUrl = apiBaseArgument?.slice("--interview-api-base-url=".length) || "";

contextBridge.exposeInMainWorld("interviewDesktop", {
  isElectron: true,
  captureHost: true,
  platform: process.platform,
  apiBaseUrl,
  createInterview: (apiBaseUrl) => ipcRenderer.invoke("interview:create", apiBaseUrl),
  endInterview: (apiBaseUrl, interviewId, sessionToken) =>
    ipcRenderer.invoke("interview:end", apiBaseUrl, interviewId, sessionToken),
  requestCaptureInitialization: () => ipcRenderer.invoke("capture:initialize"),
});
