# Berkeley Bot / Berkeley Knowledge Engine - Project Memory

## 1. 项目一句话

这是一个面向 Berkeley 课程社区的知识引擎系统：从 Discord 频道采集消息，把聊天内容沉淀成可检索、可审核的课程/教授知识，再通过 API、Discord Bot 和 Web UI 提供问答能力。

如果要再压缩成一句：

> 它是一个“以后端为中心、以审核和可追溯为核心”的课程知识系统，而不是一个直接让 LLM 随便读群聊回答问题的聊天机器人。

---

## 2. 这个项目想解决什么问题

核心问题有 3 个：

1. Discord 课程群里的信息很碎，容易被刷掉，后来的同学很难复用。
2. 课程经验、FAQ、资源链接都混在聊天流里，没有结构化沉淀。
3. 直接拿原始群聊给模型回答，风险很高，容易幻觉、泄露隐私、或者把瞬时噪音当事实。

所以这个项目的思路不是“直接对聊天记录做 RAG”，而是：

1. 先采集原始消息。
2. 再抽取成 proposal。
3. 再经过人工审核沉淀为 card。
4. 再面向问答做 scoped retrieval。

另外，项目还想做一层 filesystem-first memory，把长期记忆落到文件系统，数据库只是派生索引，而不是唯一事实源。

---

## 3. 整体架构

仓库分成 4 个核心部分：

### `engine/`

系统核心。职责包括：

- FastAPI API
- 数据库存储
- 消息 ingest
- proposal 抽取
- admin 审核
- `/api/ask`
- OpenAPI 契约导出

它是整个系统的 single source of truth。

### `discord_bot/`

Discord 客户端，只通过 HTTP 调 engine，不自己做知识处理。

它除了问答，还承担：

- 用户注册
- Berkeley 邮箱/SID 绑定
- 学生角色管理
- 课程私有线程 enrollment/drop
- 频道消息自动 ingest

所以它不只是“问答机器人”，还是 Discord 社区运营入口。

### `web/`

纯静态前端，没有 React/Vue，只有 HTML/CSS/JS。

提供两个页面：

- `/ask`
- `/admin`

它不持有业务状态，只调用 engine API。

### `memory/`

文件系统优先的记忆层：

- 原始消息 append 到 daily markdown
- 达到 chunk 条件后切 chunk
- chunk 写回磁盘
- SQLite 建可重建索引
- 支持 BM25 + 向量混合检索
- 支持 flush 成 durable facts

---

## 4. 最重要的设计原则

### 4.1 后端优先

Discord 和 Web 都是 thin client，业务规则尽量放在 engine。

### 4.2 审核优先

不是所有从群聊提炼出的内容都会直接服务用户。项目有 proposal -> approve -> card 这条审核链。

### 4.3 scope 强约束

问答不是全局乱搜，而是尽量限定在：

- course
- professor
- global

这能减少错误召回和跨课程污染。

### 4.4 文件系统优先记忆

memory 的目标不是“数据库里存一份 embedding 就完了”，而是：

- markdown 文件是 durable source
- SQLite 只是可重建索引
- 即使索引丢了，也能从 chunk 文件重建

### 4.5 可追溯

证据、来源 chunk、原始 message 都尽量能追到。

---

## 5. 端到端主流程

### 流程 A: 消息采集

1. Discord 用户发消息。
2. `discord_bot` 在 `on_message` 里异步调用 engine 的 `/api/ingest/message`。
3. engine 先做 client credential 校验。
4. engine 检查 channel 是否启用，必要时根据 catalog 信息自动补 channel map。
5. 消息写入 engine 的 `messages` 表。
6. 同时尝试写入 `memory` 子系统。

### 流程 B: proposal 抽取

1. 管理员触发 `/api/admin/jobs/extract`，或 worker 消费 job。
2. `ExtractionService` 遍历 enabled channels。
3. 从最近时间窗口读取消息。
4. `extractor.py` 用规则生成 proposals。
5. proposals 写入 `proposals` 表，等待审核。

### 流程 C: 审核

1. 管理员查看 proposals。
2. approve 后 proposal 变成 approved card。
3. reject/merge 也有对应操作。

### 流程 D: 问答

1. 用户从 web 或 Discord 发起 ask。
2. engine 解析 mode 和 scope。
3. 若 scope 模糊，返回 disambiguation candidates。
4. scope 确定后，调用 `memory.search` 做 scoped retrieval。
5. 返回匹配到的 chunks、preview 和 context token。

注意：当前代码里这条 ask 主链路主要返回 memory chunks，不是完整的“基于 approved cards 生成自然语言答案”。

---

## 6. 当前代码里的真实实现状态

这一节非常重要，面试时必须按“当前代码事实”回答，而不是按设计文档想象回答。

### 已经实现的

- engine / discord_bot / web / memory 四层拆分
- 数据库抽象支持 SQLite / Postgres / MySQL
- migration 按方言目录自动执行
- ingest 鉴权
- channel map
- 原始消息入库
- memory daily 文件写入
- chunk 生成
- FTS5 文本检索
- 本地 deterministic embedding 检索
- 混合排序
- proposals / cards / admin API
- scope 解析和 disambiguation
- Discord 侧问答与频道自动 ingest
- Web 侧静态问答页和 admin 页

### 设计上有，但主链路没完全落地的

- ask 返回“approved cards + evidence-backed final answer”的完整闭环
- engine 内真正使用 `LLM_ENABLED` 控制问答行为
- context token 的完整服务端校验

### 当前实现要如实说的事实

1. `/api/ask` 现在主要是 memory chunk retrieval，不是真正的 card-based answer synthesis。
2. `cards`、`evidence`、`experience_strength` 这些响应字段已经预留，但默认 ask 路径里基本没有被填满。
3. embedding 不是外部语义模型，而是本地 hash-token embedding，优点是便宜、可离线、确定性强，缺点是语义能力有限。
4. extraction 目前是 rule-based，不是 LLM 抽取。

---

## 7. 数据模型

### engine 主数据库

关键表包括：

- `messages`: 原始消息
- `proposals`: 待审核知识提案
- `cards`: 审核通过后的知识卡片
- `channel_map`: Discord channel 到 scope/offering 的映射
- `courses`
- `course_offerings`
- `professors`
- `clients`: ingest client 凭证
- `jobs`: 后台任务
- `audit_logs`

### memory 派生索引库

每个 guild 一个 SQLite：

- `offerings`
- `professors`
- `channel_map`
- `chunks`
- `chunks_fts`
- `embeddings`
- `memory_entries`
- `pending_messages`

这个库的定位是“服务检索和 flush 的局部索引”，不是唯一真相源。

---

## 8. 文件系统记忆布局

memory 的 canonical layout 是：

```text
memory/
  workspace/
    guild_<GUILD_ID>/
      courses/
        <COURSE_CODE>/
          <TERM>/
            MEMORY.md
            daily/
              YYYY-MM-DD.md
            chunks/
              YYYY-MM-DD/
                chunk_<start>_<end>.md
      professors/
        <PROFESSOR_SLUG>/
          MEMORY.md
          daily/
          chunks/
      global/
        MEMORY.md
        daily/
```

语义上：

- `daily/*.md` 是 append-only 的流水日志
- `chunks/*` 是切分后的检索单元
- `MEMORY.md` 是更高层、可累积的 curated memory

这套设计的价值是：

- 人能读
- 能 diff
- 能手工修复
- 能从文件重建索引

---

## 9. ingest 细节

`IngestService` 的逻辑是：

1. 从 payload 里抽 catalog 信息。
2. 如有 professor_name，先 upsert professor。
3. 查 `channel_map`。
4. 如果没 map，尝试根据 course_code + term 自动解析 offering。
5. 如果开启 `ENGINE_ALLOW_UNMAPPED_CHANNELS` 且 guild 可信，还可以自动创建 offering/channel_map。
6. 若 channel 未启用，拒绝写入。
7. 写 `messages` 表。
8. best-effort 调 `memory.ingest_message`，即使 memory 失败也不影响主 ingest 成功。

这里体现了一个设计取舍：

> ingest 可靠性高于 memory 完整性，memory 失败不能拖垮主消息采集链路。

---

## 10. extraction 细节

当前抽取完全是规则驱动，在 `engine/berkeley_engine/extractor.py`。

### 规则类型

#### Resource

如果消息里带 URL，就生成 resource proposal。

#### FAQ

如果同一个归一化问题在窗口内出现至少 3 次，就生成 FAQ proposal。

#### Experience

如果消息包含 workload / exam / grading / project 等经验关键词，就按 topic 聚合。
当 topic 至少有足够作者或足够证据时，生成 experience proposal。

#### Knowledge

如果消息里出现 explanation hints，比如：

- because
- means
- so that
- in order to

就把它视为“可能是解释型知识”，生成 knowledge proposal。

### 这个抽取器的特点

优点：

- 成本低
- 可解释
- 不依赖外部模型
- 本地开发稳定

缺点：

- recall 和 precision 都比较有限
- 语义泛化弱
- 容易产生标题重复或信息粒度粗糙的问题

---

## 11. ask / retrieval 细节

### 11.1 mode 判定

若调用方显式传 `knowledge` 或 `experience`，直接用。
否则根据 query 是否包含 experience keywords 自动推断。

### 11.2 scope 解析优先级

1. `offering_id`
2. `course_code + term (+ section)`
3. `professor_id`
4. `professor_name`
5. `type=auto` 时从 query 文本里猜课程号或教授名

如果匹配到多个候选，就返回 disambiguation，让客户端二次确认，而不是后端擅自猜。

### 11.3 检索逻辑

调用 `memory.search`：

- BM25 from SQLite FTS5
- 向量相似度 from 本地 deterministic embedding
- 最终分数 = `vector_weight * vectorScore + text_weight * textScore`

默认配置是：

- vector 0.7
- text 0.3

### 11.4 当前 ask 返回内容

主要返回：

- `resolved`
- `resolved_scope`
- `disambiguation`
- `answer`
- `chunks`

其中 `answer` 当前通常只是：

- `Found N relevant memory chunks.`
- 或 `No relevant memory found in this scope.`

也就是说，ask 目前更像“scoped retrieval API”，不是最终的自然语言问答代理。

---

## 12. embedding 方案

这个项目当前不是 OpenAI embedding，也不是 FAISS/pgvector。

它的 embedding 是本地 deterministic hash embedding：

- 对 token 做简单 hash
- 落到固定维度桶
- 再归一化

优势：

- 无外部依赖
- 便宜
- 可离线
- 重建稳定

劣势：

- 不具备强语义能力
- 本质更接近 token hashing / bag-of-words 近似，而不是真正语义向量

所以如果面试官问“你们是不是做了语义检索”，最准确的回答是：

> 做了向量检索接口和混合检索框架，但当前实现是 deterministic local embedding，主要为了低成本和开发稳定性；后续可以平滑替换成真实 embedding 模型。

---

## 13. flush 机制

设计目标上，`memory.flush` 是把 chunk 编译成 durable facts：

1. 读取 scope 下的 chunks
2. 调用 OpenAI Chat Completions
3. 要求模型只输出 JSON
4. 生成 `durable_facts` 和 `daily_summary`
5. 写入 `MEMORY.md`
6. 写入 `memory_entries`
7. 同步写 engine proposal，等待人工审核

这条链的思想非常好，因为它把：

- 原始聊天
- 结构化 durable fact
- 审核入库

串起来了。

但要注意当前代码里有一个实现问题：

> `memory/flush.py` 里把 `MemoryDB` 变量覆盖成了 `Database`，后面继续调用 `list_chunks` 等 memory 方法会报错，所以 flush 按现状是有 bug 的，不能当成稳定能力宣称。

---

## 14. auth 和安全边界

### engine API

- admin 接口用 `X-ADMIN-TOKEN`
- ingest 接口优先用 `X-CLIENT-ID + X-CLIENT-TOKEN`
- 兼容 legacy `X-BOT-TOKEN`

### client 模型

engine 有 `clients` 表，可限制：

- `allowed_platforms`
- `allowed_workspaces`
- revoke / rotate token

### 数据脱敏

系统会对 excerpt 做基础脱敏：

- Discord mention
- `@handle`
- 长数字 ID

### context token

ask 会给 chunk 签发 `context_token`，web 也会把 token 带到 `/api/messages/context`。

但当前服务端这个 endpoint 实际没有接收并校验 `context_token` 参数，所以这是一个“设计已开始、闭环未完成”的点。

---

## 15. Discord Bot 不只是问答

如果被问“这个 bot 除了问答还做什么”，要答全：

- 自动 ingest Discord 消息
- 支持 mention ask 和 slash `/ask`
- 处理 scope disambiguation
- 学生注册与 Berkeley 身份校验
- 课程 enrollment / drop
- 管理私有课程 thread
- 维护 term 状态

也就是说，这个仓库其实同时覆盖了：

- knowledge pipeline
- Discord 社区管理
- course access workflow

---

## 16. Web 前端特点

web 是纯静态页面，定位是 demo/admin client，而不是复杂 SPA。

特点：

- 零框架依赖
- 运行成本低
- 可以直接用 `python -m http.server`
- 通过 runtime config 指向 engine

`/ask` 页面做了两件比较实用的事：

1. 课程 / 教授 scope 辅助选择
2. 展示 memory hit preview，并支持拉取上下文窗口

---

## 17. 部署和运行方式

本地开发默认：

- engine: SQLite
- memory: filesystem
- web: static server
- bot: 可接 DB，也可 JSON fallback

生产建议：

- engine 用 Postgres/MySQL
- worker 单独进程跑 jobs
- 如果没有持久卷，禁用 memory

这说明项目对部署环境的假设是分层的：

- engine 可以云化
- memory 更依赖持久磁盘
- web 最轻
- bot 可以独立部署

---

## 18. 这个项目的亮点

如果从面试角度总结亮点，我会说这几个：

### 亮点 1: 清晰的职责切分

engine、bot、web、memory 各自职责明确，避免把 Discord bot 做成“大泥球”。

### 亮点 2: scope-aware retrieval

课程、教授、global 三种作用域是这个项目区别于普通聊天机器人很关键的地方。

### 亮点 3: 审核链路

proposal -> approve -> card 让系统更适合高信任场景，不是纯生成式黑盒。

### 亮点 4: 文件系统优先记忆

这是比较有个性的设计，不只是向量库堆功能，而是考虑可恢复性、可读性、可维护性。

### 亮点 5: 成本受控

很多能力先用 rule-based / local embedding 跑通，降低对外部模型和预算的依赖。

---

## 19. 这个项目的局限和技术债

面试里最好主动承认这些点，反而显得你清楚系统边界。

1. ask 主链路和“approved cards”闭环还没完全打通，当前更偏 retrieval。
2. extraction 规则比较简单，知识质量上限有限。
3. embedding 不是强语义模型，召回质量有限。
4. `memory.flush` 当前有变量覆盖 bug，不能算稳定能力。
5. `context_token` 机制没有真正完成服务端校验。
6. `LLM_ENABLED` 配置目前基本只出现在日志，不是实际控制开关。
7. web/admin/ask 还是比较轻量的 demo 级客户端，不是成熟运营后台。

---

## 20. 如果让我继续演进，我会怎么做

优先级从高到低：

1. 打通 ask 主链路，把 approved cards 和 memory chunks 融合成真正 evidence-backed answer。
2. 修复 `memory.flush`，让“chunk -> durable fact -> proposal”真正可用。
3. 把 deterministic embedding 替换成真实 embedding，并保留本地 fallback。
4. 完成 context token 校验，收紧 message context 访问边界。
5. 把 extraction 从 rule-based 升级为“规则 + LLM 结构化抽取”双通道。
6. 增加 proposal/card 质量评估和重复检测。
7. 补更多端到端测试，尤其是 ingest -> extract -> approve -> ask 全链路。

---

## 21. 面试回答模板

### Q: 这个项目最核心的技术设计是什么？

A: 核心是把聊天机器人拆成知识引擎架构。Discord 和 Web 都只是客户端，真正的数据、审核、检索和问答入口都在 engine。这样可以把采集、提炼、审核、检索做成稳定后端能力，而不是绑死在某个聊天平台里。

### Q: 为什么要做 memory 文件系统，而不是直接数据库加向量库？

A: 我想保留 durable、可读、可重建的记忆层。markdown 文件是长期真相源，SQLite 只是可重建索引。这样更容易 debug、迁移、diff 和人工修复，也适合低成本部署。

### Q: 这个系统怎么避免胡说八道？

A: 设计上主要靠三层约束：scope 限制、proposal/card 审核链、证据可追溯。当前 ask 主链路主要还是检索 memory chunk，所以已经减少了无依据生成，但完整的 approved-card synthesis 还在演进中。

### Q: 现在 retrieval 用的是什么方案？

A: 混合检索。文本侧是 SQLite FTS5 BM25，向量侧是本地 deterministic embedding，最后按权重融合。当前重点是先把架构和可重建性打通，而不是追求最强语义效果。

### Q: 最难的设计取舍是什么？

A: 是“工程可靠性 vs 智能程度”。我优先让 ingest、scope、审核、可重建 memory 先稳定，再逐步把 LLM 引入到 flush 和更高质量抽取里，而不是一开始就把所有关键路径都交给模型。

---

## 22. Deep Dive 时要抓住的关键词

- backend-first
- single source of truth
- scoped retrieval
- auditable knowledge pipeline
- filesystem-first memory
- rebuildable derived index
- thin clients
- proposal approval workflow
- deterministic low-cost retrieval baseline
- gradual path to LLM enhancement

---

## 23. 最短版项目介绍

如果上下文很紧，只保留下面这段：

> 这是一个 Berkeley 课程社区知识引擎。Discord Bot 会把课程群消息 ingest 到后端，engine 负责存储、抽取 proposal、管理员审核成 cards，并提供 course/professor scoped ask API。系统还有一个 filesystem-first memory 层，会把消息按 guild/course/professor 写成 daily markdown 和 chunk 文件，再用 SQLite FTS5 + 本地 deterministic embedding 做混合检索。当前 ask 主链路主要返回 memory chunks，完整的 approved-card synthesis 还在演进中。这个项目的重点不是做一个随便回答的聊天机器人，而是做一个可审核、可追溯、可逐步演进的知识系统。

