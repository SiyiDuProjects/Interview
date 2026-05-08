# AGENTS.md

给后续 Codex/agent 的项目说明。

## 项目概况

这是一个本地 Electron + React + FastAPI 的实时面试辅助项目。当前实时主链路使用 OpenAI Realtime：

- `interviewer` = 系统音频，进入主 Realtime 会话并触发文字答案。
- `candidate` = 麦克风，只作为候选人上下文注入主会话，不触发答案。
- 旧 `/api/coach/respond`、detail SSE、旧 `/ws/transcribe` 保留作备用和测试回归。

不要把两路音频混成一路。说话人区分依赖采集来源。

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

## Realtime API 注意事项

- 默认模型：`OPENAI_REALTIME_MODEL=gpt-realtime-2`。
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
- 没有语音/媒体权限时，Realtime 手动文字模式还没有完全独立出来；未开始 Realtime 会话时，手动输入会走旧 `/api/coach/respond` 备用链路。
- 截图输入是离散 image input，不是连续视频理解。
- 背景资料是 instructions + 轻量关键词检索，不是向量数据库。

## 编辑原则

- 保留旧 coach pipeline，除非用户明确要求删除。
- 新增 Realtime 行为优先加测试到 `tests/test_realtime.py`。
- 不要把 OpenAI API key 放到前端；浏览器只连本地 FastAPI。
- Windows 命令优先使用 PowerShell 原生命令，避免破坏性 git/文件操作。
