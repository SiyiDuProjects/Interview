# 面试产品全链路可靠性审计

日期：2026-09-21。范围：当前工作区的 Electron、React、FastAPI、Live 与候选人转写适配、托管工具、代码文档、截图、资料、认证、恢复、部署脚本，以及可读取的公网和部署记录。

**结论：发现并修复了多处会造成漏听、假在线、任务卡死、旧代码提交和发布不兼容的本地缺陷；尚未完成真实面试验收。最紧迫的发布阻断是：生产仍运行 v4 Realtime/Whisper 链路，本地已经是 v5 Live 链路。**

本轮没有部署、重启生产、启动真实采集或调用付费模型。保留原有未提交改动。以下区分源码/模拟证据、离线 Electron 证据和生产只读证据，不能把测试通过当成供应商或真实设备验收。

## 1. 最重要的发现

| 优先级 | 发现 | 面试影响 | 当前状态 |
| --- | --- | --- | --- |
| 发布阻断 | 线上为 v4、gpt-realtime-2.1、gpt-realtime-whisper | 新界面接旧服务产生协议/功能差异；本地修复尚未保护线上面试 | 本地 v5 握手拒绝旧协议；生产未发布 |
| 高 | 实际账号的 Live、托管 Astra、原生转写未做真实请求 | 配额、授权、实际事件或供应商异常可能导致有界面却无答案 | 对照官方协议和模拟事件检查；仍需真实供应商验收 |
| 高 | 实际系统音频、麦克风、蓝牙、会议软件未验收 | 设备切换、权限、回声、输出设备变化可能让某一路漏听 | 合成 Electron Worklet 已验证，不等于真实双路采集 |
| 高 | 服务为单进程内存状态 | 重启丢会话；错误运行多个 worker/副本会分裂状态 | 本地有部署门禁和恢复测试；生产进程配置未读取 |
| 高 | 生产资料挂载/完整性未确认 | 简历/JD 缺失或陈旧，回答与本人经历不符 | 新增认证后资料数量提示；本地 1 份/5775 字符不证明生产内容完整 |
| 中 | 长会话与大截图只做了合成恢复测试 | 上下文、请求体、内存、恢复耗时、限流仍可能出问题 | 200 轮/10 张截图/10 次代码版本通过，未做数小时真实负载 |

优先级表示应先闭环的风险，不代表未经验证的情况已经在生产发生。

## 2. 本轮本地修复

“证据”指故障测试或源码合同检查；供应商均为模拟对象。

| 编号 | 触发条件与问题 | 修复后的行为 | 主要位置/证据 |
| --- | --- | --- | --- |
| F01 | 生成代码期间收到新转写，整个任务失效，连重读上下文也被拒绝 | 原工具循环可重读完整转写并重新考虑；旧版本禁止直接提交；取消、换题、手动改文档不可复活 | live_session.py、interview_tools.py；上下文变化与取消测试 |
| F02 | 主 Live 和候选人转写共用连接锁 | 分开连接锁；一路握手不阻塞另一条健康音频 | openai_realtime.py；双向连接阻塞测试 |
| F03 | ASR 未接受配置就发送音频 | 等 session.updated；拒绝或 15 秒超时关闭该路，显示恢复状态 | 启动、超时、拒绝测试 |
| F04 | 短连接握手成功就清零失败次数 | 异常短连接保留计数，1–30 秒退避；稳定 30 秒才重新计数 | 连续立即断线测试 |
| F05 | 网络发送、关闭或 HTTP 操作没有整体边界 | 上游发送 5 秒、关闭有界；创建/结束/登录 HTTP 为 10 秒；手动代码 HTTP 有传输和整体期限 | 连接适配、Electron/App、手动建议超时测试 |
| F06 | UI 发送失败后只移除订阅，旧 socket 仍“开着” | 主动关闭失败连接，触发重连和快照恢复 | 慢 UI 故障测试 |
| F07 | 工具结果发不出去，模型继续等；代码可能已经提交 | 关闭故障主连接，恢复以应用文档为准；保留已提交代码 | 工具结果发送失败测试 |
| F08 | 已丢弃的输出音频仍累积每帧去重 ID | 记录去重 ID 前丢弃输出音频 | 2000 个输出音频事件测试 |
| F09 | 后台永不返回终态，界面/代码区一直忙 | 原任务及工具循环共用 120 秒期限；清忙、提示失败、拒绝迟到写入 | 托管超时、无事件手动请求、迟到代码测试 |
| F10 | 暂停中显式请求的权限被无关自动委派继承 | allow_held 只授予该显式任务；自动任务受暂停限制 | 暂停、显式截图、自动委派测试 |
| F11 | Electron 断开或音轨终止后上游仍存在 | 释放对应上游，保留会话/历史及健康通道；旧连接清理不关闭替代连接 | 断开、换轨、替代连接竞态测试 |
| F12 | 模型握手/慢发送堵住采集控制读取，积压旧语音 | 每路最多一个在途音频任务；控制读取继续；过期帧不补发；遗漏可见 | 慢发送、慢启动、结束期间握手测试 |
| F13 | 结束发生在握手期间，旧任务稍后又发布连接 | 先关闭准入，取消在途音频；握手后复查会话有效性 | 结束/握手竞态、lifecycle 测试 |
| F14 | 主线程 PCM 转换随 React/Markdown 渲染卡顿 | AudioWorklet 独立处理；首个 PCM 后才 ready；处理器错误不被 ready 覆盖；加载中停止不能复活 | PCM/停止/处理器失败测试、离线 Electron |
| F15 | 客户端可能排队约 5 秒语音，网络恢复后过时音频继续入模 | 待发预算收紧至约半秒；丢弃过时 Worklet 帧并提示缺口 | 采集背压、陈旧 PCM 测试 |
| F16 | WebSocket 半断，界面不更新却显示连接正常 | UI/两路采集共用轻量 ping/pong；未获回复关闭重连 | 半断、健康流量、空闲无模型 heartbeat 测试 |
| F17 | 只看连接成功，旧协议也显示就绪 | UI/采集强校验 v5；不兼容停止重试并提示后端先升级 | 首连/重连协议变化测试 |
| F18 | Start 没有 operation_status 却登记为普通模型操作 | Start 以 interview_state 确认，不再遗留永久“已发送” | App.tsx；完整模拟 Start、UI 回归、构建 |
| F19 | 空资料库缺乏直观信号 | 认证快照显示文档数/字符数和空资料提示；公开 health 无正文/私有文件名 | API 快照测试、界面构建 |
| F20 | 新部署脚本只认识 Live health，无法描述旧 Realtime 回滚目标 | 旧版本快照允许旧字段；候选新版本仍严格匹配 v5/模型/release ID | 旧版回滚快照测试 |
| F21 | HTML 入口和稳定 Worklet URL 没有明确缓存策略 | 200/304 均带 no-cache，发布后重新校验，减少继续加载旧入口风险 | 静态站点条件请求测试；[缓存规则](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Caching) |
| F22 | 私有简历被 Git 忽略但可能进入 Docker 构建 | .dockerignore 同样排除该已知私有文件；生产仍需外部只读挂载 | Docker/Git 排除检查；未检查实际生产镜像 |
| F23 | Python/Node 依赖命中已知安全公告 | 升级兼容的 FastAPI/Starlette、AnyIO、IDNA、pip、Joi；移除本地未声明且无依赖方的旧 multipart | 完整回归、pip check、最终依赖审计 |

F12/F15 防止过时语音继续排队，不是保证弱网不漏音。F09/F10 控制应用写入、显示和忙状态，不宣称取消供应商计算或计费。缓存修复也无法替浏览器删除此前已缓存的旧页面。

## 3. 审查与回归覆盖范围

| 链路 | 已覆盖 | 证据边界 |
| --- | --- | --- |
| 认证/会话 | 首帧认证先于模型；UI/capture token 隔离；HTTPS/WSS；current 会话；错误脱敏 | API/WS 模拟测试；公网未认证接口 401 |
| 双路音频 | 来源分开、空闲无上游、静音/断轨、单路替换、暂停仍收上下文 | 单元/ASGI；无真实回声和设备验证 |
| 候选人转写 | delta 立即转发；native item 顺序；乱序 final；最终修正/撤回；断线部分文字 | test_candidate_transcript.py；不是识别准确率评测 |
| Live/Astra | startup ACK；静默候选人上下文；字幕与慢工具并行；任务关联；重复事件；身份不明拒写 | test_live_session.py；未调真实 API |
| 代码 | 短文件、文档/输入版本、重复提交、手动编辑竞态、未保存草稿、撤销、换题、迟到结果 | 后端/UI；无执行与外部编辑器同步 |
| 截图 | 明确选源；不静默换屏；多页收集；HTTP 上传；先认证再读 body；大小/过期/错误/取消 | Electron 模拟/API；无真实截图识别验收 |
| 字幕/人工操作 | 答案追加；旧回答阅读位置；指定回答改写；纠正/暂停/恢复；操作终态 | UI/后端；显示分段不是供应商逐回答 done |
| 恢复 | UI 快照；主连接重放已记录历史；单路故障；旧连接事件；忙状态终结；200 轮恢复 | 有限合成场景；无未转写音频/重启内存恢复 |
| 部署 | start/drain 原子性；只改单服务；回滚；协议/模型/release ID；不打断已开始的新面试 | 部署 fixture；未执行生产发布 |

测试覆盖明确的输入和故障组合，不能推出所有并发时序、供应商行为与设备环境都正确。

## 4. 生产只读证据

2026-09-21 再次读取[生产健康接口](https://interview.siyidu.com/health)，HTTP 200：

```json
{
  "version": "0.5.0",
  "realtime_protocol": "realtime-interview-v4",
  "realtime_model": "gpt-realtime-2.1",
  "realtime_transcription_model": "gpt-realtime-whisper",
  "realtime_reasoning_effort": "low",
  "code_model": "gpt-6-astra",
  "release_id": "20260907-livefix-223955"
}
```

- 网页返回 200，引用旧资产 index-eDvHdQTm.js、index-D8ZkTE0B.css，HTML 未明确设置 Cache-Control。
- 未认证 GET /api/deployment、GET /api/interviews/current 均为 401。只能证明拒绝这些请求，不能证明当前有无面试或认证后的 gate 状态。
- 可见 deploy-server.yml 最近一次是[2026-08-30 的成功运行](https://github.com/SiyiDuProjects/Interview/actions/runs/33337392520)，commit 575b0870a95be8203588e7f3823f30bc63ced464。它早于 health 的 release 标识，不能判断后续发布途径，更不能证明当前工作区已上线。
- 没有可用的 SSH 主机配置供本轮继续核对。未读取生产容器依赖、实际 Compose 命令/副本、代理超时、内存限制、私有资料挂载、配额、日志或实际会话。
- 生产未修改。新版客户端需要先部署并验证新版后端，否则会拒绝旧协议。

## 5. 剩余风险与最小闭环

| 风险 | 剩余影响与边界 | 最小验证 |
| --- | --- | --- |
| 真实模型可用性 | 配置名不等于账号有权限；配额、限流、连接上限、实际工具流可能失败 | 经授权跑完整真实请求，记录脱敏事件、状态、延迟 |
| 开始时漏音窗口 | 模型握手期间的声音可能丢失；用户可能忽略 connecting 立即说话 | 真机测 Start 至两模型 ready 时间，确认就绪再开始 |
| 双路与回声 | 分路不等于消除声学回声；外放会串入麦克风，loopback 会包含通知/其他程序 | 实际会议软件、耳机/蓝牙、输出设备切换、同时发言 |
| 休眠/网络/手机后台 | 合盖、睡眠、切网、浏览器冻结会停顿；heartbeat 定时器也可能延迟 | 断网重连、唤醒、手机后台返回，核对缺口提示 |
| 长时与大图 | 原始记录/图片增长，恢复受内存、请求体、上下文和速率限制；上游会压缩旧对话 | 60–120 分钟与多张较大截图；不以静默裁剪资料代替正确恢复 |
| 听错但无错误 | 术语、口音、重叠语音可能得到可信但错误的文字，模型随后答错题 | 用真实题验证“听错/补充条件”能快速纠正 |
| 连续字幕边界 | 1.2 秒静默只是显示分段；后台终态不等于解释结束；暂停中显式回答依赖 Live 遵循之后保持暂停的提示 | 暂停→显式回答→保持暂停、打断/追问/换题；不声称精准停计费 |
| 无关联的模型错误 | Live 通用 error 可见，但不一定能判断必须重连 | 用真实错误类型验证，避免猜协议增加多套调度 |
| 资料完整性 | 本地数量只证明文件可读，不证明内容最新或生产已挂载 | 认证后检查数量、部署只读目录；修改后新开 interview |
| 代码正确性 | 工具成功仅表示内部文档写入；代码未执行；外部编辑器由用户控制 | 短题、改约束、手动改一行、撤销、连续追问验收 |
| UI 极端负载 | 长答案/列表、慢手机可能卡；JS 约 712 kB、gzip 221 kB，有大块警告 | 真机首屏与长会话渲染；已移走 PCM，不为警告重构平台 |
| 慢网快照 | 快照总时限 5 秒、单次 UI 发送 2 秒；极慢网络/极大历史可能反复恢复失败 | 大历史加慢网验证 |
| 重启/多进程 | 内存状态不可在重启后恢复，多个 worker 不共享 runtime | 生产单 worker/单副本；避免面试中重启，保留 gate |
| 采集退出后仍 active | 已释放对应上游，但保留会话恢复能力，会阻止部署直到明确结束 | 回 UI 结束；不自动删除面试来清门禁 |
| 浏览器登录过期 | Cookie 1 小时；已有 WS 可继续，过期刷新可能需登录 | 长面试手机重开；active runtime 不因普通 TTL 到点而中断 |
| 安全/供应链 | 本地公告零命中不覆盖生产 OS/镜像、完整 Electron 二进制和未来漏洞；CI 主机信任未生产核对 | 发布前核对镜像/主机，不称“无漏洞” |

没有新增第三条长期上游、备用回答链路、RAG、数据库、通用 agent 平台或解题状态机。修复集中在采集、传递、任务有效性和应用状态。

## 6. 验证记录

| 检查 | 结果 | 能证明什么 |
| --- | --- | --- |
| 后端 unittest discover | **158 项通过** | 当前本地故障场景与 API/WS 合同 |
| test:ui | **22 项通过** | 草稿、答案历史、操作状态、安全 Markdown 等 |
| test:capture | **33 项通过** | 模拟采集/WS、Electron 权限/截图等适配 |
| TypeScript + Vite build | 通过，有大 chunk 提示 | 构建成功，不是交互验收 |
| pip check | 无依赖冲突 | 当前 Python 环境依赖一致 |
| Python 依赖公告审计 | 最终未发现已知漏洞 | 本地 venv；artifacts/backend-dependency-audit-final.json |
| npm 依赖公告审计 | 最终 0 | 当前依赖树；不是生产系统扫描 |
| 真实 Electron 离线 Worklet | 24 kHz、1024 样本、5 帧、非零 PCM、secure context | 打包后 file URL 下 Chromium 处理；仅合成振荡器，未读任何真实音源 |
| 200 轮合成恢复 | 200 轮问答、10 张截图、10 次代码版本，顺序/恢复断言通过 | 有限规模状态；不是两小时真实语音 |
| 完整 ASGI 模拟链路 | 两 capture WS→不同模拟供应商→字幕/候选人转写→浏览器重连通过 | 路由、认证、PCM 来源、快照能串起来；非外网/供应商验收 |
| 生产只读 | health/网页 200；受保护接口 401；旧协议/模型确认 | 当前公网可观察状态 |

后端有 Starlette 对旧 HTTPX TestClient 和 413 常量名称的弃用提示，当前测试通过；后续升级应处理。依赖审计工具在独立测试环境，未加入生产 requirements。

复验命令（对应目录运行，不启动真实采集）：

```powershell
# apps/server
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check

# apps/desktop
npm.cmd run test:ui
npm.cmd run test:capture
npm.cmd run build
# 仅离线合成信号；隐藏窗口，20 秒超时：
.\node_modules\electron\dist\electron.exe tests/worklet-smoke.cjs
```

依赖修复依据：[Starlette Range DoS](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8)、[Windows UNC](https://github.com/Kludex/starlette/security/advisories/GHSA-wqp7-x3pw-xc5r)、[AnyIO](https://github.com/agronholm/anyio/security/advisories/GHSA-82r6-8w77-94w6)、[IDNA](https://github.com/kjd/idna/security/advisories/GHSA-65pc-fj4g-8rjx)。公告命中不等于证明本产品每条漏洞路径可利用。

协议核对：[Live delegation](https://developers.openai.com/api/docs/guides/live-delegation)、[Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)。音频依据：[AudioWorklet](https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet)、[processor process](https://developer.mozilla.org/en-US/docs/Web/API/AudioWorkletProcessor/process)。

## 7. 上线前最小验收顺序

1. 确认无活跃面试后，经现有 gate 发布后端；核对本地/公网 release ID、v5、Live/ASR/Astra，以及单进程、私有资料只读挂载。旧服务若无 gate，仍需明确维护窗口。
2. 连接新版 Electron 和手机 UI，检查资料数量、两路采集与模型状态；不把 HTTP 200 当作唯一就绪依据。
3. 经授权做真实彩排：简历追问→长题连续截图→短代码→改约束→手动编辑/撤销→暂停/显式回答→单路断轨/断网恢复。
4. 记录漏听、首段文字延迟、代码可用性、恢复耗时、资源占用和实际费用。通过之前，准确状态是“本地审计和可靠性修复完成，生产与真机验收未完成”。

本报告尽量枚举已发现的问题与重要未知项，不保证所有潜在问题已经穷尽。
