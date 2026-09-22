# Interview Copilot

一个本地 Electron + React + FastAPI 的实时面试辅助工具。当前架构刻意保持简单：两路音频分开采集、Live 与候选人转写两个长期上游、文字答案、三个工具，没有旧 coach pipeline、第三方 ASR、运行时文件上传或向量数据库。

Electron 和浏览器使用同一套 React 界面。Electron 额外承担双路音频与截图采集；桌面或手机浏览器从同一服务器读取同一场面试、看到同一答案时间线，也能执行开始、手动提问、截图和结束操作。没有独立“手机只读版”，也没有二维码或带会话凭证的分享链接。

## 核心架构

- 主连接：`gpt-live-1`，接收面试官的连续系统音频，自行掌握接话、打断和后台委派时机。应用显示 Live 输出字幕，丢弃输出音频，不播放模型语音。
- 托管推理后台：`gpt-6-astra`，默认 `high`、每次最多 8192 token，处理技术推理、个人资料、截图和代码。由 Live 管理连接及上下文，解释和后台工作可以同时进行。
- 候选人连接：`gpt-live-transcribe`，原生 `server_vad` 分段；每个转写 delta 立即同步到后台及 Live 的静默上下文，不等停顿、不定时合并、不主动触发答案。原生 completed 确认或修正同一段文字。
- 应用只维护两个长期上游；两路 PCM 永远不混音。普通问答、追问和代码共用同一场面试。
- 回答历史按字幕段追加。Live 没有逐回答的 done 事件，应用以短暂停顿划分显示段；段结束不代表后台任务结束。

```text
系统音频 → GPT-Live → 双语文字解释
              ↕ 原生委派，交流与后台并行
          托管 Astra → 资料 / 截图 / 更新代码区
麦克风 → Live Transcribe（原生分段）→ 转写增量 → Live / Astra 静默上下文
```

[Live 官方委派文档](https://developers.openai.com/api/docs/guides/live-delegation)。不再维护主会话 VAD 回合调度、取消确认等待或串行的“HTTP 分析后再续答”流程。

## 三个后台工具

- `search_context`：无参数读取完整固定背景资料和当前题目、代码及版本。
- `capture_current_screen`：请求一张离散截图，图片只发送给推理后台。
- `update_code`：提交单文件代码或 SQL，检查任务、输入和文档版本后自动打开代码区，保留差异和撤销。文字回答只解释思路，不重复代码。

小文件直接返回完整新内容，应用计算差异；不引入复杂 patch 引擎。手动保存、撤销、换题和追问会阻止旧任务覆盖新代码。折叠入口仍可手动生成待采用建议，只有这个可选按钮使用一次性 Responses HTTP 请求；自动流程不使用它。

## 背景资料

背景资料必须在启动前放入上下文目录：

```text
apps/server/context/
  resume.md
  job-description.txt
  projects.md
```

- 默认目录是 `apps/server/context`，可用 `INTERVIEW_CONTEXT_DIR` 覆盖。
- 只读取 `.md` 和 `.txt`，完整保留文件正文、段落和相关资料，不把简历、项目或 JD 切成关键词命中的碎片。
- 运行时不提供文件上传，不创建 OpenAI Files/vector store，也不做重 RAG。
- Live 使用简短的角色、回答风格及委派提示；详细操作规则在 Astra 后台。
- 完整资料在每次主连接启动时作为后台独立消息提供，`search_context` 随时返回本场固定的完整原文。当前代码以应用文档为准。
- 接受 Live 自动压缩旧对话，不承诺模型始终持有整场原文；应用仍保留本场已观察到的完整记录，不按关键词裁剪资料。托管接口不提供 `truncation:disabled` 参数，不伪造这个配置。
- 缺少资料或无法完整传递时应明确说明，不以截取的片段冒充完整上下文，不补造个人事实。
- 修改资料后应开始新的 interview，使会话得到一致的上下文快照。

主会话重连会在关闭旧连接后，向推理后台重新注入完整背景与本场已记录的对话、答案草稿、分析和截图；Live 只收到恢复提示与当前问题；未完成回答会明确标为中断。原始音频和未转写部分无法恢复，服务重启仍会失去本场内存历史。截图保留来源、时间和题目归属；模型建议不等于候选人已经说过或采用。

## 面试中的应急操作

主界面保留「看题」「纠正」「深入」「先别答」四个动作；答案上的重答、简短、展开和换说法作用于当前选中的答案。纠正入口支持改题目、补条件、补充自己的事实、输入新问题和回到之前的问题。「先别答」继续采集上下文，「现在回答」使用期间收集的当前问题继续回答。服务端报告真实的进行中/完成/失败/取消状态，浏览旧答案时新答案不会自动抢走阅读位置。

「深入分析」发送给 Live 托管的 Astra。工具在独立任务中运行，不阻塞字幕读取。新问题或更正使旧代码任务失效。文字区显示解释、表格和列表，代码只在独立代码区展示。

截图先在设备详情中选择屏幕或窗口，离散截图通过独立 HTTP 请求上传。静音、音轨中断、AudioContext 停止和连接中断分别报告；恢复单路音频时保留健康的一路。截图上传不能挤占语音 WebSocket。采集权限、实际画质和双路音频仍需在获准的 Electron 会话中验证。

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
OPENAI_LIVE_MODEL=gpt-live-1
OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-live-transcribe
OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES=
OPENAI_CODE_MODEL=gpt-6-astra
OPENAI_CODE_REASONING_EFFORT=high
INTERVIEW_CONTEXT_DIR=
INTERVIEW_ACCESS_TOKEN=
INTERVIEW_ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
INTERVIEW_SESSION_TTL_SECONDS=3600
INTERVIEW_SCREENSHOT_MAX_BYTES=5242880
```

本地 `INTERVIEW_CONTEXT_DIR` 留空时使用 `apps/server/context`。生产镜像不包含资料正文，必须通过只读挂载提供资料；宿主机资料目录应位于部署目录外，并用 `INTERVIEW_CONTEXT_DIR` 指向容器内的挂载路径。发布预检会确认资料挂载得到保留。
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

桌面端默认连接 `https://interview.siyidu.com`：

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

连接生产服务时，服务器必须配置 `INTERVIEW_ACCESS_TOKEN`，桌面端使用相同值。Electron 主进程启动时从仓库根目录的私有 `.env` 读取 `INTERVIEW_API_BASE_URL` 和 `INTERVIEW_ACCESS_TOKEN`；显式进程环境变量优先，包括本地开发时设置的空 token。不会从该文件加载 OpenAI API key 或其他变量。静态访问 token 只由 Electron 主进程用于创建 interview；本场 session token 由 renderer 在 WebSocket 建连后的第一帧发送，不进入 URL。不要把真实 token 写入代码或提交到 Git。

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
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

前端：

```powershell
cd D:\Projects\Interview\apps\desktop
npm.cmd run test:ui
npm.cmd run test:capture
npm.cmd run build
```

源代码检查不能替代 Electron 中的双路媒体、截图和实际 OpenAI 会话验证。

## 部署

本版客户端使用 breaking protocol `realtime-interview-v5`。发布时必须先部署后端并确认 `/health` 返回该协议，再启动默认连接远程后端的桌面端；否则请按上面的本地开发命令显式连接 `127.0.0.1:8000`。UI 与采集端在认证握手中拒绝旧协议，不能把健康接口 HTTP 200 当作版本兼容。

本轮完整审计、修复证据和仍未验证的实战风险见 [可靠性审计](docs/reliability-audit.md)。音频转换使用 AudioWorklet；网络半断、采集断开、模型握手失败和后台任务超时会显式提示。源码回归及离线合成音频验证不等于真实面试验收。

当前运行时状态只在单个 FastAPI 进程内存中，生产必须保持一个 Uvicorn worker 和一个服务副本；引入共享状态存储前不要横向扩容。部署先通过认证的原子 gate 拒绝正在进行的面试，再备份源码、环境和旧镜像；校验协议、模型和 release ID，失败时在安全空闲条件下回滚。首次升级旧的无 gate 服务需要明确维护窗口，见 [部署说明](apps/server/deploy/README.md)。

- Workflow：`.github/workflows/deploy-server.yml`
- Compose 路径：`/home/ubuntu/siyi`
- 部署路径：`/opt/interview/server`
- Compose service：`interview_api`
- 服务端口：`8000`
- 公网域名：`https://interview.siyidu.com`

生产 `.env` 只放在 `/opt/interview/server/.env`。GitHub Actions 只同步 `apps/server/` 并排除 `.env`；不要把上下文私密资料、API key 或访问 token 提交到仓库。需要私有生产资料时，将 `INTERVIEW_CONTEXT_DIR` 指向部署目录外的只读路径。

生产资料约定放在宿主机 `/opt/interview/private/context`，只读挂载到容器 `/run/interview-context`，并设置 `INTERVIEW_CONTEXT_DIR=/run/interview-context`；文档约定不能替代对当前部署挂载的核验。完整背景传递使用该目录的本场资料快照，设备详情显示实际加载的资料数量与字符数。个人经历回答依据完整资料和候选人实际补充，答案每段英文后紧跟对应中文，不补造资料中未提供的数据。本地修改与生产生效需分别验证。私有 `context/resume.txt` 同时排除于 Git 和 Docker 构建上下文。

`apps/mac-glass-ui` 保留的是旧独立原型，使用已移除的接口，不能作为当前面试客户端；当前桌面/浏览器入口统一为 `apps/desktop`。

## 关键文件

- `apps/desktop/src/App.tsx`
- `apps/desktop/src/audioCapture.ts`
- `apps/desktop/electron/main.cjs`
- `apps/server/app/main.py`
- `apps/server/app/services/openai_realtime.py`
- `apps/server/app/services/realtime_context.py`
- `apps/server/context/`
- `docs/architecture.md`
