/// <reference types="vite/client" />

interface InterviewSessionCredentials {
  interview_id: string;
  session_token: string;
  capture_token: string;
}

interface InterviewScreenSource {
  id: string;
  name: string;
  displayId: string;
  thumbnailDataUrl: string;
  selected: boolean;
}

interface Window {
  interviewDesktop?: {
    isElectron: boolean;
    captureHost?: boolean;
    platform: string;
    apiBaseUrl?: string;
    getWindowState?: () => Promise<{ collapsed: boolean; codeExpanded?: boolean; pinned: boolean; recoveryNotice?: string }>;
    setCollapsed?: (value: boolean) => Promise<boolean>;
    setCodeExpanded?: (value: boolean) => Promise<boolean>;
    setPinned?: (value: boolean) => Promise<boolean>;
    hideWindow?: () => Promise<void>;
    listScreenSources?: () => Promise<InterviewScreenSource[]>;
    selectScreenSource?: (sourceId: string) => Promise<{ id: string; name: string }>;
    captureScreenSnapshot?: () => Promise<{ image_data: string; source_id: string; captured_at: string }>;
    createInterview?: (apiBaseUrl: string) => Promise<InterviewSessionCredentials>;
    requestCaptureInitialization?: () => Promise<void>;
    endInterview?: (
      apiBaseUrl: string,
      interviewId: string,
      sessionToken: string,
    ) => Promise<{ ok: boolean }>;
  };
}
