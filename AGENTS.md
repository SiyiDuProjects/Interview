# AGENTS.md

给后续 Codex/agent 的项目说明。

## 项目概况

这是一个本地 Electron + React + FastAPI 的实时面试辅助项目。当前实时主链路只使用 OpenAI Realtime：

- `interviewer` = 系统音频，进入主 Realtime 会话并触发文字答案。
- `candidate` = 麦克风，只作为候选人上下文注入主会话，不触发答案。
- 手动输入走同一个 Realtime WebSocket 的 `manual_text` text input。

不要把两路音频混成一路。说话人区分依赖采集来源。

## 模型边界

- 实时回答：`OPENAI_REALTIME_MODEL=gpt-realtime-2`
- 实时转写：`OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper`
- 代码/复杂题深度工具：`OPENAI_CODE_MODEL=gpt-5.5`

不要重新引入旧 fast/deep HTTP coach pipeline、第三方实时转写 provider，或旧的小模型回答配置。

## 关键文件

- Frontend: `apps/desktop/src/App.tsx`
- Audio capture: `apps/desktop/src/audioCapture.ts`
- Electron permissions/backend startup: `apps/desktop/electron/main.cjs`
- FastAPI routes: `apps/server/app/main.py`
- Realtime orchestrator: `apps/server/app/services/openai_realtime.py`
- Realtime instructions/context lookup: `apps/server/app/services/realtime_context.py`
- Project facts: `docs/project-contexts/`

## 本地启动

后端：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

桌面端：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\desktop
npm.cmd run dev:desktop
```

如果 `.venv` 不存在，用 bundled Python 创建：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 验证命令

后端：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m unittest tests.test_realtime
.\.venv\Scripts\python.exe -m unittest tests.test_interview_flow tests.test_latency
```

前端：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\desktop
npm.cmd run build
```

PowerShell 下不要用 `npm`，会被执行策略拦截；用 `npm.cmd`。

## 部署约定

后端 CI/CD 按 `connection` 项目的方式部署，但服务独立：

- GitHub workflow: `.github/workflows/deploy-server.yml`
- VPS SSH 用户：`ubuntu`
- VPS Compose 路径：`/home/ubuntu/muxing`
- VPS 部署路径：`/opt/interview/server`
- Compose service/container：`interview_api`
- 端口：`8000`
- 公网域名：`https://interview.reachard.co`

服务器已有端口不要复用：

- `8080` = sub2api
- `8787` = connection
- `20241` = cloudflared metrics
- `40000` = WARP

生产 `.env` 只放在 `/opt/interview/server/.env`，不要提交到 GitHub。GitHub Actions 只通过 rsync 同步 `apps/server/`，并排除 `.env` / `.env.*`。

GitHub organization secrets 约定：

- 共享：`SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_KEY`、`COMPOSE_PATH`
- Interview 专属：优先用 `INTERVIEW_DEPLOY_PATH`、`INTERVIEW_PUBLIC_HEALTH_URL`

workflow 兼容仓库级 `DEPLOY_PATH` / `PUBLIC_HEALTH_URL` 作为 fallback。不要在 organization 里用通用 `DEPLOY_PATH` / `PUBLIC_HEALTH_URL` 表示项目专属值，否则多个项目会互相冲突。

Electron 支持远程后端：如果 `INTERVIEW_API_BASE_URL` 或 `VITE_API_BASE_URL` 是非 localhost URL，会直接连接远程 API，不启动本地 FastAPI。

## Realtime API 注意事项

- 默认模型：`OPENAI_REALTIME_MODEL=gpt-realtime-2`。
- 转写模型：`OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper`。
- 当前后端 payload 使用当前 API 接受的字段：
  - `modalities: ["text"]`
  - `input_audio_format: "pcm16"`
  - `turn_detection`
  - `input_audio_transcription`，仅 candidate/context 通道使用
- 不要重新引入 `session.type`、`session.output_modalities`、嵌套 `session.audio`，这些字段已在实际测试中被 API 拒绝。
- `response.create` 使用 `response: { modalities: ["text"] }`。

## 媒体权限注意事项

不要用 Codex in-app browser 或普通浏览器验证双路采集。它们可能返回 `Permission denied`。

用 Electron 桌面端测试，因为 `apps/desktop/electron/main.cjs` 配置了：

- `media`
- `display-capture`
- `microphone`
- system audio loopback

如果用户报告“麦克风和系统音频都没有启动成功”，先确认是不是在 Electron 桌面窗口里运行。

## 当前限制

- 第一版 Realtime 不做结构化 JSON 输出，只输出可口述文字答案。
- 截图输入是离散 image input，不是连续视频理解。
- 背景资料是 instructions + 轻量关键词检索，不是向量数据库。

## 编辑原则

- 新增 Realtime 行为优先加测试到 `tests/test_realtime.py`。
- 不要把 OpenAI API key 放到前端；浏览器只连本地 FastAPI。
- Windows 命令优先使用 PowerShell 原生命令，避免破坏性 git/文件操作。
