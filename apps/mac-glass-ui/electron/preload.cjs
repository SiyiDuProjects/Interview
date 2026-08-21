const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("glassDesktop", {
  apiBaseUrl: process.env.INTERVIEW_API_BASE_URL || process.env.VITE_API_BASE_URL || "",
  localApiEnabled: process.env.INTERVIEW_LOCAL_API_ENABLED === "1",
  isElectron: true,
  platform: process.platform,
  invoke: (channel, payload) => ipcRenderer.invoke(channel, payload),
  onCommand: (handler) => {
    const listener = (_event, command) => handler(command);
    ipcRenderer.on("glass-command", listener);
    return () => ipcRenderer.off("glass-command", listener);
  },
});
