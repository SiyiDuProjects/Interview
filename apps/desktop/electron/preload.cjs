const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("interviewDesktop", {
  isElectron: true,
  platform: process.platform,
  apiBaseUrl: process.env.INTERVIEW_API_BASE_URL || "",
  localApiEnabled: process.env.INTERVIEW_LOCAL_API_ENABLED === "1",
});
