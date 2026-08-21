# Sage Glass UI Prototype

Cluely-style macOS glass UI prototype for the Interview Realtime backend.

This app is intentionally isolated from the current desktop client. It connects to the same FastAPI Realtime WebSocket routes:

- `/ws/realtime/interview/interviewer`
- `/ws/realtime/interview/candidate`

## Local Development

```bash
cd apps/mac-glass-ui
npm install
npm run dev:desktop
```

If `INTERVIEW_API_BASE_URL` is not set, the Electron shell uses `https://interview.reachard.co`.
To use a local FastAPI backend, start with `INTERVIEW_API_BASE_URL=http://127.0.0.1:8000 npm run dev:desktop`.

## Shortcuts

- `CommandOrControl+\`: show or hide the window
- `CommandOrControl+Enter`: send the current prompt
- `CommandOrControl+Shift+\`: start or stop the Realtime session
- `CommandOrControl+R`: clear local transcript and answers

## Notes

Privacy mode enables Electron content protection and hides the Dock icon on macOS. It is meant to reduce accidental screen-share exposure of the helper UI.
