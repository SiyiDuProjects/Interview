# Interview Copilot

一个本地 Electron + React + FastAPI 的实时面试辅助工具。当前架构刻意保持简单：两路音频分开采集、两个 OpenAI Realtime 上游、文字答案、三个工具，没有旧 coach pipeline、第三方 ASR、运行时文件上传或向量数据库。

Electron 和浏览器使用同一套 React 界面。Electron 额外承担双路音频与截图采集；桌面或手机浏览器从同一服务器读取同一场面试、看到同一答案时间线，也能执行开始、手动提问、截图和结束操作。没有独立“手机只读版”，也没有二维码或带会话凭证的分享链接。

## 核心架构

- 主上游：`gpt-realtime-2.1`。接收面试官的系统音频，完成输入转写、推理、工具调用和文字回答。
- 候选人上游：`gpt-realtime-whisper`。只转写候选人麦克风，并把最终文本作为上下文注入主会话，不触发回答。
- 两路 PCM 永远不混音；说话人身份来自采集通道。
- 输出固定为 text-only。即使模型支持音频输出，桌面端也不播放模型语音。
- 每次答案都追加到历史末尾；已完成答案不被后续回复覆盖或改写。

```text
system audio ──> gpt-realtime-2.1 ──> text answer
                         │
                         ├── search_context
                         ├── capture_current_screen
                         └── analyze_problem

microphone ──> gpt-realtime-whisper ──> candidate context ──┘
```

OpenAI 官方模型页：[GPT-Realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。

## 三个工具

- `search_context`：在预存的候选人资料中做轻量关键词检索。
- `capture_current_screen`：需要当前题面、白板或代码时请求一次离散截图。
- `analyze_problem`：处理算法、复杂 SQL、debug、系统设计或代码追问；按需调用一次 `gpt-5.6-sol` Responses 请求，但不创建第三个常驻上游。

普通概念题、项目题和行为题由主模型直接回答。工具失败时主会话继续工作，不切回旧 HTTP coach。

## 背景资料

背景资料必须在启动前放入上下文目录：

```text
apps/server/context/
  resume.md
  job-description.txt
  projects.md
```

- 默认目录是 `apps/server/context`，可用 `INTERVIEW_CONTEXT_DIR` 覆盖。
- 只读取 `.md` 和 `.txt`。
- 运行时不提供文件上传，不创建 OpenAI Files/vector store，也不做重 RAG。
- Realtime instructions 只保留极短的角色、路由和安全规则；事实通过 `search_context` 按需取回。
- 修改资料后应开始新的 interview，使会话得到一致的上下文快照。

## 会话隔离

每次 interview 都有独立的 `interview_id` 和 token。主会话、候选人转写、截图请求、工具状态与答案历史都必须按这两个值隔离，不能使用跨用户全局 hub。

`INTERVIEW_ACCESS_TOKEN` 是可选的生产访问密钥：

- 本地仅绑定 `127.0.0.1` 时可以不设置。
- 暴露远程后端时应设置，并在启动 Electron 时提供同一个值。
- OpenAI API key 只保存在后端环境中，绝不进入 renderer 或仓库。

个人版只保留“一台采集设备 + 一场 current interview”：

- Electron 启动后自动初始化麦克风与系统音频，并向服务器报告设备状态；无需每场重复授权。Windows 明确拒绝媒体权限时才显示错误。
- 空闲状态只保持本地媒体就绪，音频块在本地丢弃，服务器也拒绝空闲二进制帧；点击开始后才上传并按需建立 OpenAI 上游。
- 浏览器打开服务器固定网址，首次输入访问密钥后由 HttpOnly Cookie 维持浏览器认证，再从 `/api/interviews/current` 获取当前场次。
- FastAPI 是答案、转写和设备状态的唯一事实源；Electron 与浏览器都不另存一份答案历史。
- 普通网络断开可通过同一场次的服务端快照恢复。服务器当前是单进程内存态；服务重启会丢失本场历史和 OpenAI 上下文，由 Electron 创建新的 current interview。

## 环境变量

在仓库根目录准备 `.env`：

```env
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_REASONING_EFFORT=low
OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper
OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=
OPENAI_CODE_MODEL=gpt-5.6-sol
OPENAI_CODE_REASONING_EFFORT=high
INTERVIEW_CONTEXT_DIR=
INTERVIEW_ACCESS_TOKEN=
INTERVIEW_ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
INTERVIEW_SESSION_TTL_SECONDS=3600
INTERVIEW_SCREENSHOT_MAX_BYTES=5242880
```

`INTERVIEW_CONTEXT_DIR` 留空时使用 bundled `apps/server/context`。生产可以把它指向部署目录外的只读资料目录。
远程 `OPENAI_BASE_URL` 与 `INTERVIEW_API_BASE_URL` 都必须使用 HTTPS；只有 loopback 本地开发地址允许 HTTP。

## 本地启动

后端：

```powershell
cd D:\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如果 `.venv` 不存在：

```powershell
cd D:\Projects\Interview\apps\server
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

桌面端默认连接 `https://interview.reachard.co`：

```powershell
cd D:\Projects\Interview\apps\desktop
npm.cmd install
npm.cmd run dev:desktop
```

强制使用本地后端：

```powershell
cd D:\Projects\Interview\apps\desktop
$env:INTERVIEW_API_BASE_URL="http://127.0.0.1:8000"
$env:INTERVIEW_ACCESS_TOKEN=""
npm.cmd run dev:desktop
```

生产启用访问 token 时，在启动桌面端前把 `INTERVIEW_ACCESS_TOKEN` 设置为服务器配置的同一值。静态访问 token 只由 Electron 主进程用于创建 interview；本场 session token 由 renderer 在 WebSocket 建连后的第一帧发送，不进入 URL。不要把真实 token 写入代码或提交到 Git。

## 媒体权限

双路采集必须在 Electron 桌面窗口验证。Codex in-app browser 或普通浏览器可能因权限策略返回 `Permission denied`。

Electron 需要：

- `media`
- `display-capture`
- `microphone`
- system audio loopback

## 验证

后端：

```powershell
cd D:\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m unittest tests.test_realtime
.\.venv\Scripts\python.exe -m unittest tests.test_interview_flow tests.test_latency
```

前端：

```powershell
cd D:\Projects\Interview\apps\desktop
npm.cmd run build
```

源代码检查不能替代 Electron 中的双路媒体、截图和实际 OpenAI 会话验证。

## 部署

本版客户端使用 breaking protocol `realtime-interview-v4`。发布时必须先部署后端并确认 `/health` 返回该协议，再启动默认连接远程后端的桌面端；否则请按上面的本地开发命令显式连接 `127.0.0.1:8000`。

当前运行时状态只在单个 FastAPI 进程内存中，生产必须保持一个 Uvicorn worker 和一个服务副本；引入共享状态存储前不要横向扩容。

- Workflow：`.github/workflows/deploy-server.yml`
- Compose 路径：`/home/ubuntu/muxing`
- 部署路径：`/opt/interview/server`
- Compose service：`interview_api`
- 服务端口：`8000`
- 公网域名：`https://interview.reachard.co`

生产 `.env` 只放在 `/opt/interview/server/.env`。GitHub Actions 只同步 `apps/server/` 并排除 `.env`；不要把上下文私密资料、API key 或访问 token 提交到仓库。需要私有生产资料时，将 `INTERVIEW_CONTEXT_DIR` 指向部署目录外的只读路径。

## 关键文件

- `apps/desktop/src/App.tsx`
- `apps/desktop/src/audioCapture.ts`
- `apps/desktop/electron/main.cjs`
- `apps/server/app/main.py`
- `apps/server/app/services/openai_realtime.py`
- `apps/server/app/services/realtime_context.py`
- `apps/server/context/`
- `docs/architecture.md`
