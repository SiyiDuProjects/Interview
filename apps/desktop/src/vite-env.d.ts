/// <reference types="vite/client" />

interface InterviewSessionCredentials {
  interview_id: string;
  session_token: string;
  capture_token: string;
}

interface Window {
  interviewDesktop?: {
    isElectron: boolean;
    captureHost?: boolean;
    platform: string;
    apiBaseUrl?: string;
    createInterview?: (apiBaseUrl: string) => Promise<InterviewSessionCredentials>;
    requestCaptureInitialization?: () => Promise<void>;
    endInterview?: (
      apiBaseUrl: string,
      interviewId: string,
      sessionToken: string,
    ) => Promise<{ ok: boolean }>;
  };
}
