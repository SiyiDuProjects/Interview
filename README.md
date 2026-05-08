# Interview Copilot

本项目是一个本地桌面端实时面试辅助工具。桌面端使用 Electron + React，服务端使用 FastAPI。当前实时主链路已经改为 OpenAI Realtime：系统音频进�?Realtime 会话并流式返回文字答案，候选人麦克风内容只作为上下文�?
## 当前能力

- 双路采集：麦克风表示候选人，系统音频表示面试官�?- Realtime 回答：面试官问题触发 `gpt-realtime-2` 文字流式答案�?- 手动输入：没有电话或语音时，可以手动输入面试官问题测试回答链路�?- 背景资料：姓名、目标岗位、简历、JD、补充备注会写入 Realtime instructions�?- 项目上下文：`Innovation AI`、`CanvasBot`、`DiscordBot` 按钮会切换回答范围，并通过轻量工具查找 `docs/project-contexts/` 下的项目资料�?- 截图输入：支持“截图上下文”和“截图回答”，把当前屏幕截图作�?Realtime image input�?- 旧链路保留：`/api/coach/respond`、detail SSE、旧 `/ws/transcribe` 仍保留作备用和回归测试�?
## 运行方式

### 1. 准备环境变量

复制 `.env.example`�?
```powershell
Copy-Item .env.example .env
```

至少填写�?
```env
OPENAI_API_KEY=你的 OpenAI API Key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_REALTIME_MODEL=gpt-realtime-2
```

�?ASR 变量 `DEEPGRAM_*`、`XFYUN_*` 仍可保留，但�?Realtime 主链路不依赖它们�?
### 2. 安装后端依赖

如果 `apps/server/.venv` 不存在：

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 启动后端

```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

### 4. 启动桌面�?
另开一�?PowerShell�?
```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\desktop
npm.cmd install
npm.cmd run dev:desktop
```

实时麦克风和系统音频必须�?Electron 桌面窗口测试。不要用 Codex in-app browser 或普通浏览器测试双路采集，它们可能没有麦克风/屏幕音频权限，会显示 `Permission denied`�?
## 无语音时怎么测试

如果暂时没有电话或面试音频：

1. 启动后端�?Electron 桌面端�?2. 可以不点“开始对话”，直接在左下输入框输入面试官问题并发送�?3. 未进�?Realtime 会话时，手动输入会走�?`/api/coach/respond` 备用链路�?4. 如果要测�?Realtime 手动文字流式回答，需要先�?Realtime session 在线；目前仍依赖“开始对话”成功建立采集会话。没有可用媒体权限时，这部分会被浏览器权限卡住�?
后续可以加一个“文�?Realtime 模式”，�?Realtime WebSocket 独立于音频采集启动�?
## Realtime 主链�?
前端�?
- `apps/desktop/src/audioCapture.ts` 采集 24kHz PCM�?- `apps/desktop/src/App.tsx` 连接 `/ws/realtime/interview/interviewer` �?`/ws/realtime/interview/candidate`�?- 右侧优先显示 `RealtimeAnswer` 流式文本卡�?
后端�?
- `apps/server/app/main.py` 暴露 `/ws/realtime/interview/{speaker}`�?- `apps/server/app/services/openai_realtime.py` 管理 OpenAI Realtime 会话�?- `apps/server/app/services/realtime_context.py` 构�?instructions，并实现 `lookup_candidate_context` 轻量背景检索�?
说话人区分方式：

- `interviewer`：系统音频，触发回答�?- `candidate`：麦克风，只注入上下文，不触发回答�?
## 截图输入

桌面端提供两个按钮：

- `截图上下文`：把截图加入当前 Realtime 会话，不立刻生成答案�?- `截图回答`：把截图加入会话并立即触发文字回答�?
截图是离散图片输入，不是连续视频流。建议只在题目、白板、代码片段需要视觉信息时使用�?
## 测试

后端测试�?
```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m unittest tests.test_realtime
.\.venv\Scripts\python.exe -m unittest tests.test_interview_flow tests.test_latency
```

前端构建�?
```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\desktop
npm.cmd run build
```

## 常见问题

### 页面显示 `Permission denied`

这是媒体权限问题，不�?OpenAI Realtime 问题。请�?Electron 桌面端测试，不要�?Codex in-app browser。Electron 已配置自动允�?`media`、`display-capture`、`microphone` 权限，并使用系统音频 loopback�?
### 页面显示 `invalid_model`

检�?`.env`�?
```env
OPENAI_REALTIME_MODEL=gpt-realtime-2
```

当前默认模型 ID �?`gpt-realtime-2`。如果你显式配置了旧值，重启后端后才会生效�?
### 后端启动失败，提示找不到 python

本机可能没有系统 `python`。用项目虚拟环境�?
```powershell
cd C:\Users\Administrator\Desktop\Projects\Interview\apps\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Electron 启动时也会优先使�?`apps/server/.venv/Scripts/python.exe`�?
### 手动输入能用，实时采集不�?
通常说明 OpenAI 回答链路正常，问题在媒体权限、系统音频采集或桌面端权限。优先检查是否在 Electron 桌面端运行�?
## 相关文件

- `apps/desktop/src/App.tsx`
- `apps/desktop/src/audioCapture.ts`
- `apps/desktop/electron/main.cjs`
- `apps/server/app/main.py`
- `apps/server/app/services/openai_realtime.py`
- `apps/server/app/services/realtime_context.py`
- `docs/project-contexts/`

