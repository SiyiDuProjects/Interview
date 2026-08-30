# Architecture

## Objective

Keep the interview assistant low-latency and understandable:

- two capture sources;
- two long-lived OpenAI upstreams;
- text-only answers;
- three tools;
- pre-stored text context;
- isolated interview state;
- one shared client for Electron and remote browsers;
- no fallback pipeline.

## Runtime topology

```text
Electron capture host                      shared React clients
  system audio (interviewer, PCM) ──┐      ├── Electron window
  microphone (candidate, PCM) ──────┼─────>├── desktop browser
  discrete screen snapshots <───────┘      └── mobile browser
                   │                              │
                   └──────── FastAPI interview room ────────┐
                                                            │
                         ┌──────────────────────────────────┴─────────────┐
                         ▼                                                ▼
                gpt-realtime-2.1                              gpt-realtime-whisper
                transcript, tools, answers                    candidate context only
```

The two audio channels may share transport infrastructure, but their PCM payloads are never mixed. Speaker identity is determined by the capture channel, not by diarization.

## Client contract

Electron and remote browsers render the same React application and use the same `client` WebSocket protocol. There is no separate mobile viewer API or second answer state.

- Every authenticated client receives the same transcript events, append-only answer events and reconnect snapshots.
- Every client may send manual questions, request a host screenshot and end the interview.
- Only Electron has the capture adapter. Its `interviewer` and `candidate` WebSockets carry audio and fulfill screenshot requests.
- Electron opens both media sources when it starts and reports `ready`; it drops audio locally while the interview is idle. OpenAI upstreams are not opened until the first active audio chunk.
- Closing the Electron window hides it to the tray. The renderer remains alive and background throttling is disabled so capture continues.
- Desktop and mobile browsers open the fixed server URL. After browser authentication, they discover the single current interview from the server; there is no QR code, share link or session credential in the URL.

The FastAPI room is the sole live state source. Electron does not relay answers to browsers, and browsers do not maintain a parallel answer history. A browser may poll slowly only while waiting for the capture host to create a current interview; the live interview itself is WebSocket-driven.

The first personal release runs one capture device and one current interview, while allowing multiple equal-capability UI clients. Its registry and interview history are deliberately process-local and in-memory: an ordinary network reconnect receives snapshots for the same runtime, but a server restart loses that interview history and its OpenAI conversation. Electron then creates a new current interview. Run one Uvicorn worker and one service replica until shared state is intentionally introduced.

## Upstream contract

### Core

`gpt-realtime-2.1` owns:

- interviewer audio input;
- interviewer input transcription;
- semantic turn detection;
- text-only response generation;
- the three tool calls;
- the default conversation state.

It must not emit audible model speech. `analyze_problem` may issue one on-demand `gpt-5.6-sol` Responses request with `store:false`, but must not create a third long-lived model session.

### Candidate transcription

`gpt-realtime-whisper` owns only candidate microphone transcription. A final candidate transcript is added to the core conversation as context without creating a response.

If candidate transcription fails, interviewer answers continue and the UI marks candidate context as stale.

## Context contract

Context is read before an interview from `INTERVIEW_CONTEXT_DIR`, defaulting to `apps/server/context`.

- Accepted extensions: `.md`, `.txt`.
- Files are read-only during a session.
- There is no runtime upload endpoint.
- There is no vector store, embedding pipeline or remote file registry.
- A new interview receives a stable snapshot of the available context.

The core instructions remain intentionally short. They define role, answer style, routing and the requirement not to invent candidate facts. Full project/resume/JD text is not copied into the prompt; `search_context` returns only relevant passages.

## Tool contract

### `search_context`

Input: a focused query and an optional result limit.

Output: bounded text passages with source labels. No match is an explicit valid result; the model must answer generically instead of inventing personal experience.

### `capture_current_screen`

Requests one screenshot from Electron when a question depends on a visible prompt, whiteboard, IDE or code. Screenshots are discrete image inputs, not continuous video.

Failure returns a tool error to the core session and does not end the interview.

### `analyze_problem`

Used for coding, algorithms, complex SQL, debugging, concurrency, system design and code follow-ups. It returns a concise answer suitable for speaking or implementing. Ordinary concept, project and behavioral questions must not call it.

Failure returns control to the core model with the context already available.

## Session and authorization contract

Every runtime object belongs to one `interview_id` with two random, short-lived capabilities:

- `session_token`: joins the shared client channel, sends controls and may end the interview;
- `capture_token`: opens only the two Electron capture channels and is never returned by browser APIs.

The runtime owns:

- both upstreams;
- candidate and interviewer transcripts;
- pending screenshot requests;
- tool calls and outputs;
- context snapshot;
- answer history.

No process-global hub may share those objects between interviews.

`INTERVIEW_ACCESS_TOKEN` is optional for localhost development and expected for a remote production backend. Electron main uses it to ensure the single current interview. A browser enters it once over HTTPS; the server verifies it and stores only a derived value in an HttpOnly, SameSite cookie. The authenticated browser may then discover the current session without a pairing link. WebSocket capabilities are sent in the first authentication frame, never in a WebSocket query string. Authorization is checked before opening an OpenAI upstream.

## Answer history

Answer history is append-only:

- a pending item may stream until it reaches one terminal state;
- completing a response never rewrites an older completed response;
- a new model response creates a new history item;
- reconnects do not merge unrelated response IDs.

Transcript rendering may merge partial chunks for the same active turn, but committed transcript turns remain ordered.

## Failure behavior

- Core failure: mark the active answer failed; do not call an old HTTP coach.
- Candidate transcription failure: preserve the core session.
- Context miss: return no match; do not invent candidate facts.
- Screenshot failure: return a bounded tool error.
- Analysis failure: let the core answer with existing context.
- Invalid interview token: reject before consuming OpenAI resources.
- Reconnect: create or recover only the matching interview state.
- Server restart: start a new current interview; do not claim recovery of the previous in-memory conversation.

## Deployment boundary

Bundled, non-secret context lives under `apps/server/context` so it is included in the server deployment. Private production context should live outside `/opt/interview/server` and be mounted read-only through `INTERVIEW_CONTEXT_DIR`.

The deployment workflow builds the shared Vite client, stages it under `apps/server/web`, synchronizes the server bundle, preserves the production `.env`, rebuilds `interview_api`, and checks the public health endpoint. FastAPI serves that build from the same origin as the API and WebSockets. It must never sync API keys, access tokens or private interview materials from the repository.

## Verification

Automated tests should cover:

- exactly two long-lived upstreams;
- channel separation and no mixed audio;
- candidate transcripts never create answers;
- text-only core configuration;
- exactly three tool definitions;
- short instruction budget;
- context search success and no-match behavior;
- `interview_id + token` isolation;
- authorization before upstream connection;
- session/capture capability separation;
- two or more clients receiving the same ordered events;
- append-only answer history;
- reconnect and tool failure degradation.
- idle audio never opening an upstream and both capture channels being ready before start;
- snapshot-to-live ordering and identical ordered delivery to multiple UI clients.

Electron verification is still required for microphone permission, system-audio loopback, screenshots and real Realtime behavior.
