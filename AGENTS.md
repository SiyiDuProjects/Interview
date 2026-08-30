# AGENTS.md

给后续 Codex/agent 的项目说明。

## 项目边界

这是 Electron + React + FastAPI 的实时面试辅助项目。当前实现追求一条极简 OpenAI Realtime 主链路，不要把它扩成通用 agent 平台。

必须保持：

- `interviewer` = 系统音频，进入主 Realtime core 并触发文字答案。
- `candidate` = 麦克风，只转写并注入候选人上下文，不触发答案。
- 两路音频按采集来源区分，永远不要混音。
- 输出为 text-only；不播放模型语音。
- 答案历史 append-only；新回答只能追加，不能覆盖已完成回答。

## 模型与上游边界

- 主模型：`OPENAI_REALTIME_MODEL=gpt-realtime-2.1`
- 候选人转写：`OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper`
- reasoning 默认：`OPENAI_REALTIME_REASONING_EFFORT=low`

一次 interview 只有两个长期 OpenAI 上游：

1. `gpt-realtime-2.1` 主会话：面试官音频、输入转写、文字回答和工具。
2. `gpt-realtime-whisper` candidate transcription：麦克风转写后注入主会话。

不要新增第三个常驻 code/deep 模型连接，不要恢复旧 fast/deep HTTP coach pipeline、Deepgram、讯飞、本地 Whisper/Vosk 或其他实时 provider。

## 工具边界

主会话只暴露三个工具：

- `search_context`
- `capture_current_screen`
- `analyze_problem`

`search_context` 读取预存上下文；`capture_current_screen` 请求离散截图；`analyze_problem` 处理算法、复杂 SQL、debug、系统设计和代码追问。工具失败必须回到当前主会话降级，不得切换旧 pipeline。

`analyze_problem` 可以按需发起一次 `gpt-5.6-sol` Responses 请求（`store:false`），但不能创建第三个长期 WebSocket。

## 上下文边界

- 默认上下文目录：`apps/server/context`
- 可用 `INTERVIEW_CONTEXT_DIR` 覆盖。
- 只读取预先放置的 `.md` / `.txt`。
- 不提供运行时文件上传。
- 不引入 vector database、embedding pipeline、OpenAI Files/vector store 或重 RAG。
- Realtime prompt 必须极短，只保留角色、路由、回答风格和禁止编造规则；候选人事实通过 `search_context` 按需查找。
- 私有生产资料应放在部署目录外，并通过只读 `INTERVIEW_CONTEXT_DIR` 挂载；不要提交到 Git。

## 会话与权限

- 所有运行时状态按 `interview_id + token` 隔离。
- 不允许进程级全局 hub 共享候选人资料、截图、工具状态、上游连接或答案。
- `INTERVIEW_ACCESS_TOKEN` 本地可留空；远程生产应配置。
- OpenAI API key 与访问 token 只存在后端/桌面进程环境，不能写入 renderer、日志、文档示例真实值或仓库。
- 远程 `OPENAI_BASE_URL` / `INTERVIEW_API_BASE_URL` 必须使用 HTTPS/WSS；仅 loopback 允许 HTTP/WS。
- 未通过 token 校验时，不得先建立 OpenAI upstream。
- WebSocket 的 session token 必须放在认证首帧，不得放进 query string 或日志。

## 客户端合同

- Electron 与桌面/手机浏览器复用同一个 React 界面和 `client` WebSocket，不维护第二套 mobile/viewer API。
- Electron 是唯一采集宿主，额外持有 `capture_token`；浏览器 API 永远不能返回该 token。
- Electron 启动即初始化两路媒体；空闲时本地丢弃音频，服务端也必须在 `active=false` 时丢弃二进制且不得创建 OpenAI upstream。
- 浏览器使用固定服务器 URL，经 `INTERVIEW_ACCESS_TOKEN` 登录换取 HttpOnly Cookie，再发现单个 current interview；不要加入二维码、配对链接或 URL token。
- 服务端是转写、答案与设备状态的唯一事实源。前端不得用 `localStorage` / `sessionStorage` 维护平行答案历史。
- 个人版只允许一台采集设备和一场 current interview，但允许多个同权 UI 客户端。
- 当前 Registry 是单进程内存态：普通断线可快照恢复，服务重启不可恢复本场历史或 OpenAI 上下文。生产保持单 worker、单副本，除非以后引入共享状态。

## 关键文件

- Frontend：`apps/desktop/src/App.tsx`
- Audio capture：`apps/desktop/src/audioCapture.ts`
- Electron permissions/backend startup：`apps/desktop/electron/main.cjs`
- FastAPI routes：`apps/server/app/main.py`
- Realtime orchestration：`apps/server/app/services/openai_realtime.py`
- Context lookup/prompt：`apps/server/app/services/realtime_context.py`
- Bundled context：`apps/server/context/`
- Architecture contract：`docs/architecture.md`

## 本地启动

后端：

```powershell
cd D:\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

桌面端：

```powershell
cd D:\Projects\Interview\apps\desktop
npm.cmd run dev:desktop
```

桌面端默认连接 `https://interview.reachard.co`。本地开发时显式设置：

```powershell
$env:INTERVIEW_API_BASE_URL="http://127.0.0.1:8000"
npm.cmd run dev:desktop
```

如果 `.venv` 不存在：

```powershell
cd D:\Projects\Interview\apps\server
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell 下使用 `npm.cmd`，不要用会被执行策略拦截的 `npm.ps1`。

## 验证命令

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

新增或改变 Realtime 行为优先补 `tests/test_realtime.py`。源码测试不等于 Electron 媒体权限、视觉和真实 OpenAI API 验证。

## 部署约定

`realtime-interview-v4` 是 breaking protocol。部署顺序必须是后端先上线并通过 `/health` 协议检查，再发布/启动默认连接远程服务的桌面端。

- Workflow：`.github/workflows/deploy-server.yml`
- VPS SSH 用户：`ubuntu`
- Compose 路径：`/home/ubuntu/muxing`
- 部署路径：`/opt/interview/server`
- Compose service/container：`interview_api`
- 端口：`8000`
- 公网域名：`https://interview.reachard.co`

生产 `.env` 只放在 `/opt/interview/server/.env`，不要提交。共享 secrets 使用 `SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_KEY`、`COMPOSE_PATH`；项目专属值优先使用 `INTERVIEW_DEPLOY_PATH`、`INTERVIEW_PUBLIC_HEALTH_URL`。

服务器已有端口不要复用：

- `8080` = sub2api
- `8787` = connection
- `20241` = cloudflared metrics
- `40000` = WARP

## 媒体权限

不要用 Codex in-app browser 或普通浏览器验证双路采集；它们可能返回 `Permission denied`。使用 Electron 桌面端验证 `media`、`display-capture`、`microphone` 和 system audio loopback。

## 编辑原则

- 优先删除兼容层和重复状态，不新增同功能第二条链路。
- 不修改或删除用户未提交的无关文件。
- Windows 文件操作使用 PowerShell 原生命令。
- 删除、迁移或重命名前先核对绝对目标、引用和差异。
- 不把 OpenAI API key、访问 token、简历/JD 或私有项目资料写入前端、日志或 Git。
