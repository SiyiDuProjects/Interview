# AGENTS.md

给后续 Codex/agent 的项目说明。

## 项目边界

这是 Electron + React + FastAPI 的实时面试辅助项目。当前实现追求一条极简 GPT-Live 与托管 Astra 主链路，不要把它扩成通用 agent 平台。

产品原则：面试是范围集中的场景，优先保证当前题目、代码和个人资料准确连续；允许 Live 自动压缩旧对话，由模型理解、推理和回答。程序负责可靠采集、传递、保留上下文与必要的应急操作；避免用碎片检索、静默裁剪或复杂规则编排丢掉模型本来可以理解的信息。

必须保持：

- `interviewer` = 系统音频，进入主 Live 会话 并触发文字答案。
- `candidate` = 麦克风，只转写并注入候选人上下文，不触发答案。
- 两路音频按采集来源区分，永远不要混音。
- 输出为 text-only；不播放模型语音。
- 答案历史 append-only；新回答只能追加，不能覆盖已完成回答。

## 模型与上游边界

- 主模型：`OPENAI_LIVE_MODEL=gpt-live-1`，`/v1/live/sessions`，等待 `session.started` 后发送连续音频。
- 托管后台：`OPENAI_CODE_MODEL=gpt-6-astra`，reasoning `high`，8192 token。
- 候选人转写：`OPENAI_REALTIME_TRANSCRIPTION_MODEL=gpt-live-transcribe`，保留 Realtime transcription 连接，使用原生 `server_vad`、`delay:low`；可用 `OPENAI_REALTIME_TRANSCRIPTION_LANGUAGES` 配置逗号分隔的语言提示。
- 应用只有 Live、候选人转写两个长期上游。Astra 连接由 Live 托管，不增加应用侧第三条 WebSocket。
- Live 自行接话和委派；不恢复主会话 VAD/commit/response.cancel 调度、旧 fast/deep coach 或其他 ASR provider。
- 产品仅显示文字字幕，必须丢弃 Live 输出音频；这不是声称 Live 使用 text-only 上游配置。

## 工具边界

托管 Astra 只暴露三个工具：`search_context`、`capture_current_screen`、`update_code`。

`search_context` 无参数，返回完整资料和当前单文件文档/输入版本；截图只送后台。`update_code` 在任务有效、上下文和文档版本匹配时自动打开代码区并提交完整短文件，保留差异与撤销；代码不进入解释正文。主 Live 的文字解释可与后台并行，不等待 HTTP 分析再续答。

手动 `code_action.generate` 是可选待采用建议，仍可发起一次 `store:false`、`truncation:disabled` 的 Astra Responses HTTP 请求；自动工具和“深入”按钮不能走这条路径。无代码执行能力，不声称测试过代码，不故意植入 bug 或伪造调试经历。

## 上下文边界

- 默认上下文目录：`apps/server/context`
- 可用 `INTERVIEW_CONTEXT_DIR` 覆盖。
- 只读取预先放置的 `.md` / `.txt`，完整保留各文件正文、段落与资料之间的关系，不切成简历碎片，不按关键词丢弃内容。
- 每场 interview 使用固定的完整资料快照；修改资料后开始新的 interview。
- Live instructions 保持简短，详细操作规则放托管后台。每次主连接启动向后台注入独立的完整资料消息，不把正文塞入 instructions。
- 后台启动资料和 search_context 返回完整原文；手动建议 HTTP 请求携带完整资料与记录，不用关键词检索替代。
- `search_context` 无参数（`properties: {}`、`required: []`），返回 `{ok: true, documents: [{source, text}], workspace: {...}, transcripts: [...]}`。资料不存在或无法完整传递时明确说明，不编造个人事实。
- 用户已接受 Live 自动压缩旧对话；不能宣称上游始终保留整场原文。应用记录仍完整保留，当前代码以应用文档为准。托管配置不支持 `truncation`/`store`，不得编造字段；Live session 设置 `store:false`。
- 不提供运行时文件上传。
- 不引入 vector database、embedding pipeline、OpenAI Files/vector store 或重 RAG。
- 私有生产资料应放在部署目录外，并通过只读 `INTERVIEW_CONTEXT_DIR` 挂载；不要提交到 Git。
- 完整背景重新注入不等于恢复此前实时对话、答案、音频或在途任务；不要据此宣称全部重连和实时问题已解决。

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
- Session/auth orchestration：`apps/server/app/services/openai_realtime.py`
- Live transport/hosted tools：`apps/server/app/services/live_session.py`
- Application tool definitions/handlers：`apps/server/app/services/interview_tools.py`
- Candidate native-event relay：`apps/server/app/services/candidate_transcript.py`
- Ordered observed history：`apps/server/app/services/realtime_history.py`
- UI operation semantics：`apps/server/app/services/realtime_controls.py`
- Context background/prompt：`apps/server/app/services/realtime_context.py`
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

桌面端默认连接 `https://interview.siyidu.com`。本地开发时显式设置：

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

以下检查按改动范围选择。真实双路音频验证会把音频发送给 OpenAI；只有本次会话已明确授权该测试时才点击 Start。普通构建或源码检查不需要启动采集会话。

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

新增或改变 Live 行为优先补 `tests/test_live_session.py`；候选人 ASR 与公共会话合同保留原测试。源码测试不等于 Electron 媒体权限、视觉和真实 OpenAI API 验证。

完整回归可用 `python -m unittest discover -s tests`；前端增加 `npm.cmd run test:ui` 和 `npm.cmd run test:capture`。

## 运行时恢复约定

- PCM 转换使用独立 AudioWorklet；两路仍独立。客户端积压预算约半秒，服务端每路只有一个在途音频发送任务，握手/背压不能堵住采集控制循环。过期/跳过音频明确提示，不伪造完整音频恢复。
- 主模型和转写各持有自己的连接锁。转写等待原生 `session.updated` 确认；网络发送、关闭、HTTP 操作有超时，反复短连接按指数退避。客户端轻量 ping/pong 识别半断连接；断线或终止媒体错误释放对应上游，保留本场记录。
- 托管任务整条工具循环最多等待 120 秒，超时释放应用忙状态并拒绝迟到写入，不声称停止供应商计费。手动 HTTP 代码建议受总超时约束。
- 输入变化只禁止旧版本写入，尚有效的原工具循环可重新 `search_context` 读取完整转写后重新考虑；取消、换题、手动改代码、换文档不能通过重读复活。暂停中的显式请求权限不能传给无关自动委派。
- 认证快照包含仅有数量/字符数的 `context_status`，空资料明确显示。不得将背景正文或私有文件名放入未认证 health。

- 代码区是本场面试内的单文件文档。主流程由托管 Astra 调用 `update_code` 自动打开并提交代码修改，回复区保留文字解释。版本与上下文检查通过后直接提交，保留改动对比和撤销；不增加主模型工具或常驻上游。`code_action` 为手动补充控制，折叠入口的 `generate` 仍返回需采用的建议。
- 文档提交检查 `document_id + base_revision`；采用建议还检查 proposal ID。候选人/面试官新增转写或截图只将手动代码建议标为待核对，用户必须确认已看到的上下文版本；不得覆盖后续手动编辑。撤销按实际提交顺序执行，版本号持续递增。
- 手动代码分析和 `collect_only:true` 的截图采集不随普通语音变化取消；停止代码任务、换代码题或结束面试时取消对应任务。普通主会话工具仍遵循下面的版本与取消约定。
- 自动代码工具遵循主会话的取消约定，并检查生成前后的文档和完整输入版本；输入变化、手动保存、撤销或换题后不得提交旧结果。自动展开使用一次性标识，同一次运行的状态同步不能反复打开用户刚收起的代码区。前端未保存草稿始终保留。
- 代码、建议和连续截图集合通过同一个 client WebSocket 同步与断线恢复，仍为当前内存态会话；没有新增服务重启恢复能力。

- 主事件 reader 不等待工具。通过 delegation、response 和 client event ID 关联后台任务；身份不明或版本过期不得提交代码。
- Live 连续字幕没有逐回答 done。应用短暂停顿只用于分段持久显示，不触发回答；后台完成与字幕段完成独立。暂停会抑制显示、拒绝工具写入并给 Live 停止提示，不声称已取消托管计费。
- 候选人分段使用原生 item_id、VAD 和 completed；每个 delta 立即入队转发，不用静默 timer、定时合并或规则分句。FIFO 仅隔离网络背压，不能取消已拥有文本的提交 worker。每次部分转写就更新输入版本，拒绝旧代码结果；最终识别修正归属同一语音段，断线保留部分文字并标明未完成。原生开始事件固定历史顺序，乱序 final 不得覆盖其他段；UI 和重连快照保留同一 turn_id/status。
- 主上游断开先终结旧字幕段并关闭旧连接，再把完整资料和记录提供给后台，Live 接收当前问题与恢复提示。不能把未转写音频或服务重启后的历史说成已恢复。
- `question_id` 是输入定位锚点，程序不另造规则化题型分类器或删减摘要。截图、纠正、分析和回答保留来源/关联，模型建议不等于候选人已说过。
- UI 使用 `operation_status` / 快照确认操作结果；`device_status.channel_details` 保留静音、断轨和错误，恢复单路时保留健康通道。截图图片走独立 capture-token HTTP 上传。
- 部署通过认证的原子 gate 拒绝活跃面试，保留回滚，再核对本地与公网的协议、模型及 release ID。无 gate 的旧版本首次升级走明确维护窗口，不自动绕过保护。

## 部署约定

`realtime-interview-v5` 是 breaking protocol。UI 和采集端必须校验 `session_ready.realtime_protocol`，拒绝旧协议；不能只看 HTTP 200。部署顺序必须是后端先上线并通过 `/health` 协议检查，再发布/启动默认连接远程服务的桌面端。

- Workflow：`.github/workflows/deploy-server.yml`
- VPS SSH 用户：`ubuntu`
- Compose 路径：`/home/ubuntu/siyi`
- 部署路径：`/opt/interview/server`
- Compose service/container：`interview_api`
- 端口：`8000`
- 公网域名：`https://interview.siyidu.com`

生产 `.env` 只放在 `/opt/interview/server/.env`，不要提交。共享 secrets 使用 `SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_KEY`、`COMPOSE_PATH`；项目专属值优先使用 `INTERVIEW_DEPLOY_PATH`、`INTERVIEW_PUBLIC_HEALTH_URL`。

服务器已有端口不要复用：

- `8080` = sub2api
- `8787` = connection
- `20241` = cloudflared metrics
- `40000` = WARP

## 媒体权限

不要用 Codex in-app browser 或普通浏览器验证双路采集；它们可能返回 `Permission denied`。使用 Electron 桌面端验证 `media`、`display-capture`、`microphone` 和 system audio loopback。

## 编辑原则

- 产品扩展沿现有边界接入：工具 schema 与 handler 放 `interview_tools.py`，结果状态属于本场 runtime/`CodeWorkspace`，展示放独立 `CodePanel`；复用现有 client 操作和快照，不把新业务写进 Live 音频/事件循环。未来代码讲解、多方案、逐步优化按此扩展，不预建插件平台或解题状态机。异步 handler 等待后、写入前重新检查任务有效性，文档提交继续检查版本；见 `docs/architecture.md` 的 Product extension seams。
- 优先删除兼容层和重复状态，不新增同功能第二条链路。
- 不修改或删除用户未提交的无关文件。
- Windows 文件操作使用 PowerShell 原生命令。
- 删除、迁移或重命名前先核对绝对目标、引用和差异。
- 不把 OpenAI API key、访问 token、简历/JD 或私有项目资料写入前端、日志或 Git。
