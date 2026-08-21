# Canvas Tracker / Smart Learning Planner - Project Memory

## How To Use This Context

If you are an LLM answering questions about this project:

- Answer in first person, as if you built the project.
- Stay factual to the implementation that exists in this repo.
- If a feature is not clearly implemented here, say it is not in the current version instead of inventing it.
- Default to concise interview-style answers first, then expand when asked to deep dive.

## One-Line Summary

This is a full-stack student productivity app that connects to Canvas, syncs course data, projects grades, summarizes course content with AI, and generates a day-by-day study plan across all active courses.

## 30-Second Intro

I built Canvas Tracker, later positioned as Smart Learning Planner, to solve a practical student pain point: Canvas shows raw course data, but it does not help students understand grade trajectory, prioritize work across courses, or turn announcements and files into an actual study plan. The project uses a React + TypeScript frontend and a FastAPI backend. The backend verifies and stores Canvas PATs securely, proxies Canvas data, and the frontend turns that data into grade projections, reminders, AI summaries, and a local study planner.

## Problem Statement

The main problem I wanted to solve was that students usually have to piece together their academic status from multiple disconnected Canvas views:

- assignments live in one place
- announcements live somewhere else
- files and syllabus are separate
- there is no cross-course daily planning layer
- grade visibility is partial because many assignments are unreleased

So the product goal was to turn passive LMS data into an active planning and decision-making tool.

## What The App Does

Core user-facing capabilities:

- connect a Canvas account using a Personal Access Token and Canvas base URL
- sync all active courses
- fetch assignments, announcements, files, and syllabus per course
- convert synced assignments into a normalized grade-tracking model
- compute secured score, floor, ceiling, and projected final outcomes
- show reminders for assignments due within 48 hours that do not have a score yet
- generate AI syllabus summaries and announcement digests
- generate a per-day study plan based on due dates and assignment weight
- let users add announcements/files into the planner as manual tasks

## Tech Stack

Frontend:

- React 18
- TypeScript
- Vite
- localStorage for lightweight persistence

Backend:

- FastAPI
- httpx for Canvas API requests
- SQLAlchemy Core for persistence
- cryptography.Fernet for PAT encryption
- SQLite by default, PostgreSQL-compatible via `DATABASE_URL`

External integrations:

- Canvas LMS REST API
- OpenAI Chat Completions API (`gpt-4o-mini`) called directly from the browser

## Why This Architecture

I split the system into a thin backend plus a stateful frontend.

Why the backend exists:

- Canvas PAT verification should not be trusted purely on the client
- PATs should be encrypted and stored server-side instead of being kept in browser storage
- Canvas requests need a stable integration layer I control

Why the frontend still owns a lot of business logic:

- grade projection, planner generation, drawers, and caches are highly UI-driven
- local-first interactions make the app feel responsive
- I wanted to avoid building unnecessary backend complexity for an MVP/full-stack class project scale

## High-Level Architecture

1. User enters Canvas base URL and PAT in the frontend.
2. Frontend calls `POST /auth/pat_connect`.
3. Backend verifies the PAT via `GET /users/self` on Canvas.
4. Backend encrypts the PAT with Fernet and upserts it into the `accounts` table.
5. Frontend stores only non-secret account metadata locally: `accountId`, `baseUrl`, `userName`.
6. Frontend fetches courses and then syncs assignments for every active course.
7. For the selected course, the frontend also fetches announcements, files, and syllabus, using local cache first and network refresh after.
8. Frontend derives grade metrics, reminders, and planner output from the synced assignment dataset.
9. AI summaries are generated in-browser and cached per course.

## Main Backend Design

Main files:

- `backend/main.py`
- `backend/config.py`
- `backend/database.py`

Key backend responsibilities:

- configure CORS
- validate PATs
- read paginated Canvas resources
- map Canvas payloads into smaller frontend-friendly DTOs
- persist encrypted PATs
- expose simple REST endpoints for the frontend

Implemented API endpoints:

- `GET /health`
- `POST /auth/pat_connect`
- `GET /courses`
- `GET /assignments`
- `GET /announcements`
- `GET /files`
- `GET /syllabus`

### Canvas API Wrapper

`CanvasAPI` in `backend/main.py` is the main integration layer.

Important details:

- it builds requests against `<base_url>/api/v1`
- it uses bearer token auth with the decrypted PAT
- it handles pagination by parsing the `Link` header and following `rel="next"`
- it converts large Canvas payloads into smaller response objects the UI actually needs
- it surfaces 401, 404, and 429 as explicit HTTP errors

### Token Storage And Security

PAT handling is one of the most important backend decisions.

What is implemented:

- PAT is submitted once from the client
- backend verifies it before storage
- backend encrypts it with `cryptography.Fernet`
- encryption key comes from `FERNET_SECRET` or a generated local `backend/.fernet` file
- database stores only the encrypted token, not plaintext
- frontend stores only account metadata, not the PAT

This is a meaningful improvement over a pure frontend approach because the highest-sensitivity credential is not persisted in browser localStorage.

### Persistence Model

The persistence layer is intentionally small.

`accounts` table stores:

- `id`
- `base_url`
- `email`
- `user_name`
- `encrypted_token`
- `created_at`
- `updated_at`

Upsert logic is keyed by `base_url + user_name`, so reconnecting the same Canvas account refreshes the token instead of creating duplicate rows.

## Main Frontend Design

Main files:

- `src/App.tsx`
- `src/services/canvas.ts`
- `src/hooks/useCourseContent.ts`
- `src/hooks/useCourseData.ts`
- `src/utils/calculations.ts`
- `src/utils/scheduler.ts`
- `src/services/ai.ts`

The frontend is the orchestration layer. `App.tsx` coordinates connection state, course sync, content drawers, AI summaries, planner state, and reminder state.

## Frontend State Strategy

I used React state plus custom hooks instead of adding Redux/Zustand because:

- the app is medium complexity, not huge
- state ownership is still understandable inside one main composition root
- custom hooks were enough to isolate the heavier logic

State split:

- account and selected course state
- synced assignments grouped by course
- per-course content caches for announcements/files/syllabus
- derived grade metrics
- planner schedule and manual tasks
- AI summary cache
- UI state for drawers, modals, search, and status messages

## Data Flow On The Frontend

### 1. Connection Flow

- user submits base URL + PAT
- frontend calls `connectWithPAT`
- backend returns `accountId`, `baseUrl`, `userName`
- frontend saves that metadata in `SLP_CANVAS_ACCOUNT`

### 2. Course Sync Flow

- once connected, frontend calls `fetchCourses`
- if no course is selected yet, it auto-selects the first course
- then it iterates through all active courses and fetches assignments for each one
- assignment sync is guarded by a `Set` ref to avoid duplicate in-flight syncs

### 3. Selected Course Content Flow

For announcements, files, and syllabus:

- read from localStorage cache first
- immediately render cached content if present
- then fire background refresh from the API
- update UI and cache after fetch returns

This pattern improves perceived performance and reduces blank states during navigation.

## Grade Tracking Model

The grade system is not a raw mirror of Canvas. I map Canvas assignments into the app's own course/category/item model so the UI can reuse the same grade-calculation engine for both sample data and live Canvas data.

### Assignment Transformation

All synced Canvas assignments are transformed into a single category called `Canvas Assignments`.

Because Canvas assignment weights are not consistently available in the current integration payload, I normalize assignment points into a 100-point course scale:

- sum all assignment `points_possible`
- compute normalization factor `100 / totalPoints`
- each assignment gets `maxCoursePoints = points_possible * normalization`

That lets me compute comparable course-level metrics even when the source system only gives raw points.

### Metrics I Compute

From `src/utils/calculations.ts`:

- `secured`: points already guaranteed from released work
- `floor`: same as secured, assuming zero on unreleased work
- `ceiling`: secured plus all unreleased weight, assuming perfect scores
- `releasedAverage`: weighted average over released items
- projection by category average
- projection by uniform ratio assumption

### Projection Strategies

I implemented two forecasting strategies because a single forecast is too brittle:

1. `category-average`
   - unreleased items inherit the average performance of released items in that category
   - fallback is overall released average if the category has no released items

2. `uniform`
   - unreleased items all use the same user-controlled ratio
   - useful for "what if I average 85% from now on" style scenarios

This was a good interview talking point because it shows I separated deterministic metrics from scenario-based forecasting.

## Planner Algorithm

The scheduler is implemented in `src/utils/scheduler.ts`.

What it does:

- takes assignments from all synced courses
- ignores empty input or invalid daily limit
- builds a calendar from today to the latest due date
- gives each day a fixed hour budget
- estimates assignment study time proportional to assignment points
- allocates time backward from each assignment's due date toward today
- if capacity is insufficient, spills leftover time onto today as a fallback

### Why I Chose This Approach

I intentionally used a heuristic instead of a complex optimization solver.

Reasons:

- easy to explain and debug
- deterministic output
- fast enough for client-side recomputation on every slider change
- good product value for MVP scope

### Planner Inputs

- all assignments from all active courses
- `dailyLimitHours` chosen by the user
- assignment `pointsPossible`
- assignment due date

### Planner Outputs

Each day contains:

- date
- a list of scheduled tasks
- each task includes course, assignment, due date, and expected hours

### Manual Task Integration

Users can also add tasks from announcements or files into the planner.

Those manual tasks store:

- source type
- source id
- course id and course name
- title
- estimated hours
- scheduled date
- status (`pending` or `done`)

This lets the planner combine auto-generated study work with user-curated tasks.

## Reminder Logic

Reminder logic is intentionally simple and useful:

- if an assignment is due within 48 hours
- and Canvas does not show a submitted/released score
- then show a reminder banner

This gives the dashboard an actionable signal instead of just passive reporting.

## AI Feature Design

AI support is implemented in `src/services/ai.ts`.

Implemented behavior:

- API key is stored in browser localStorage under `OPENAI_API_KEY`
- the key is only base64-encoded, not securely encrypted
- summaries are generated by direct browser calls to OpenAI
- model used is `gpt-4o-mini`
- syllabus summary and announcement digest are cached per course

Two AI use cases:

- summarize a syllabus into concise bullets with deadlines and grading cues
- summarize the latest announcements into a digest

### Why AI Calls Bypass The Backend

This was a deliberate tradeoff:

- simpler backend
- faster iteration
- no need to proxy LLM traffic server-side

Downside:

- browser-side API key storage is weaker from a security standpoint

If asked in an interview, the honest answer is that this is acceptable for a prototype or student tool, but in production I would move LLM calls behind the backend and use user-scoped server-side credentials or a proxy.

## Caching Strategy

I used localStorage as a lightweight client cache and persistence layer.

Important keys:

- `SLP_CANVAS_ACCOUNT`
- `SLP_SELECTED_COURSE`
- `SLP_PLANNER_CACHE`
- `SLP_MANUAL_TASKS`
- `anns_<courseId>`
- `files_<courseId>`
- `syllabus_<courseId>`
- `ai_syl_<courseId>`
- `ai_anns_<courseId>`
- `OPENAI_API_KEY`

Why localStorage:

- implementation speed
- enough for small to medium per-user payloads
- persistence across refreshes
- zero extra infrastructure

If asked what I would change later, I would consider IndexedDB for larger cached payloads and add cache invalidation rules beyond simple overwrite-on-refresh.

## Strong Project Talking Points

Good points to emphasize in an interview:

- I took an existing frontend-style MVP and turned it into a real full-stack product
- I added a security boundary for Canvas PATs instead of storing them in the browser
- I normalized raw LMS data into a reusable grade domain model
- I built both analytical features and workflow features, not just dashboards
- I balanced backend responsibility and frontend responsiveness intentionally
- I used caching to improve perceived performance
- I designed for cross-course planning, which Canvas itself does not provide

## Tradeoffs And Honest Limitations

Be explicit about these if asked.

### What Is Strong

- clear separation between Canvas credential storage and UI logic
- thin backend keeps system understandable
- local-first UX makes the app feel responsive
- forecasting and planning features create real user value beyond CRUD

### What Is Still Limited

- there are no automated tests in the current repo
- scheduler is heuristic-based, not an optimal planner
- AI key is stored on the client, which is not production-grade
- grade normalization uses raw points, not full Canvas grading-rule semantics
- there is no multi-user auth/session layer beyond stored Canvas accounts
- no background sync, push notifications, or webhook-based updates

### Important Accuracy Note

The backend explicitly surfaces rate-limit errors, but it does not automatically retry after 429 in the current implementation. Do not claim automatic retry logic exists.

## What I Would Improve Next

If asked about next steps, good answers are:

1. Move OpenAI calls behind the backend for better key security and quota control.
2. Add automated tests for grade calculations, scheduler behavior, and API integration boundaries.
3. Support richer grading semantics from Canvas, such as assignment groups and real weighting rules.
4. Add user authentication instead of relying only on account ids stored in localStorage.
5. Improve the scheduler with priority, estimated difficulty, and workload balancing across days.
6. Add background refresh and notification mechanisms.

## Interview-Ready Deep Dive Topics

### 1. Why a thin backend instead of a heavy backend?

Because the backend's core job is trust and integration, not UI orchestration. Credential verification, encryption, and Canvas API proxying belong on the server. Forecasting, planner interactions, drawers, and caches are highly interactive and benefit from client-side derivation.

### 2. Why normalize assignments onto a 100-point scale?

Canvas returns raw assignment points, but the app needs a consistent course-level metric for secured/floor/ceiling/projection math. Normalizing to 100 makes different assignments comparable and lets the same calculation engine work across sample data and synced data.

### 3. Why two projection strategies?

Because forecasting unreleased work is assumption-heavy. Category-average is grounded in actual past performance, while uniform projection gives the user a controllable scenario tool.

### 4. Why cache course content locally?

Announcements, files, and syllabus are read-heavy and change relatively slowly. Cache-first rendering reduces wait time and avoids empty UI while fresh data is loading.

### 5. Why not store the PAT in the browser?

Because it is the highest-sensitivity credential in the system. Storing it server-side in encrypted form reduces exposure and gives me a cleaner trust boundary.

## Short "Tell Me About The Project" Answer

I built a full-stack Canvas companion app that turns raw LMS data into a planning workflow. On the backend, I verify Canvas PATs, encrypt and store them, and expose simplified endpoints for courses, assignments, announcements, files, and syllabus. On the frontend, I sync that data into a local grade model, compute score projections, detect urgent deadlines, generate AI summaries, and build a day-by-day study plan across all courses. The most important engineering decisions were creating a secure token boundary, normalizing Canvas data into a reusable domain model, and using a lightweight heuristic scheduler that could recompute instantly in the browser.

## Short "What Was The Hardest Part?" Answer

The hardest part was turning Canvas's raw data into something actionable. Canvas gives you assignments, scores, and content, but not a clean planning model. I had to design a normalized course/item structure, make reasonable projection assumptions for unreleased work, and build a scheduling heuristic that balanced simplicity, speed, and usefulness.

## Short "What Are You Most Proud Of?" Answer

I am most proud that the project goes beyond a dashboard. It does not just display Canvas data; it transforms it into forecasts, reminders, summaries, and a concrete daily plan. That makes it a stronger product and a stronger engineering story.

## If Asked About Files

Useful code references:

- frontend orchestration: `src/App.tsx`
- Canvas client bridge: `src/services/canvas.ts`
- course content caching: `src/hooks/useCourseContent.ts`
- course metrics state: `src/hooks/useCourseData.ts`
- grade math: `src/utils/calculations.ts`
- planner heuristic: `src/utils/scheduler.ts`
- AI integration: `src/services/ai.ts`
- backend API layer: `backend/main.py`
- backend config: `backend/config.py`
- backend persistence and encryption: `backend/database.py`

## Boundaries: Do Not Claim These Unless You Added Them Later

- no production auth system
- no test suite
- no websocket/live updates
- no automatic retry queue for Canvas rate limits
- no server-side AI proxy in the current code
- no advanced optimization solver for scheduling

## Final Memory Anchor

The clearest way to describe this project is:

"I built a full-stack learning planner on top of Canvas. The backend securely verifies and stores Canvas credentials, and the frontend converts synced LMS data into grade projections, AI summaries, reminders, and a practical day-by-day study schedule."
