import {
  getCaptureLabel,
  startLocalAudioCapture,
  type AudioCaptureHandle,
} from "./audioCapture";
import { buildInterviewSocketUrl, safeSocketSend } from "./sessionClient";
import type { ChannelState, InterviewSession, ServerEvent, Speaker } from "./types";

interface CaptureAdapterCallbacks {
  onChannelChange: (speaker: Speaker, state: ChannelState) => void;
  onError: (message: string) => void;
  onMediaEnded: (speaker: Speaker) => void;
  onSessionEnded: () => void;
}

const SPEAKERS: Speaker[] = ["interviewer", "candidate"];
const READY_TIMEOUT_MS = 10_000;
const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000];
const MAX_BUFFERED_AUDIO_BYTES = 256 * 1024;

export class CaptureAdapter {
  private readonly handles: Record<Speaker, AudioCaptureHandle>;
  private readonly sockets: Partial<Record<Speaker, WebSocket>> = {};
  private readonly ready: Record<Speaker, boolean> = { interviewer: false, candidate: false };
  private readonly sending: Record<Speaker, boolean> = { interviewer: false, candidate: false };
  private readonly reconnectTimers: Partial<Record<Speaker, number>> = {};
  private readonly reconnectAttempts: Record<Speaker, number> = {
    interviewer: 0,
    candidate: 0,
  };
  private session: InterviewSession | null = null;
  private disposed = false;

  constructor(
    private readonly apiBaseUrl: string,
    streams: Record<Speaker, MediaStream>,
    private readonly callbacks: CaptureAdapterCallbacks,
  ) {
    this.handles = {
      interviewer: startLocalAudioCapture({
        stream: streams.interviewer,
        onChunk: (chunk) => this.sendAudio("interviewer", chunk),
        onEnded: () => this.handleMediaEnded("interviewer"),
      }),
      candidate: startLocalAudioCapture({
        stream: streams.candidate,
        onChunk: (chunk) => this.sendAudio("candidate", chunk),
        onEnded: () => this.handleMediaEnded("candidate"),
      }),
    };
  }

  async connect(session: InterviewSession) {
    if (this.disposed) {
      throw new Error("采集设备已经关闭，请重新初始化。");
    }
    if (!session.capture_token) {
      throw new Error("采集会话缺少 capture_token。");
    }
    this.disconnectSession();
    this.session = session;
    try {
      await Promise.all(SPEAKERS.map((speaker) => this.openChannel(speaker, false)));
    } catch (error) {
      this.disconnectSession();
      throw error;
    }
  }

  disconnectSession() {
    this.session = null;
    SPEAKERS.forEach((speaker) => {
      this.sending[speaker] = false;
      this.ready[speaker] = false;
      const timer = this.reconnectTimers[speaker];
      if (timer !== undefined) {
        window.clearTimeout(timer);
        delete this.reconnectTimers[speaker];
      }
      const socket = this.sockets[speaker];
      delete this.sockets[speaker];
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close(1000, "capture session changed");
      }
    });
  }

  dispose() {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.disconnectSession();
    SPEAKERS.forEach((speaker) => this.handles[speaker].stop());
  }

  private openChannel(speaker: Speaker, reconnecting: boolean): Promise<void> {
    const session = this.session;
    if (!session?.capture_token || this.disposed) {
      return Promise.reject(new Error("采集会话已经结束。"));
    }
    this.callbacks.onChannelChange(speaker, {
      phase: reconnecting ? "reconnecting" : "connecting",
      message: reconnecting ? "重连中" : "连接采集通道",
    });

    return new Promise((resolve, reject) => {
      const socket = new WebSocket(
        buildInterviewSocketUrl(this.apiBaseUrl, session.interview_id, speaker),
      );
      this.sockets[speaker] = socket;
      this.ready[speaker] = false;
      this.sending[speaker] = false;
      let settled = false;
      let authenticated = false;

      const failBeforeReady = (message: string) => {
        if (settled) {
          return;
        }
        settled = true;
        window.clearTimeout(timeoutId);
        reject(new Error(message));
      };
      const timeoutId = window.setTimeout(() => {
        failBeforeReady(`${getCaptureLabel(speaker)}通道连接超时。`);
        socket.close();
      }, READY_TIMEOUT_MS);

      socket.addEventListener("open", () => {
        if (
          this.sockets[speaker] !== socket ||
          !safeSocketSend(socket, { type: "authenticate", token: session.capture_token })
        ) {
          socket.close(1008, "authentication failed");
        }
      });

      socket.addEventListener("message", (message) => {
        if (typeof message.data !== "string") {
          return;
        }
        const event = parseServerEvent(message.data);
        if (!event) {
          return;
        }
        if (event.type === "session_ready") {
          authenticated = true;
          this.ready[speaker] = true;
          this.reconnectAttempts[speaker] = 0;
          this.callbacks.onChannelChange(speaker, { phase: "listening", message: "已就绪" });
          safeSocketSend(socket, { type: "capture_ready" });
          if (!settled) {
            settled = true;
            window.clearTimeout(timeoutId);
            resolve();
          }
          return;
        }
        if (event.type === "capture_start") {
          this.sending[speaker] = true;
          this.callbacks.onChannelChange(speaker, { phase: "listening", message: "采集中" });
          return;
        }
        if (event.type === "capture_stop") {
          this.sending[speaker] = false;
          this.callbacks.onChannelChange(speaker, { phase: "listening", message: "已就绪" });
          return;
        }
        if (event.type === "screen_capture_request" && speaker === "interviewer") {
          void this.respondToScreenCapture(event, socket);
          return;
        }
        if (event.type === "session_ended") {
          this.callbacks.onSessionEnded();
          return;
        }
        if (event.type === "error") {
          const detail = event.detail ?? event.error ?? event.message;
          if (detail) {
            this.callbacks.onError(detail);
          }
        }
      });

      socket.addEventListener("error", () => {
        failBeforeReady(`${getCaptureLabel(speaker)}通道连接失败。`);
      });

      socket.addEventListener("close", (event) => {
        window.clearTimeout(timeoutId);
        const wasCurrent = this.sockets[speaker] === socket;
        if (wasCurrent) {
          delete this.sockets[speaker];
          this.ready[speaker] = false;
          this.sending[speaker] = false;
        }
        failBeforeReady(`${getCaptureLabel(speaker)}通道在就绪前关闭。`);
        const sessionIsCurrent =
          wasCurrent &&
          this.session?.interview_id === session.interview_id &&
          !this.disposed;
        if (sessionIsCurrent && event.code === 1008) {
          this.callbacks.onSessionEnded();
        } else if (sessionIsCurrent && authenticated) {
          this.scheduleReconnect(speaker);
        }
      });
    });
  }

  private scheduleReconnect(speaker: Speaker) {
    if (!this.session || this.disposed || this.reconnectTimers[speaker] !== undefined) {
      return;
    }
    const attempt = this.reconnectAttempts[speaker];
    const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
    this.reconnectAttempts[speaker] = attempt + 1;
    this.callbacks.onChannelChange(speaker, { phase: "reconnecting", message: "重连中" });
    this.reconnectTimers[speaker] = window.setTimeout(() => {
      delete this.reconnectTimers[speaker];
      void this.openChannel(speaker, true).catch((error) => {
        if (!this.session || this.disposed) {
          return;
        }
        this.callbacks.onError(errorMessage(error, `${getCaptureLabel(speaker)}重连失败。`));
        this.scheduleReconnect(speaker);
      });
    }, delay);
  }

  private sendAudio(speaker: Speaker, chunk: ArrayBuffer) {
    const socket = this.sockets[speaker];
    if (
      !this.sending[speaker] ||
      !this.ready[speaker] ||
      socket?.readyState !== WebSocket.OPEN ||
      socket.bufferedAmount > MAX_BUFFERED_AUDIO_BYTES
    ) {
      return;
    }
    try {
      socket.send(chunk);
    } catch {
      // The socket close handler owns reconnect and visible state.
    }
  }

  private async respondToScreenCapture(event: ServerEvent, socket: WebSocket) {
    if (!event.request_id || this.sockets.interviewer !== socket || !this.ready.interviewer) {
      return;
    }
    const snapshot = this.handles.interviewer.snapshot;
    if (!snapshot) {
      safeSocketSend(socket, {
        type: "screen_snapshot",
        request_id: event.request_id,
        error: "屏幕快照不可用。",
      });
      return;
    }
    try {
      const imageData = await snapshot();
      if (this.sockets.interviewer === socket && this.ready.interviewer) {
        safeSocketSend(socket, {
          type: "screen_snapshot",
          request_id: event.request_id,
          image_data: imageData,
        });
      }
    } catch (error) {
      if (this.sockets.interviewer === socket && this.ready.interviewer) {
        safeSocketSend(socket, {
          type: "screen_snapshot",
          request_id: event.request_id,
          error: errorMessage(error, "截图失败。"),
        });
      }
    }
  }

  private handleMediaEnded(speaker: Speaker) {
    if (!this.disposed) {
      this.callbacks.onMediaEnded(speaker);
    }
  }
}

function parseServerEvent(value: string): ServerEvent | null {
  try {
    const parsed = JSON.parse(value) as ServerEvent;
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}
