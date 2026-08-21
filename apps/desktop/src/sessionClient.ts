import type { InterviewSession, ServerEvent, Speaker } from "./types";

export type ClientConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting";

interface SessionClientCallbacks {
  onConnectionChange: (state: ClientConnectionState) => void;
  onEvent: (event: ServerEvent) => void;
  onError: (message: string) => void;
  onSessionUnavailable: () => void;
}

const READY_TIMEOUT_MS = 10_000;
const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000];

export class SessionClient {
  private socket: WebSocket | null = null;
  private reconnectTimer: number | undefined;
  private reconnectAttempt = 0;
  private active = false;
  private ready = false;

  constructor(
    private readonly apiBaseUrl: string,
    private readonly session: Pick<InterviewSession, "interview_id" | "session_token">,
    private readonly callbacks: SessionClientCallbacks,
  ) {}

  async start() {
    if (this.active) {
      return;
    }
    this.active = true;
    try {
      await this.openSocket(false);
    } catch (error) {
      this.active = false;
      throw error;
    }
  }

  stop() {
    this.active = false;
    this.ready = false;
    if (this.reconnectTimer !== undefined) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) {
      socket.close(1000, "client stopped");
    }
    this.callbacks.onConnectionChange("disconnected");
  }

  isReady() {
    return this.ready && this.socket?.readyState === WebSocket.OPEN;
  }

  send(payload: Record<string, unknown>) {
    const socket = this.socket;
    return Boolean(socket && this.ready && safeSocketSend(socket, payload));
  }

  private openSocket(reconnecting: boolean): Promise<void> {
    this.callbacks.onConnectionChange(reconnecting ? "reconnecting" : "connecting");

    return new Promise((resolve, reject) => {
      if (!this.active) {
        reject(new Error("会话连接已取消。"));
        return;
      }

      const socket = new WebSocket(
        buildInterviewSocketUrl(this.apiBaseUrl, this.session.interview_id, "client"),
      );
      this.socket = socket;
      this.ready = false;
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
        failBeforeReady("会话同步连接超时。");
        socket.close();
      }, READY_TIMEOUT_MS);

      socket.addEventListener("open", () => {
        if (
          this.socket !== socket ||
          !safeSocketSend(socket, {
            type: "authenticate",
            token: this.session.session_token,
          })
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
          this.ready = true;
          this.reconnectAttempt = 0;
          this.callbacks.onConnectionChange("connected");
          if (!settled) {
            settled = true;
            window.clearTimeout(timeoutId);
            resolve();
          }
          return;
        }
        this.callbacks.onEvent(event);
      });

      socket.addEventListener("error", () => {
        failBeforeReady("会话同步连接失败。");
      });

      socket.addEventListener("close", (event) => {
        window.clearTimeout(timeoutId);
        const wasCurrent = this.socket === socket;
        if (wasCurrent) {
          this.socket = null;
          this.ready = false;
        }
        failBeforeReady("会话同步在就绪前关闭。");

        if (!wasCurrent || !this.active) {
          return;
        }
        if (event.code === 1008) {
          this.active = false;
          this.callbacks.onConnectionChange("disconnected");
          this.callbacks.onError("会话认证失败或会话已结束。");
          this.callbacks.onSessionUnavailable();
          return;
        }
        if (authenticated) {
          this.scheduleReconnect();
        }
      });
    });
  }

  private scheduleReconnect() {
    if (!this.active || this.reconnectTimer !== undefined) {
      return;
    }
    const delay = RECONNECT_DELAYS_MS[
      Math.min(this.reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)
    ];
    this.reconnectAttempt += 1;
    this.callbacks.onConnectionChange("reconnecting");
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = undefined;
      void this.openSocket(true).catch((error) => {
        if (!this.active) {
          return;
        }
        this.callbacks.onError(errorMessage(error, "会话同步重连失败。"));
        this.scheduleReconnect();
      });
    }, delay);
  }
}

export function buildInterviewSocketUrl(
  apiBaseUrl: string,
  interviewId: string,
  channel: Speaker | "client",
) {
  const base = new URL(apiBaseUrl);
  if (base.username || base.password) {
    throw new Error("API 地址不能包含用户名或密码。");
  }
  const isLoopback = ["localhost", "127.0.0.1", "::1", "[::1]"].includes(base.hostname);
  if (base.protocol === "https:") {
    base.protocol = "wss:";
  } else if (base.protocol === "http:" && isLoopback) {
    base.protocol = "ws:";
  } else {
    throw new Error("远程实时会话必须使用 HTTPS/WSS。");
  }
  base.pathname = `/ws/interviews/${encodeURIComponent(interviewId)}/${channel}`;
  base.search = "";
  base.hash = "";
  return base.toString();
}

export function safeSocketSend(socket: WebSocket, payload: Record<string, unknown>) {
  if (socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  try {
    socket.send(JSON.stringify(payload));
    return true;
  } catch {
    return false;
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
