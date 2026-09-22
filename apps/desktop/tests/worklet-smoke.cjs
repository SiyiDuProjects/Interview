// Offline Chromium integration: synthetic oscillator only, no devices/network.
const { app, BrowserWindow } = require("electron");
const { pathToFileURL } = require("node:url");
const path = require("node:path");

const timeout = setTimeout(() => { console.error("WORKLET_SMOKE_TIMEOUT"); app.exit(1); }, 20_000);
app.whenReady().then(async () => {
  const window = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  try {
    await window.loadFile(path.join(__dirname, "worklet-smoke.html"));
    const url = pathToFileURL(path.join(__dirname, "../dist/pcm-worklet.js")).href;
    const result = await window.webContents.executeJavaScript(`(async () => {
      const context = new AudioContext({ sampleRate: 24000 });
      try {
        await context.audioWorklet.addModule(${JSON.stringify(url)});
        const processor = new AudioWorkletNode(context, "interview-pcm", {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: "explicit",
        });
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        gain.gain.value = 0;
        oscillator.connect(processor).connect(gain).connect(context.destination);
        let frames = 0, nonzero = false, length = 0;
        const complete = new Promise((resolve, reject) => {
          processor.onprocessorerror = () => reject(new Error("processorerror"));
          processor.port.onmessage = ({ data }) => {
            frames++;
            const pcm = new Int16Array(data.pcm);
            length = pcm.length;
            nonzero ||= pcm.some((sample) => sample !== 0);
            if (frames >= 5) resolve();
          };
        });
        oscillator.start();
        await context.resume();
        await complete;
        oscillator.stop(); processor.port.close();
        return { sampleRate: context.sampleRate, frames, length, nonzero, secureContext: isSecureContext };
      } finally { await context.close(); }
    })()`, true);
    if (result.sampleRate !== 24000 || result.length !== 1024 || !result.nonzero || !result.secureContext) throw new Error("Unexpected synthetic PCM result");
    console.log("WORKLET_SMOKE_OK " + JSON.stringify(result));
    clearTimeout(timeout); window.destroy(); app.exit(0);
  } catch (error) {
    console.error("WORKLET_SMOKE_FAILED " + error.message);
    clearTimeout(timeout); window.destroy(); app.exit(1);
  }
});
