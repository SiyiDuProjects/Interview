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
  getWindowState: () => ipcRenderer.invoke("window:state"),
  setCollapsed: (value) => ipcRenderer.invoke("window:collapse", value),
  setCodeExpanded: (value) => ipcRenderer.invoke("window:code", value),
  setPinned: (value) => ipcRenderer.invoke("window:pin", value),
  hideWindow: () => ipcRenderer.invoke("window:hide"),
  listScreenSources: () => ipcRenderer.invoke("screen:list-sources"),
  selectScreenSource: (sourceId) => ipcRenderer.invoke("screen:select-source", sourceId),
  captureScreenSnapshot: () => ipcRenderer.invoke("screen:capture"),
  createInterview: (apiBaseUrl) => ipcRenderer.invoke("interview:create", apiBaseUrl),
  endInterview: (apiBaseUrl, interviewId, sessionToken) =>
    ipcRenderer.invoke("interview:end", apiBaseUrl, interviewId, sessionToken),
  requestCaptureInitialization: () => ipcRenderer.invoke("capture:initialize"),
});
