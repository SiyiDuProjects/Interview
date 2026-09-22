import type { Speaker } from "./types";

export type CaptureHealthPhase = "ready" | "muted" | "interrupted" | "error";

export interface CaptureHealth {
  phase: CaptureHealthPhase;
  detail: string;
}

export interface AudioCaptureHandle {
  getHealth: () => CaptureHealth;
  stop: () => void;
}

interface LocalAudioCaptureOptions {
  stream: MediaStream;
  onChunk: (chunk: ArrayBuffer) => void;
  onHealthChange?: (health: CaptureHealth) => void;
  onEnded?: () => void;
}

const DISPLAY_MEDIA_CONSTRAINTS: DisplayMediaStreamOptions = { audio: true, video: true };
const USER_MEDIA_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: false,
    autoGainControl: false,
  },
  video: false,
};
const TARGET_SAMPLE_RATE = 24000;
const PCM_STALL_MS = 3000;
const MAX_PCM_AGE_SECONDS = 0.5;

export async function requestCaptureStream(speaker: Speaker): Promise<MediaStream> {
  if (speaker === "candidate") return navigator.mediaDevices.getUserMedia(USER_MEDIA_CONSTRAINTS);
  const stream = await navigator.mediaDevices.getDisplayMedia(DISPLAY_MEDIA_CONSTRAINTS);
  if (stream.getAudioTracks().length === 0) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error("没有采集到系统音频，请确认系统允许音频采集。");
  }
  return stream;
}

export function startLocalAudioCapture(options: LocalAudioCaptureOptions): AudioCaptureHandle {
  const audioTracks = options.stream.getAudioTracks();
  if (audioTracks.length === 0) throw new Error("当前媒体流没有音频轨道。");
  const tracks = options.stream.getTracks();
  const audioContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
  let sourceNode: MediaStreamAudioSourceNode;
  let processorNode: AudioWorkletNode | undefined;
  let gainNode: GainNode;
  try {
    sourceNode = audioContext.createMediaStreamSource(new MediaStream(audioTracks));
    if (audioContext.sampleRate !== TARGET_SAMPLE_RATE || !audioContext.audioWorklet) {
      throw new Error("音频处理环境不支持 24 kHz AudioWorklet，请重新打开桌面端。");
    }
    gainNode = audioContext.createGain();
    gainNode.gain.value = 0;
    gainNode.connect(audioContext.destination);
  } catch (error) {
    tracks.forEach((track) => track.stop());
    void audioContext.close().catch(() => {});
    throw error;
  }

  let stopped = false;
  let endedNotified = false;
  let processorFailed = false;
  let receivedPcm = false;
  let lastPcmAt = Date.now();
  let health: CaptureHealth = { phase: "interrupted", detail: "正在初始化音频处理。" };

  function reportHealth(next: CaptureHealth) {
    if (stopped || (health.phase === next.phase && health.detail === next.detail)) return;
    health = next;
    options.onHealthChange?.({ ...health });
  }

  function checkHealth() {
    if (stopped) return;
    if (processorFailed) {
      reportHealth({ phase: "error", detail: "音频处理启动失败或异常停止，请恢复这一路采集。" });
    } else if (endedNotified || tracks.some((track) => track.readyState === "ended")) {
      reportHealth({ phase: "error", detail: "媒体轨道已结束，请恢复这一路采集。" });
    } else if (audioContext.state === "closed") {
      reportHealth({ phase: "error", detail: "音频处理已关闭，请恢复这一路采集。" });
    } else if (audioTracks.some((track) => track.muted)) {
      // Track.muted means unavailable source data, not a person being quiet.
      reportHealth({ phase: "muted", detail: "媒体源暂时没有提供音频，正在等待恢复。" });
    } else if (String(audioContext.state) !== "running") {
      reportHealth({ phase: "interrupted", detail: "音频处理已暂停，请恢复这一路采集。" });
    } else if (!receivedPcm || Date.now() - lastPcmAt > PCM_STALL_MS) {
      reportHealth({ phase: "interrupted", detail: "音频处理未输出数据，请恢复这一路采集。" });
    } else {
      reportHealth({ phase: "ready", detail: "音频处理正常。" });
    }
  }

  function handleTrackEnded() {
    if (stopped || endedNotified) return;
    endedNotified = true;
    checkHealth();
    options.onEnded?.();
  }
  tracks.forEach((track) => {
    track.addEventListener("ended", handleTrackEnded);
    track.addEventListener("mute", checkHealth);
    track.addEventListener("unmute", checkHealth);
  });
  audioContext.addEventListener("statechange", checkHealth);
  const healthTimer = window.setInterval(checkHealth, 1000);

  void audioContext.audioWorklet.addModule(new URL("./pcm-worklet.js", document.baseURI).href).then(() => {
    if (stopped) return;
    processorNode = new AudioWorkletNode(audioContext, "interview-pcm", {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
      channelCount: 1, channelCountMode: "explicit",
    });
    processorNode.onprocessorerror = () => { processorFailed = true; checkHealth(); };
    processorNode.port.onmessage = ({ data }) => {
      if (stopped || !(data.pcm instanceof ArrayBuffer)) return;
      if (audioContext.currentTime - data.endTime > MAX_PCM_AGE_SECONDS) {
        reportHealth({ phase: "interrupted", detail: "界面处理延迟，部分过期音频已跳过；请补充遗漏内容。" });
        return;
      }
      receivedPcm = true;
      lastPcmAt = Date.now();
      checkHealth();
      // Zero-valued PCM is valid silence, not a broken device.
      if (health.phase === "ready") options.onChunk(data.pcm);
    };
    sourceNode.connect(processorNode);
    processorNode.connect(gainNode);
  }).catch(() => { processorFailed = true; checkHealth(); });

  checkHealth();
  if (audioContext.state === "suspended") {
    void audioContext.resume().then(checkHealth).catch(() => {
      reportHealth({ phase: "interrupted", detail: "音频处理无法恢复，请重新连接这一路采集。" });
    });
  }

  return {
    getHealth: () => ({ ...health }),
    stop: () => {
      if (stopped) return;
      stopped = true;
      window.clearInterval(healthTimer);
      if (processorNode) {
        processorNode.port.onmessage = null;
        processorNode.port.close();
        processorNode.onprocessorerror = null;
        processorNode.disconnect();
      }
      audioContext.removeEventListener("statechange", checkHealth);
      sourceNode.disconnect();
      gainNode.disconnect();
      tracks.forEach((track) => {
        track.removeEventListener("ended", handleTrackEnded);
        track.removeEventListener("mute", checkHealth);
        track.removeEventListener("unmute", checkHealth);
        track.stop();
      });
      if (audioContext.state !== "closed") void audioContext.close().catch(() => {});
    },
  };
}

export function getCaptureLabel(speaker: Speaker): string {
  return speaker === "candidate" ? "麦克风" : "系统音频";
}
