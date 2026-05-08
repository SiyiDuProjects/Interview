# Interview Copilot

本项目是本地 Electron + React + FastAPI 的实时面试辅助工具。当前主链路只使用 OpenAI Realtime：

- `interviewer`：系统音频，进入主 Realtime 会话并触发文字回答。
- `candidate`：麦克风，只作为候选人上下文注入主会话，不触发回答。
- 手动输入同样走 Realtime WebSocket 的 text input，不再走旧的 coach HTTP 备用链路。

## 当前能力

- 双路采集：麦克风代表候选人，系统音频代表面试官。
- Realtime 回答：面试官问题触发 `gpt-realtime-2` 文字流式答案。
- Realtime 转写：实时转写使用 `gpt-realtime-whisper`。
- 深度代码/复杂题：Realtime 工具调用 `solve_code_question` 时使用 `gpt-5.5`。
- 背景资料：姓名、目标岗位、简历、JD、补充备注会写入 Realtime instructions。
- 项目上下文：`Innovation AI`、`CanvasBot`、`DiscordBot` 按钮会切换回答范围，并通过 `docs/project-contexts/` 做轻量检索。
- 截图输入：支持“截图上下文”和“截图回答”，截图作为离散 image input 发送给 Realtime。

## 环境变量

复制 `.env.example`：

```powershell
Copy-Item .env.example .env
```

至少填写：

```env
OPENAI_API_KEY=你的 OpenAI API Key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_REALTIME_MODEL=gpt-realtime-2
OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper
OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=zh
OPENAI_CODE_MODEL=gpt-5.5
```

不要把 `.env` 或 OpenAI API key 放到前端。Electron 只连接本地或远程 FastAPI。

## 本地启动

后端：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如果 `.venv` 不存在：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

桌面端：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\desktop
npm.cmd install
npm.cmd run dev:desktop
```

使用远程后端时：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\desktop
$env:INTERVIEW_API_BASE_URL="https://interview.reachard.co"
npm.cmd run dev:desktop
```

如果 `INTERVIEW_API_BASE_URL` 或 `VITE_API_BASE_URL` 是非 localhost URL，Electron 会直接连接远程后端，不会启动本地 FastAPI。

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

## Realtime 主链路

前端：

- `apps/desktop/src/audioCapture.ts` 采集 24kHz PCM。
- `apps/desktop/src/App.tsx` 连接 `/ws/realtime/interview/interviewer` 和 `/ws/realtime/interview/candidate`。
- 手动输入会发送 `manual_text` 到同一个 Realtime WebSocket。

后端：

- `apps/server/app/main.py` 暴露 `/ws/realtime/interview/{speaker}`。
- `apps/server/app/services/openai_realtime.py` 管理 OpenAI Realtime 会话。
- `apps/server/app/services/realtime_context.py` 构建 instructions，并实现 `lookup_candidate_context`。

说话人区分方式：

- `interviewer`：系统音频，触发回答。
- `candidate`：麦克风，只注入上下文，不触发回答。

## 媒体权限

实时麦克风和系统音频必须用 Electron 桌面端测试。不要用 Codex in-app browser 或普通浏览器验证双路采集，它们可能返回 `Permission denied`。

Electron 权限配置在 `apps/desktop/electron/main.cjs`，包括：

- `media`
- `display-capture`
- `microphone`
- system audio loopback

## 测试

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

## CI/CD 部署

后端按 `connection` 项目的方式部署到同一台 VPS，但使用独立服务和独立端口：

- SSH 用户：`ubuntu`
- Compose 路径：`/home/ubuntu/muxing`
- 部署路径：`/opt/interview/server`
- Compose service/container：`interview_api`
- 本机端口：`8000`
- 公网域名：`https://interview.reachard.co`

GitHub Actions workflow 位于 `.github/workflows/deploy-server.yml`。push 到 `main` 且改动包含 `apps/server/**` 或 workflow 时，会自动：

1. 安装 Python 依赖并跑后端测试。
2. rsync `apps/server/` 到 VPS 的 `/opt/interview/server/`。
3. 在 `/home/ubuntu/muxing` 重建 `interview_api`。
4. 检查 `https://interview.reachard.co/health`。

GitHub organization `SiyiDuProjects` 建议配置这些共享 Actions secrets，并授权给需要部署到同一台 VPS 的仓库：

```text
SSH_HOST=49.51.38.235
SSH_PORT=22
SSH_USER=ubuntu
SSH_KEY=<Siyi.pem 的完整私钥内容>
COMPOSE_PATH=/home/ubuntu/muxing
```

Interview 专属 secrets 建议也放在 organization 里，但要用项目前缀，避免和其他项目冲突：

```text
INTERVIEW_DEPLOY_PATH=/opt/interview/server
INTERVIEW_PUBLIC_HEALTH_URL=https://interview.reachard.co/health
```

VPS 上的生产环境变量只放在 `/opt/interview/server/.env`：

```env
OPENAI_API_KEY=你的 OpenAI API Key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_REALTIME_MODEL=gpt-realtime-2
OPENAI_REALTIME_REASONING_EFFORT=low
OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-realtime-whisper
OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE=zh
OPENAI_CODE_MODEL=gpt-5.5
OPENAI_CODE_REASONING_EFFORT=high
OPENAI_CODE_TIMEOUT_SECONDS=45
```

Cloudflare Tunnel 需要添加 public hostname：

```text
Hostname: interview.reachard.co
Service: http://localhost:8000
```

## 相关文件

- `apps/desktop/src/App.tsx`
- `apps/desktop/src/audioCapture.ts`
- `apps/desktop/electron/main.cjs`
- `apps/server/app/main.py`
- `apps/server/app/services/openai_realtime.py`
- `apps/server/app/services/realtime_context.py`
- `docs/project-contexts/`
