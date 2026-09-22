# Architecture

## Objective

Keep the interview assistant low-latency and understandable:

- two capture sources;
- two application-owned long-lived OpenAI upstreams;
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
                gpt-live-1                                   gpt-live-transcribe
                captions + hosted Astra                       candidate context only
```

The two audio channels may share transport infrastructure, but their PCM payloads are never mixed. Speaker identity is determined by the capture channel, not by diarization.

## Client contract

Electron and remote browsers render the same React application and use the same `client` WebSocket protocol. There is no separate mobile viewer API or second answer state.

The contract is `realtime-interview-v5`, validated on both UI and capture `session_ready` frames. Old/missing protocols fail before capture or controls. A 10-second ping/pong heartbeat closes a half-open client socket when the next check sees no reply; browsers may delay timers while suspended. Native upstream WebSocket keepalives remain enabled.

- Every authenticated client receives the same transcript events, append-only answer events and reconnect snapshots.
- Every client may send manual questions, request a host screenshot and end the interview.
- Only Electron has the capture adapter. Its `interviewer` and `candidate` WebSockets carry separate audio. A small control requests each screenshot; the image is uploaded over a separate authenticated HTTP request.
- Electron opens both media sources when it starts and reports `ready`; it drops audio locally while the interview is idle. OpenAI upstreams are not opened until the first active audio chunk.
- AudioWorklet performs PCM conversion off the renderer thread. Readiness requires actual PCM; normal silence remains healthy. Old worklet messages and excessive outgoing buffering are rejected visibly. Each server capture channel owns only one in-flight audio send, keeping control reception independent of provider startup/backpressure. This cannot bound Internet transit delay or replay lost audio.
- Closing the Electron window hides it to the tray. The renderer remains alive and background throttling is disabled so capture continues.
- Desktop and mobile browsers open the fixed server URL. After browser authentication, they discover the single current interview from the server; there is no QR code, share link or session credential in the URL.

The FastAPI room is the sole live state source. Electron does not relay answers to browsers, and browsers do not maintain a parallel answer history. A browser may poll slowly only while waiting for the capture host to create a current interview; the live interview itself is WebSocket-driven.

The first personal release runs one capture device and one current interview, while allowing multiple equal-capability UI clients. Its registry and interview history are deliberately process-local and in-memory: an ordinary network reconnect receives snapshots for the same runtime, but a server restart loses that interview history and its OpenAI conversation. Electron then creates a new current interview. Run one Uvicorn worker and one service replica until shared state is intentionally introduced.

## Upstream contract

### Tool-driven code workspace and screenshot collection

The interview remains the primary flow. A collapsible single-file code pane shares its complete context. Completed answer code blocks can be loaded as an unsaved draft; they are never treated as externally typed or adopted code. Opening the pane enlarges the Electron window within the current display; closing it restores the compact window without clearing the document.

Astra, hosted by Live, uses `update_code` to open and edit the pane. The tool takes the full short file and exact document/input versions read from `search_context`. It commits atomically only while the originating task remains valid. The application computes a diff and retains undo; no patch engine or external editor automation is involved. Source code/SQL must stay out of the Live explanation.

`code_action` retains save, undo, reset, stop and the optional generate/apply/discard preview controls. Only that optional manual preview uses a one-shot Responses HTTP call with `store:false`, `truncation:disabled`, high reasoning and an 8192-token output/reasoning limit. Automatic work and the Deep button use the hosted backend. Previews survive speech changes but require explicit review of the latest context version before adoption. Subsequent manual edits cannot be overwritten. Unsaved renderer drafts survive server updates with a conflict notice.

Each automatic run has one `reveal_id`, so repeated snapshots do not reopen a pane the user collapsed. `last_change` preserves the applied diff and rationale. Undo follows actual commit order and increases the revision; reset changes document identity. These are application document states, not evidence of typing or execution in another editor.

Collect-only screenshots do not request model output and may continue during speech. `answer_screens` freezes selected page IDs and sends the pages together to the hosted backend, including when automatic answers are paused. New pages do not silently change the selected set. All code, proposals and pages use the same reconnect snapshots and process-local lifetime.

### Live and hosted reasoning

The primary connection is `/v1/live/sessions`, configured with `gpt-live-1`, `store:false`, PCM16LE mono at 24 kHz and Responses delegation to `gpt-6-astra`. Startup waits for `session.started`. Continuous audio uses `session.input_audio.append`, including quiet frames; there is no VAD commit, response.cancel or application-controlled audio turn scheduler.

Live controls conversational timing and native delegation. The backend handles technical reasoning, personal background, images and code; its three tools are configured in `delegation.responses`. The Live role and delegation prompt is short; detailed procedures belong in the backend prompt. Reasoning defaults to high, maximum output/reasoning tokens to 8192. The application does not open a third permanent socket or run a second automatic HTTP analysis pipeline.

`session.output_transcript.delta` is the only model answer stream shown to the user. Generated audio is discarded. This is a text-only product experience, not a text-only Live generation setting. Backend text is not forwarded to the answer pane, and backend completion does not terminate a visible caption.

The reader dispatches `response.event` envelopes without awaiting tools. It correlates delegation, response and client-command IDs, reads calls from `response.output_item.done`, returns each `function_call_output` via `response.item.create`, then uses the payload-free `response.create` command to continue after the response is terminal and all tools have returned. Lifecycle `response.output` arrays are empty and cannot be used to discover calls. Cancelled tasks do not continue. Changed speech blocks old writes but permits an otherwise valid existing tool loop to reread full observed transcripts and reconsider the new input. A read cannot revive cancelled work, a different question/document, or an overwritten document revision. Unknown or ambiguous task identity never obtains code write access.

Hosted task lifetime is bounded at 120 seconds across tool continuations. Timeout/terminal completion releases application busy state and forbids late writes; no provider billing cancellation is claimed. Manual HTTP preview has a total deadline as well as transport timeouts. A failed function-result send closes the failed connection so normal recovery can restore the committed document.

Interruption of Live speech does not itself cancel the hosted backend. The app rejects obsolete tool effects using task epoch, observed input version, document identity and revision. Stop prevents a pending code task from committing; it does not claim to cancel all provider computation or billing.

### Candidate transcription

`gpt-live-transcribe` owns only candidate microphone transcription over the existing Realtime transcription socket. It uses native `server_vad` and `delay:low`. Optional comma-separated `OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES` becomes the native `languages` array; the old singular field is not sent. The application does not issue manual commits or run its own silence/segmentation timer.

Its startup waits for `session.updated`; rejection or a 15-second acknowledgement timeout closes the connection before any audio is forwarded. Main and candidate connections have independent locks. Brief successful handshakes do not reset repeated-failure backoff. Capture disconnection or terminal track errors close only that source's upstream; the room stays recoverable in memory.

Each native delta is immediately queued, with its item/turn identity and event ID, for `session.thinking.append` and a hosted-backend reference item. No fixed batching interval or sentence parser is involved, and no response is requested. The FIFO worker only prevents socket backpressure from stopping transcript collection. Native completion supplies the authoritative transcript for that same turn and explicitly supersedes provisional ASR; this is recognition correction, not a new candidate choice. Long individual events are split only to meet Live's append-size limit without losing characters.

Native speech starts reserve ordered history entries before text arrives. Finals can arrive out of order and update their own item rather than clearing another turn. Every observed partial text update changes the input version and invalidates obsolete code; the shared UI uses turn IDs and streaming/completed/interrupted status, including in reconnect snapshots. Event replays are deduplicated by native event ID; equal text in separate events remains repeated speech. A transcription disconnect retains observed partial text as interrupted, never as a provider-final utterance. Native VAD defines speech turns, not guaranteed grammatical sentences.

These mechanisms preserve observed text and enqueue it promptly; they do not guarantee the model has consumed an update at send time or restore audio that was never transcribed. Main Live conversational timing remains independent of the candidate VAD.

If candidate transcription fails, interviewer answers continue and the UI marks candidate context as stale.

## Context contract

Context is read before an interview from `INTERVIEW_CONTEXT_DIR`, defaulting to `apps/server/context`.

- Accepted extensions: `.md`, `.txt`.
- Files are read-only during a session.
- Preserve the complete text of every configured file, including paragraphs and related material. Do not split resumes into snippets or discard content based on keyword matches.
- There is no runtime upload endpoint.
- There is no vector store, embedding pipeline or remote file registry.
- A new interview receives a stable, complete snapshot of the available context. Start a new interview after changing the files.

On every new Live connection, the complete fixed background is sent to the hosted backend as a separate reference message. The same full originals remain available from `search_context`. The current document always comes from application state. The user has accepted Live's automatic compression of older conversation: the application retains complete observed records but does not claim the upstream retains every original turn. Hosted Live Responses configuration does not expose standalone `truncation` or `store` fields; do not invent them.

The runtime retains ordered transcript anchors, corrections, answers, code revisions and screenshots with source/time. Reconnecting closes the old physical socket first, then provides this record and fixed background to the new backend. Live itself receives a reconnect notice and current question, not a fabricated restoration of its old audio timeline. No raw or untranscribed audio is replayed. Server restart still loses process-local history. Original records being retained does not prove that a provider accepted or permanently retained all background.

### Backend tools

- `search_context`: no arguments, returns `{ok: true, documents: [{source, text}], workspace: {...}, transcripts: [...]}`. The workspace contains exact document identity/revision/code, current question and observed context version. The transcript snapshot ensures a refreshed version includes the newly observed text. Empty documents mean no background; never invent missing candidate facts. Authenticated UI snapshots expose document/character counts only, with an empty-background warning.
- `capture_current_screen`: no arguments, requests one discrete image from Electron and sends it to the backend. Capture errors return bounded failures. Images are not sent as Live audio-model inputs.
- `update_code`: requires `document_id`, `base_revision`, `context_version`, `code`, `language`, `explanation`. A preceding context read and current originating task are required. No await occurs between validation and commit. Successful execution updates only the internal code pane; no code is executed.

No keyword filtering, runtime upload, vector store, web lookup, code execution or third-party ASR is added.

### Product extension seams

The product owns context, UI and concrete tool effects; Live and its hosted backend own conversational timing, reasoning and delegation. Keep future code explanations, alternative solutions and step-by-step improvements on this same path. Those features are not implemented yet.

`realtime_context.py` owns the Live and backend behavior prompts, including explanation style. `interview_tools.py` is the application tool boundary. Its static definition table pairs each native function schema with its handler and optional code-pane activity flag. The Live transport advertises and dispatches that table without a per-feature branch. It continues to own call correlation, deduplication, task validity, error handling and backend continuation. Only the three existing tools are enabled; there is no dynamic plugin loader or additional model connection.

Handlers receive an interview-scoped `ToolContext`, including the current workspace reader and task identity. An asynchronous handler must call `require_current()` after waiting and before publishing effects; document writes must also validate the target revision with no await between validation and commit. Feature state belongs to the interview runtime and its snapshots, never the shared tool definitions.

Future features attach at the existing boundaries:

| Addition | Extension point | Retained contract |
| --- | --- | --- |
| Explain an approach, complexity or selected code | Backend instructions and, if needed, an application tool handler | Live supplies prose; code stays in the code pane |
| Show multiple alternative implementations | Extend `CodeWorkspace` with named proposal data anchored to `document_id`, base revision and context version; render in `CodePanel` | Exactly one authoritative current document; a suggestion is not an applied edit |
| Compare or adopt an alternative | Reuse the code-operation path, document commit, diff and undo | Recheck current versions at adoption; do not overwrite manual changes |
| Offer a next-step button | Send an explicit intent and selected document/result reference through the existing client operation path | The model chooses the reasoning and changes; no scripted solution state machine |

`CodePanel.tsx` and its workspace types are already separate from audio capture. New result fields can travel in the existing `code_state` snapshot and be rendered there; do not create a parallel viewer API or client-owned result history. Actual feature work will still add its data fields, handlers and UI, but does not require rebuilding the Live/session architecture. Do not add speculative result stores, version branches, placeholder buttons or unused protocol fields now. Current snapshots and revision history remain in-memory and do not imply restart persistence or executed tests.

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

Live has no per-answer done event. A 1.2-second caption inactivity boundary finalizes a displayed segment; this is only a UI grouping decision, not provider task completion. More captions append another segment. An interviewer input segment or explicit cancel interrupts the current displayed segment. Input transcription uses a similar grouping boundary for history and never triggers application response creation.

Backend tool output is a separate stream and is never classified as a visible answer. A manual operation completes on its backend terminal event, not on a short caption pause. It may finish before Live has completed its explanation. Pause suppresses automatic captions and tool effects while collection continues; an explicit screenshot request may permit its answer. Provider errors are redacted, and only correlated errors fail a pending manual operation.

## Failure behavior

- Core failure: mark the active answer failed; do not call an old HTTP coach.
- Candidate transcription failure: preserve the core session.
- Missing background: report that no background is available; do not invent candidate facts or imply that a partial document is complete.
- Screenshot failure: return a bounded tool error.
- Analysis failure: let the core answer with existing context.
- Invalid interview token: reject before consuming OpenAI resources.
- Reconnect: create or recover only the matching interview state.
- Failed UI sends close the socket to trigger reconnection. Provider sends/close and application HTTP calls are bounded; raw provider errors are not shown.
- Server restart: start a new current interview; do not claim recovery of the previous in-memory conversation.

## Deployment boundary

Local context lives under `apps/server/context`. Production images contain an empty `/app/context` directory and never copy background documents from the checkout or a previous deployment. Production context lives outside `/opt/interview/server` and is mounted read-only through `INTERVIEW_CONTEXT_DIR`; the release preflight verifies that mount before changing the running service.

The deployment workflow builds and tests the shared Vite client, stages it under `apps/server/web`, and uses the authenticated atomic deployment gate before changing deployed source or `.env`. It refuses active interviews, retains source/environment/image rollback artifacts, and changes only `interview_api`. Candidate and stable containers are checked locally and publicly for protocol, models and the unique release ID. The stable container does not retain startup drain mode. Rollback after finalization also requires an idle gate; it must not kill an interview that started on the new version. Legacy servers without the gate require explicit first-upgrade maintenance; see `apps/server/deploy/README.md`. FastAPI serves that build from the same origin as the API and WebSockets. It must never sync API keys, access tokens or private interview materials from the repository.

## Verification

The shared UI has four primary recovery actions: inspect the selected screen, correct context, analyze in depth, and pause/resume answers. Answer rewrites remain attached to the selected response. Pausing retains context collection. Code blocks, tables and lists use safe Markdown rendering; new answers do not move the reader away from a selected older answer.

The client control contract is:

- `quick_answer`: `action` is `answer`, `shorten`, `expand`, `rephrase` or `deep`; optional `response_id` and `question_id` identify the selected target.
- `manual_text`: `kind` is `question`, `correction` or `candidate_context`; correction uses a `question_id` and an explicit or server-resolved `turn_id`. Candidate notes never create an answer.
- `request_screen_capture`: optional `question_id`; the selected source is configured on Electron. Images are posted to `/api/interviews/{id}/screenshots` with the capture capability in the Authorization header. Unknown, inactive or expired requests are rejected.
- `set_answer_hold`: `hold:true` suppresses automatic display and tool effects, sends Live a stop instruction and retains collection; `hold:false` resumes and requests work for the collected question. It does not promise hosted computation cancellation.

Each model/control operation has an `operation_id` and server-owned accepted/running/completed/failed/cancelled state. Start is acknowledged by `interview_state`, not a separate model operation. Reconnecting clients receive operation, question, transcript and answer snapshots; `answer_snapshot_done` marks completion of the answer portion. Sending a WebSocket message is not completion. Question IDs identify observed input anchors; they are not a programmatic classifier for interview semantics.

Audio conversation timing and delegation are owned by Live. Explicit UI requests queue the selected reference and request the hosted backend; candidate text appends only silent context. The application retains authentication, capture isolation, tool side effects and UI operation state.

Per-interview diagnostics count observed Live session seconds, hosted/manual Responses usage, tool failures, reconnects and reported audio gaps. Caption latency is measured from an observed input segment, not a speech-stop benchmark. Usage observations are not a provider invoice; unreported usage on disconnection is unknown.

Automated coverage includes raw Live startup and audio protocol, complete fixed background, silent candidate context, concurrent captions and slow tools, granular function events with empty lifecycle output, duplicate events, task/document/input staleness, manual controls, snapshots, source/token isolation and the deployment gate. Existing UI/capture tests and build remain separate checks. Real Live access, speech quality, response timing, billing and Electron media permissions still require a separately authorized provider/media test.

Official contracts: [primary WebSocket](https://developers.openai.com/api/reference/resources/live/primary-websocket), [delegation](https://developers.openai.com/api/docs/guides/live-delegation), [migration](https://developers.openai.com/api/docs/guides/live-migration).
