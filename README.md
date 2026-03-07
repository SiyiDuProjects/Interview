# Interview Copilot

本项目是一个本地桌面端模拟面试助手。

- 左侧展示实时对话、手动输入或脚本模拟
- 右侧生成滚动答案卡
- 支持候选人麦克风和面试官系统音频双路采集
- 支持快答 + 详细回答流式补全
- 支持简历、JD、备注等上下文注入

当前形态是 `Electron + React` 桌面端配合 `FastAPI` 服务端。

## 仓库结构

```text
apps/
  desktop/   Electron + React 桌面端
  server/    FastAPI 服务端
docs/
  architecture.md
```

## 当前能力

1. 实时对话页
   左侧对话气泡，右侧答案卡。
2. 双路采集
   同时采集候选人麦克风和面试官系统音频。
3. 手动输入
   手动提交面试官问题，或只把你的回答写入上下文。
4. 脚本模拟
   用脚本片段回放整段面试流程。
5. 快答路径
   面试官问题进入后，优先返回可立即复述的短答案。
6. 详答路径
   同一张答案卡继续补全更稳、更长的详细版。
7. 上下文注入
   支持姓名、目标岗位、简历、岗位描述、补充备注。
8. 追问识别
   结合最近对话判断是否是 follow-up。

## 技术架构

- 桌面端：`React 18 + Vite + Electron`
- 服务端：`FastAPI`
- 实时中文转写：讯飞 RTASR
- 实时英文转写：Deepgram
- 回答生成：OpenAI
- 详细回答流：SSE

更详细的流程见 [architecture.md](/C:/Users/Administrator/Desktop/Projects/Interview/docs/architecture.md)。

## 功能和依赖关系

不同模式依赖不同配置，不是所有密钥都必须同时填写。

### 只想看 UI 或跑手动输入/脚本模拟

- 服务端可启动
- 推荐配置 `OPENAI_API_KEY`

没有 `OPENAI_API_KEY` 时：

- UI 仍然能打开
- 手动输入和脚本模拟仍能走流程
- 答案卡会显示 AI 不可用占位内容，不会生成真实答案

### 想用中文实时面试

需要：

- `OPENAI_API_KEY`
- `XFYUN_RTASR_APP_ID`
- `XFYUN_RTASR_ACCESS_KEY_ID`
- `XFYUN_RTASR_ACCESS_KEY_SECRET`

中文实时模式会走讯飞，不走 Deepgram。

### 想用英文实时面试

需要：

- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`

英文实时模式会走 Deepgram。

## 环境变量

项目根目录放 `.env` 文件。可以从 [.env.example](/C:/Users/Administrator/Desktop/Projects/Interview/.env.example) 复制。

### 回答生成

- `OPENAI_API_KEY`
  OpenAI Key。回答生成依赖它。
- `OPENAI_BASE_URL`
  OpenAI API Base URL。
- `OPENAI_FAST_MODEL`
  快答模型。
- `OPENAI_MODEL`
  详细回答模型。
- `OPENAI_TIMEOUT_SECONDS`
  回答生成超时。

### 英文实时转写

- `DEEPGRAM_API_KEY`
  英文实时转写所需。
- `DEEPGRAM_WS_URL`
  Deepgram WebSocket 地址。
- `DEEPGRAM_MODEL`
  默认 `nova-3`。
- `DEEPGRAM_LANGUAGE`
  非英文时的语言配置。
- `DEEPGRAM_LANGUAGE_EN`
  英文语言配置，默认 `en-US`。
- `DEEPGRAM_INTERIM_RESULTS`
  是否返回中间结果。
- `DEEPGRAM_ENDPOINTING_MS`
  endpointing 参数。
- `DEEPGRAM_PUNCTUATE`
  是否自动标点。
- `DEEPGRAM_SMART_FORMAT`
  是否智能格式化。

### 中文实时转写

- `XFYUN_RTASR_APP_ID`
  讯飞应用 ID。
- `XFYUN_RTASR_ACCESS_KEY_ID`
  讯飞 access key id。
- `XFYUN_RTASR_ACCESS_KEY_SECRET`
  讯飞 access key secret。
- `XFYUN_RTASR_WS_URL`
  讯飞 RTASR WebSocket 地址。
- `XFYUN_RTASR_LANG`
  中文实时语言配置。
- `XFYUN_RTASR_PUNC`
  是否自动标点。
- `XFYUN_RTASR_PD`
  领域配置。
- `XFYUN_RTASR_VAD_MDN`
  VAD 配置。
- `XFYUN_RTASR_ENG_LANG_TYPE`
  英文混说相关配置。

### 其他转写相关

以下变量主要给服务端 chunk 转写接口或兼容路径使用，不是桌面端实时主链路的核心配置：

- `OPENAI_TRANSCRIPTION_MODEL`
- `OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS`
- `TRANSCRIPTION_PROVIDER`
- `TRANSCRIPTION_LANGUAGE`

### 桌面端

- `VITE_API_BASE_URL`
  桌面端访问服务端的地址，默认 `http://127.0.0.1:8000`。

## 本地启动

### 1. 准备 `.env`

从 [.env.example](/C:/Users/Administrator/Desktop/Projects/Interview/.env.example) 复制一份：

```powershell
Copy-Item .env.example .env
```

按你的使用场景填写最少配置：

- 手动输入 / 脚本模拟：至少填 `OPENAI_API_KEY`
- 中文实时：再填 `XFYUN_*`
- 英文实时：再填 `DEEPGRAM_API_KEY`

### 2. 启动服务端

```powershell
cd apps/server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

### 3. 启动桌面端

```powershell
cd apps/desktop
npm install
npm run dev:desktop
```

生产式启动：

```powershell
cd apps/desktop
npm run build
npm run start:desktop
```

## 页面说明

### 实时对话

- 左侧显示对话气泡
- 右侧显示答案卡
- “开始”会同时启动双路采集
- 中文模式使用讯飞
- 英文模式使用 Deepgram

### 手动输入

- “手动输入面试官问题”
  会触发右侧答案卡生成
- “手动输入你的回答”
  只写入上下文，不直接生成答案卡

适合：

- 补上下文
- 快速测试问题回答
- 没配实时转写时先调回答链路

### 脚本模拟

- 复杂场景按钮
  直接灌入一段较长面试流程
- 脚本回放
  每行一条，使用 `interviewer|` 或 `candidate|` 前缀

适合：

- 验证追问识别
- 验证长对话
- 验证答案卡复用和补全

### 个人资料

可填写：

- 姓名
- 目标岗位
- 简历
- 岗位描述
- 补充备注

这些内容会直接影响回答风格和答案内容。

## 运行说明

### 系统音频采集

开始实时采集后，系统会弹出共享/录制选择。

如果要采集面试官系统音频，需要在系统选择器里明确勾选音频共享。否则会报“没有采集到系统音频”。

### 模式切换

- 正在实时采集时，不建议切换识别语言
- 正在脚本回放时，实时采集开关会被禁用

### 长时面试

当前实现没有写死 60 分钟上限，实时链路也带了长连保活。

但它还不是“长时场景完全加固版”，目前仍有这些限制：

- 前端会持续累积对话历史和答案卡
- 没有做长会话裁剪或分段归档
- 没有做断线自动重连

所以结论是：

- 连续跑 1 小时在实现上是可能的
- 但 README 不把它表述为“已稳定支持 1 小时生产级面试”

## 测试

服务端测试：

```powershell
cd apps/server
python -m unittest tests.test_interview_flow tests.test_latency
```

覆盖内容包括：

- hybrid 模式返回
- 详细回答后台任务完成
- 详细回答流式输出
- 无 OpenAI Key 时的占位行为
- 延迟测试

桌面端构建检查：

```powershell
cd apps/desktop
npm run build
```

## 常见问题

### 1. 中文实时模式一启动就报错

优先检查：

- `XFYUN_RTASR_APP_ID`
- `XFYUN_RTASR_ACCESS_KEY_ID`
- `XFYUN_RTASR_ACCESS_KEY_SECRET`

中文实时模式走讯飞，不会使用 Deepgram 替代。

### 2. 英文实时模式一启动就报错

优先检查：

- `DEEPGRAM_API_KEY`

### 3. 系统音频没有采到

通常不是代码问题，而是共享时没有勾选音频。

### 4. 有对话但右侧没有正常答案

优先检查：

- `OPENAI_API_KEY`
- 服务端是否已启动
- `VITE_API_BASE_URL` 是否指向正确地址

### 5. 手动输入能用，实时采集不行

说明回答链路大概率正常，问题多半在实时转写配置或系统采集权限。

## 已知限制

- 当前 UI 主要按桌面窗口设计，不是网页端响应式产品
- 中文实时和英文实时使用不同服务商
- 非实时页面虽然已支持面板内滚动，但还没有做更细的长表单交互优化
- 长会话还没有做自动归档、摘要压缩和断线重连
- README 描述以当前代码为准，不保证覆盖未来所有实验分支

## 相关文件

- [README.md](/C:/Users/Administrator/Desktop/Projects/Interview/README.md)
- [architecture.md](/C:/Users/Administrator/Desktop/Projects/Interview/docs/architecture.md)
- [.env.example](/C:/Users/Administrator/Desktop/Projects/Interview/.env.example)
- [desktop App.tsx](/C:/Users/Administrator/Desktop/Projects/Interview/apps/desktop/src/App.tsx)
- [desktop styles.css](/C:/Users/Administrator/Desktop/Projects/Interview/apps/desktop/src/styles.css)
- [server main.py](/C:/Users/Administrator/Desktop/Projects/Interview/apps/server/app/main.py)
- [server config.py](/C:/Users/Administrator/Desktop/Projects/Interview/apps/server/app/config.py)
