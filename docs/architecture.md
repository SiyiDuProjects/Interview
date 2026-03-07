# Architecture

## Goal

Build a local, low-latency mock interview assistant with a simple transcript view and a rolling answer panel.

## UX Shape

- Left: chat-style transcript
  - interviewer messages on the left
  - candidate messages on the right
  - live chunk buffers that mimic streaming ASR fragments
  - optional microphone and system-audio capture controls
- Right: rolling answer blocks
  - fast answer for immediate guidance
  - detailed answer streamed into the same card
  - likely follow-up angles

## Hybrid Low-Latency Pipeline

```text
manual text, live chunks, or captured audio
  -> STT API transcription
  -> auto cut into complete turns
  -> lightweight topic detection
  -> context stitcher
  -> fast AI answer path
  -> answer card appears immediately
  -> background detail job
  -> SSE stream pushes richer content into the same card
```

## Runtime Boundaries

- React: transcript, live chunk simulator, media capture controls, and rolling answer feed
- FastAPI: audio transcription endpoint, topic classification, follow-up detection, fast answer generation, detail job orchestration, and detail streaming
- Browser or desktop shell: microphone capture and system-audio capture permissions

## Testing Shape

- Manual text input simulates completed ASR turns.
- Live chunk simulation mimics partial ASR output and automatic sentence cutting.
- Microphone and system-audio capture feed audio chunks into the same sentence-cutting pipeline after transcription.
- Long dialogue scenario exercises repeated interviewer and candidate turns.
- The detail pipeline can run against OpenAI or a mock provider.
- The mock provider keeps the real-time UI update path testable without external dependencies.

## MVP Constraint

The current codebase now supports manual text, live chunk simulation, and real audio capture. The audio transcription path now uses an STT API instead of a local Whisper runtime, which trades pure local execution for a more stable real-time path.
