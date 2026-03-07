const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("interviewDesktop", {
  isElectron: true,
  platform: process.platform,
});
