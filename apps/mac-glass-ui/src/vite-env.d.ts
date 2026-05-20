/// <reference types="vite/client" />

type GlassCommand = "submit" | "toggle-session" | "new-chat" | "theme-updated";

interface Window {
  glassDesktop?: {
    apiBaseUrl: string;
    isElectron: boolean;
    platform: string;
    invoke: <T = unknown>(channel: string, payload?: unknown) => Promise<T>;
    onCommand: (handler: (command: GlassCommand) => void) => () => void;
  };
}
