import { type Speaker } from "./types";

export interface AudioCaptureHandle {
  snapshot?: () => Promise<string>;
  stop: () => void;
}

interface LocalAudioCaptureOptions {
  stream: MediaStream;
  chunkDurationMs: number;
  minLevel: number;
  onChunk: (chunk: ArrayBuffer) => void;
}

const DISPLAY_MEDIA_CONSTRAINTS: DisplayMediaStreamOptions = {
  audio: true,
  video: true,
};

const USER_MEDIA_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
  },
  video: false,
};

const TARGET_SAMPLE_RATE = 24000;
const SCRIPT_BUFFER_SIZE = 1024;
export async function requestCaptureStream(speaker: Speaker): Promise<MediaStream> {
  if (speaker === "candidate") {
    return navigator.mediaDevices.getUserMedia(USER_MEDIA_CONSTRAINTS);
  }

  const stream = await navigator.mediaDevices.getDisplayMedia(DISPLAY_MEDIA_CONSTRAINTS);
  if (stream.getAudioTracks().length === 0) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error("没有采集到系统音频，请确认共享时勾选了音频。");
  }
  return stream;
}

export function startLocalAudioCapture(options: LocalAudioCaptureOptions): AudioCaptureHandle {
  const audioTracks = options.stream.getAudioTracks();
  if (audioTracks.length === 0) {
    throw new Error("当前媒体流里没有音频轨道。");
  }

  const audioOnlyStream = new MediaStream(audioTracks);
  const audioContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
  const sourceNode = audioContext.createMediaStreamSource(audioOnlyStream);
  const processorNode = audioContext.createScriptProcessor(SCRIPT_BUFFER_SIZE, 1, 1);
  const gainNode = audioContext.createGain();
  gainNode.gain.value = 0;

  let stopped = false;
  processorNode.onaudioprocess = (event) => {
    if (stopped) {
      return;
    }

    const input = event.inputBuffer.getChannelData(0);
    const pcm = new Int16Array(input.length);
    let sum = 0;

    for (let index = 0; index < input.length; index += 1) {
      const sample = clampSample(input[index]);
      pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      sum += sample * sample;
    }

    // Keep upstream realtime ASR fed with continuous audio, including silence,
    // so word boundaries are detected without waiting for the next spoken token.
    options.onChunk(pcm.buffer.slice(0));
  };

  sourceNode.connect(processorNode);
  processorNode.connect(gainNode);
  gainNode.connect(audioContext.destination);

  const videoTracks = options.stream.getVideoTracks();

  return {
    snapshot:
      videoTracks.length > 0
        ? () => captureVideoFrameDataUrl(new MediaStream(videoTracks))
        : undefined,
    stop: () => {
      if (stopped) {
        return;
      }
      stopped = true;
      processorNode.disconnect();
      sourceNode.disconnect();
      gainNode.disconnect();
      options.stream.getTracks().forEach((track) => track.stop());
      void audioContext.close();
    },
  };
}

async function captureVideoFrameDataUrl(stream: MediaStream): Promise<string> {
  const video = document.createElement("video");
  video.srcObject = stream;
  video.muted = true;
  await video.play();

  try {
    const width = video.videoWidth || 1280;
    const height = video.videoHeight || 720;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("无法创建截图画布。");
    }
    context.drawImage(video, 0, 0, width, height);
    return canvas.toDataURL("image/jpeg", 0.82);
  } finally {
    video.pause();
    video.srcObject = null;
  }
}

export function getCaptureLabel(speaker: Speaker): string {
  return speaker === "candidate" ? "麦克风" : "系统音频";
}

function clampSample(sample: number) {
  if (sample > 1) {
    return 1;
  }
  if (sample < -1) {
    return -1;
  }
  return sample;
}
