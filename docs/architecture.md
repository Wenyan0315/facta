# Personal Agent 架构图

> 版本：v0.48（2026-09-17）｜随着里程碑推进持续迭代此文档
> 更新规则：架构有变更（新增层/模块）时，同步更新本文件并提升版本号；架构决策（v0.37 起）写进 docs/decisions/ 并在本文件索引表加行
> 产品定位（v0.43 起）：见 [product.md](product.md)——通用个人 agent，场景优先级由真实使用数据排序

## 设计原则

1. **接口与实现分离** —— `LLM` 是接口，mock/DeepSeek/本地模型随便换，上层不动
2. **分层解耦** —— 每层只做自己那件事，任何一层都能单独替换
3. **从简到真** —— 每个模块先手写"教学版"跑通原理，再换"工业版"（词袋→真embedding，mock→DeepSeek）
4. **渐进演化** —— 阶段一"学习助手"的地基，正好是阶段二"coding agent"的积木
5. **活 spec（方向提前定，细节临期定）** —— 路线图只锁方向（一行一里程碑）；详细方案在开工时才写（最后责任时刻决策）；实现后必回写决策记录。spec 与代码同生命周期，不做预言式大设计——MCP 三次顺延、M6.3 因 M6.2 翻车而丰富，皆是实证
6. **Human on the Loop（阶段二起，业界调研吸收）** —— 执行成本归零后，人的注意力是唯一稀缺资源：产品让人「定义、掌舵、评判」（定义任务、设定边界、确认结果），agent 负责执行。状态跟踪、单点批准、交付摘要不是附加功能，而是产品核心

## 一、系统总体架构（目标全貌）

```
┌──────────────────────────────────────────────────────────────┐
│                        用户交互层                              │
│          ✅ CLI（cli.py 壳）｜ ✅ Web UI（S2b 对话视图）    │
│          以后：任务视图（S2 预留）/ IM 渠道（S8）              │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│       入口 __main__.py（CLI）/ server/__main__.py（Web）       │
│         装配唯一真值源：orchestrator/assemble.py（依赖注入）    │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│  ★ orchestrator/ 编排层：run_turn 主循环（大脑）               │
│    core/ 地基：types / llm / 网关 / 账本 / 向量数学             │
│                                                              │
│   ┌─────────────────────────────────────────────────┐        │
│   │  ✅ 感知 → 决策 ⇄ 行动(工具，检索亦工具) → 观察     │        │
│   │ （M5 ReAct 雏形；M5.5 Agentic RAG：检索权在模型）   │        │
│   └─────────────────────────────────────────────────┘        │
│      │              │                │               │       │
│      │         ┌────▼─────┐    ┌─────▼──────┐  ┌─────▼────┐  │
│      │         │ 运行时校验 │    │  LLM Router │  │ 阶段二:   │  │
│      │         │ guardrails│    │  (网关能力)  │  │ Orchestr-│  │
│      │         │ 输出/参数  │    │ 按任务路由模型│  │ ator 多  │  │
│      │         │ 校验+重试  │    │ 重试/限流/   │  │ agent编排 │  │
│      │         └──────────┘    │ 成本统计     │  └──────────┘  │
│      │                         └─────┬──────┘                 │
└──────┼───────────────────────────────┼────────────────────────┘
       │                               │
       ▼                               ▼
┌────────────────┐        ┌────────────────────────────────┐
│  knowledge/    │        │  LLM 接入层（接口 + 实现）        │
│  知识层         │        │  ┌──────────┬─────────┬──────┐  │
│ ┌────────────┐ │        │  │ ✅mock   │ ✅echo   │repeat │  │
│ │✅ RAG 检索  │ │        │  │ 假模型   │ 假模型   │ 假模型 │  │
│ │✅ BGE-M3   │ │        │  ├─────────┴─────────┴──────┤  │
│ │  语义向量   │ │        │  │ ✅OpenAICompatibleLLM     │  │
│ │✅loader    │ │        │  │  deepseek/siliconflow     │  │
│ │✅向量库+M7 │ │        │  │ 以后: Claude / 本地Ollama   │ │
│ ├────────────┤ │        │  └──────────────────────────┘  │
│ │ M8:知识图谱 │ │        └────────────────────────────────┘
│ └────────────┘ │
│   data/notes/  │ ← 数据与代码分离：笔记是 md 文件，加笔记=丢文件，零改码
└────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│  memory/  记忆层                                               │
│   短期: ✅ messages 列表（会话内）                              │
│   跨会话: ✅ JSON 持久化（M6.1，data/memory/session.json）      │
│   压缩: ✅ 滚动摘要+原文窗口（M6.2，发送前投影，底片不动）        │
│   演进: M6.3 分层记忆完善 + 多会话 + search_history              │
└──────────────────────────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│  tools/  工具层（M5 ✅ 全项目灵魂已落地）                        │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  ToolRegistry 工具注册表 ✅                           │     │
│  │  ├── ✅ 内置工具×6: 时间/清单/读/写/检索/检索+摘要    │     │
│  │  ├── MCP 客户端 → 外部工具动态发现（顺延待排期）        │     │
│  │  └── skills/: prompt 模板 + 资源包按需加载             │     │
│  └─────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────┘

══════════════ 独立于运行链路 ══════════════

┌──────────────────────────────────────────────────────────────┐
│  evals/  离线评估                                             │
│   ├── data/            测试集: 问题 + 标准答案/期望命中块        │
│   ├── 检索指标          precision@k / recall@k / MRR (M3.5)   │
│   └── LLM-as-judge     回答质量评分 (M4后)                     │
└──────────────────────────────────────────────────────────────┘
```

## 二、各层状态一览

| 层 | 现状 | 建成后 |
|---|------|--------|
| core 地基 | ✅ types / llm 接口+实现 / 网关四件套 / 账本 / 向量数学——最底层不反认上层（S2a 依赖方向拨正） | 更多供应商 + 多模型路由 |
| orchestrator 主循环 | ✅ ReAct 雏形 + M5.5 Agentic RAG：决策→执行→观察→再决策（5轮保险丝）；检索权已移交模型，主循环不再直连 kb；streaming 流式消费（分片边收边打→merge 拼回复；内部调用照旧非流）；S2a 内核/外设分离：run_turn 零 input/print，I/O 走 on_text/on_event/should_cancel/on_confirm 四条缝（S4b 加确认缝）；S2b 协作式取消两检查点 | 并行工具调用 / 更复杂的规划策略 |
| LLM 接入 | ✅ OpenAI兼容统一类+配置表(deepseek/siliconflow) + 进程内网关(M7.5：记账/重试超时/精确+语义缓存/熔断三态/降级链+优雅兜底) | 更多供应商 + 多模型路由 |
| knowledge | ✅ Embedder接口+词袋/BGE双实现 + loader(数据外置) + VectorStore接口+双实现(M7：InMemory教学版/Chroma工业版落盘) + 增量同步(内容指纹差集) | 知识图谱 |
| memory | ✅ 会话内记忆 + 跨会话 JSON 持久化（M6.1）+ 摘要压缩（M6.2）+ 温层检索 search_history/read_history（M6.3）+ Session 状态整体持久化（压缩缓存随底片落盘，重启不再重压）+ 记忆固化 data/learned（M6.4：萃取→审查→硬校验→落盘）+ 多会话管理（S1：active+archive，/new 归档重开）+ restore_session 原语（S2a：归档写回 active 后删除，move 语义） | 用户级记忆仓库外位置 |
| tools | ✅ Tool+ToolRegistry+内置工具按家族分件（time/history/notes 三族，S4a 拆分）；write_note 安全栅栏+查重闸门；search_notes=Agentic RAG 入口；search_and_summarize=复合工具(内部调LLM，Sub-agent原型)；联网工具 web_search/fetch_web（015：Tavily Provider+SSRF 栅栏+条件注册）；MCP 外部工具配置化接入；文件四件 read_file/search_code/list_dir/write_file+diff（S4a，workspace 围栏：__file__ 锚定+敏感黑名单读都不行）；终端执行 run_command（S4b：白名单只读免确认/L2 确认缝 registry 收口/批准拒绝都落审） | 更多工具 + skills |
| server Web 壳 | ✅ S2b：FastAPI+SSE（web 可选组）——Run 三接口分离（创建 202/事件订阅/取消）+ 内存 Run Store（状态机单一终态、事件 append-only 带 seq、单锁 create_if_idle 原子）+ Last-Event-ID 断线重放 + 会话列表/新开/切回 + 静态三件零构建链；只绑 127.0.0.1。验收修复轮：人设保证（ensure_persona 装配不变量）、每轮落盘、取消检查点③（流中即时）、任务视图最小版（/tasks+GET /api/runs）、历史回放（GET /api/messages）、md 渲染（marked vendored）、Enter/Esc 键盘。S4b：waiting_approval 挂起态（进单锁口径、取消视拒、confirm.request/resolved 事件断线重放重弹）+ confirm 裁决端点 + 前端确认弹窗 | 任务视图完整版（S2 预留）；Run Store 外置（多实例触发） |
| evals | ✅ 检索评估(P/R@k, MRR, 双实现对比) + LLM-as-judge 回答质量(基线 13/13 合格, 0% 错误, 全轮 ¥0.011) | 更难的对抗题库 + 回答质量回归 |
| data | ✅ data/notes/*.md 笔记库(与evals/线上共用同一语料)；agent 可自主写入(自我进化闭环已验证)；data/memory/session.json 对话记忆(M6.1，gitignore 运行时数据)；data/learned/*.md 项目级长时记忆(M6.4，进 git) | 长文档、多来源 |

## 三、两条演进主线

```
主线1：模型      mock ──M4──→ DeepSeek ──以后──→ 多模型路由
主线2：能力循环  只说话 ──M5──→ 会用工具(ReAct) ──M5.5──→ 自主决定查不查(Agentic RAG) ──阶段二──→ 多agent协作
```

## 四、里程碑路线图

| 里程碑 | 内容 | 学到什么 |
|--------|------|---------|
| M3 收尾 | 检索接进聊天循环，agent 基于笔记回答 | RAG 完整闭环 |
| M3.5 | 检索评估实战（precision@k 等） | 度量思维 |
| M4 | 接入 DeepSeek 真模型 | API调用、key管理、校验重试 |
| M5 | 工具调用 Function Calling ⭐ | agent 从"会说话"到"会做事" |
| M5.5 ✅ | Agentic RAG：检索包成工具(search_notes)，查不查/查什么/查几次由模型决定；MCP 顺延待排期 | 放权设计、复杂度塌缩(121→87行) |
| M6 ✅ | 记忆持久化：JSON落盘(M6.1)→摘要压缩(M6.2)→温层检索(M6.3)→Session整体持久化+回归测试 | 成本控制、状态完整性 |
| P1 | 重构前置：Message→types.py（依赖方向）+ ToolContext 收敛工具依赖 + 路径统一注入 | 依赖方向、对象收敛 |
| M7 ✅ | 工业 RAG：向量库持久化(Chroma) + 增量更新（弃全量重建）｜evals/文档治理并入收官 | 向量库、增量索引 |
| M7.5 ✅ | 生产加固：网关四件套（记账+重试超时+精确/语义缓存+熔断）｜降级链 + 优雅兜底 | 容错、可观测性、成本控制 |
| M6.4 ✅ | 记忆固化：对话→分类萃取→审查→硬校验→长时记忆沉淀（data/learned/*.md，退出复盘） | 分层记忆、萃取纪律、防幻觉污染 |
| MCP ✅ | 外部工具动态发现（三次顺延的集结号，coding agent 最直接积木）｜stdio+HTTP 双传输、配置化装配、真三方验收 | 协议、动态工具 |
| streaming ✅ | 流式输出：generate_stream 接口（默认伪流，老实现零改动）+ 真流实现 + 分片重组纯函数 + 网关四衣流式语义；用户取消复用中断通道 | 增量协议、生成器惰性 |
| 阶段二 | —— coding agent 产品化（2026-09-13 规划拍板，方向提前定、细节临期定）—— | 实战整合 |
| S1 ✅ | 多会话管理：/new 会话隔离（active+archive：位置固定无指针、文件名=身份/标题=标签、先存后清、原地清不 rebind） | 状态管理 |
| S2 ✅ | 产品骨架 v1 对话视图：S2a 清账（agent_loop 正名 orchestrator 编排层、run_turn 内核/外设分离、assemble 装配单一真值源）→ S2b Web 壳（FastAPI+SSE：Run 三接口分离、内存 Run Store 单锁状态机、断线重连重放、会话切回、零构建链前端三件）；任务视图按双视图预留未实现 | 前后端、SSE、Run 状态机 |
| S3 ✅ | 安全底座：工具权限分级（L0 只读/L1 写/L2 确认留 S4）+ 注入界碑（外部内容包裹声明+免疫条款）+ 审计日志（registry 收口 append-only jsonl 按天滚动，分级截断） | 安全 |
| S4 ✅ | 工具访问三件套：文件读写 / 代码定位 / 终端执行（coding agent 的腿）；工业版目标=LSP 反馈环（Shadow Workspace 思路）。**S4a ✅ 文件四件（read/search_code/list_dir/write_file+diff）+ builtin 拆分（time/history/notes 三族）+ workspace 围栏（__file__ 锚定+敏感黑名单）**；**S4b ✅ 终端执行 run_command + L2 确认机制（只读白名单免确认/registry 确认缝第四条缝/waiting_approval 挂起/Web 弹窗+CLI input/批准拒绝都落审）** | 文件系统、进程调度 |
| S5 | skill 系统：可复用技能包与装载（2026-09 需求点名）+ 规则文件（AGENTS.md 式，learned 的读取侧；含系统提示词外置——SYSTEM_PROMPT 迁出 loop.py，改行为不碰代码，2026-09-15 裁定并入本站） | 插件化 |
| S6 | 多 agent 协作：subagent 编排（主线2 的终点；判据=噪声隔离，见调研记录；worktree 隔离机制） | 编排 |
| S7 | 知识图谱：实体关系抽取 + 图可视化（M8 支线并入；RepoWiki 为工业形态参照，v0.1 够用即止） | 结构化知识、图可视化 |
| S8 | 多入口 Gateway：IM 渠道（飞书/Telegram 等）消息归一化接入 agent_loop，与 headless 合并（gateway 常驻进程——重审「不 daemon 化」原则） | 事件驱动、常驻服务 |
| 〔另排期〕 L1 | 本地模型接入：Ollama（OpenAI 兼容端点零代码接入，Qwen3 档起步；触发信号=需要零成本/离线验收链路时排期） | 本地推理 |
| 〔支线·可跳〕 M9 | 论文推送：cron 触发 + headless 任务 + arXiv 接入 + 语义过滤 | 外部API、无头任务、信息流过滤 |
| 〔远期〕 IDE 形态 | Web 内核装进 VSCode 插件/桌面壳（类 Cursor/Trae 形态，内核复用不动） | 套壳工程 |

## 四点五、企业级考量（贯穿性约定，2026-09-05 起）

终点是企业级 agent 开发，所以**不只是 M7.5 一个节点，而是贯穿每个里程碑的持续视角**：

- **每个里程碑收尾时，补一节"企业视角"**：这个模块进了大厂会面对什么（规模/并发/成本/合规）——已讲过的例子：错误分类与容错四件套（重试退避/超时/熔断/降级链）、模型质量分级降级、语义缓存
- **教学版 vs 工业版的差距永远点名**：M7 前 KnowledgeBase 每次 add_document 全量重建向量（O(N) 重算），M7 已用 Chroma 增量索引解决——现在的教学版 = InMemory 存取 + 词袋向量（离线零成本），工业版 = Chroma 落盘 + BGE-M3（增量 + 语义）；换件不换衣服是这套打法的验收标准
- **LLM 应用特有考点**：成本控制（token 计费随轮数增长）、可观测性（没有度量就没有熔断）、评测流水线（evals 已是雏形）
- **安全**：M9 前安排一讲——提示词注入（工具越权）、key 最小权限、审计日志

## 五、关键架构决策记录（索引）

> v0.37（2026-09-15）起决策记录拆分至 `docs/decisions/`（ADR 惯例：按里程碑一篇、append-only、不改写旧记录）；本节只留索引。
> 新里程碑收官动作：决策写进 docs/decisions/ 新篇 → 本表加一行 → 版本号照升。

| 篇 | 里程碑 | 主题 |
|----|--------|------|
| [001-m4-llm-embedding](decisions/001-m4-llm-embedding.md) | M4 | OpenAICompatible 统一类+PROVIDERS 配置表；Embedder 接口+词袋/BGE 双实现；BGE 阈值校准法；对称重构（EMBED_PROVIDERS）；key 安全；网关方向 |
| [002-m5-tools-agentic-rag](decisions/002-m5-tools-agentic-rag.md) | M5/M5.5 | 工具调用闭环（ToolRegistry/入史策略）；数据与代码分离（data/notes）；write_note 双重防线；Agentic RAG 放权（121→87 行） |
| [003-m6-memory](decisions/003-m6-memory.md) | M6.1–M6.3 | JSON 持久化+窄 except；摘要压缩三件套+覆盖不变量；架构五问裁定；M9 重定义；search_history/read_history 正交互补；P0-1 Session 整体落盘；skill/多智能体/guardrails/evals 方向注记 |
| [004-m6.3-lessons](decisions/004-m6.3-lessons.md) | M6.3 验收 | 列表身份陷阱；验收五轮 saga；元教训四条（测试污染/进程复活/自证预言/停止规则）；污染分层防御；摘要纪律；防递归检查点 |
| [005-p1-refactor](decisions/005-p1-refactor.md) | P1 | 回归测试落地（ScriptedLLM）；路线重排（MCP 提前）；ToolContext 收敛；路径真值源上收 paths.py（evals 断链事故） |
| [006-m7-vector-store](decisions/006-m7-vector-store.md) | M7 | VectorStore 接口+双实现（InMemory/Chroma）；内容指纹增量同步；删除安全阀（绝对下限+比例）；验收与评审修复轮 |
| [007-m7.5-gateway](decisions/007-m7.5-gateway.md) | M7.5 | 网关四件套（记账/重试超时/双档缓存/熔断降级链）；时间戳注入投影；三方评审修复轮 |
| [008-m6.4-consolidation-evals](decisions/008-m6.4-consolidation-evals.md) | M6.4/评测 | 固化四段管线（萃取→审查→硬校验→落盘）；LLM-as-judge（跨供应商裁判）；评测题库分层；强杀丢数据边界 |
| [009-mcp](decisions/009-mcp.md) | MCP | stdio+HTTP 双传输；服务器侧沙箱；mcp__ 前缀冲突治理；死菜摘牌；mcp_servers.json 配置化；Context7/GitHub 真三方验收 |
| [010-streaming](decisions/010-streaming.md) | streaming | generate_stream 伪流默认；merge_stream_chunks 分片重组；网关四衣流式语义；取消走 KeyboardInterrupt 通道 |
| [011-phase2-s1](decisions/011-phase2-s1.md) | 阶段二/S1 | 阶段二规划拍板（界面是壳/依赖驱动排序）；S1 多会话 active+archive；六标的业界调研；聊天网关与本地模型调研 |
| [012-s2-web](decisions/012-s2-web.md) | S2 | S2a 分层清账（orchestrator 正名/内核外设分离/assemble 真值源）；S2b Web 壳（Run 三接口/SSE/Run Store/协作式取消/会话切回）；验收修复轮（人设保证/自我画像/流中取消③/每轮落盘/任务视图/历史回放/md 渲染/测试污染事故）；会话标题提炼（LLM 主题标签）；learned 幻觉污染清理；会话数量不稳定修复（空会话守卫/同秒序号/互斥锁）；新会话裸奔修复（ensure_persona 多调用点+归档后落盘） |
| [013-docs-adr-split](decisions/013-docs-adr-split.md) | 文档治理 | ADR 拆分本身（触发信号兑现；57 条 sha256 校验零改写） |
| [014-product-positioning](decisions/014-product-positioning.md) | 产品定位 | 通用个人 agent；三场景并列数据驱动排序；任务=个人待办；联网前置 S3（最低防护随行） |
| [015-web-tools](decisions/015-web-tools.md) | 联网工具 | web_search+fetch_web；搜索 Provider 供应商化（Tavily 先行/双路由留位）；SSRF 栅栏（DNS 后逐 IP 检查）；条件注册；侧栏归档时间戳 |
| [016-todos](decisions/016-todos.md) | 个人待办 | TodoStore 独立存储（跨会话资产+store 锁）；工具三件 vs TodoWrite 裁定；UI/API/agent 共用单实例；侧栏待办面板 |
| [017-s3-security](decisions/017-s3-security.md) | S3 安全 | 权限分级 L0/L1（L2 确认判据已定留 S4）；审计 registry 收口；注入界碑；威胁模型=不可信内容经模型之手变动作 |
| [018-s4a-file-tools](decisions/018-s4a-file-tools.md) | S4a 文件工具 | 文件四件（write_file 带 diff）；workspace 围栏（__file__ 锚定+敏感黑名单读都不行）；builtin 按家族拆三件；note vs file 分工边界 |
| [019-s4b-terminal-confirm](decisions/019-s4b-terminal-confirm.md) | S4b 终端+确认 | run_command（超时/截断/cwd 锚定）；白名单免确认双条件（用户拍板粒度）；确认缝 registry 收口（loop 第四条缝）；waiting_approval 挂起+断线重弹；拒绝回灌不炸会话；批准拒绝都落审 |
| [veto-archive](decisions/veto-archive.md) | 否决档案（活清单） | 被否决方案+原因+重新考虑触发信号，持续追加 |

## 已知问题（活清单）

> 非阻塞但已登记的病灶，随里程碑推进逐个消除。

- **任务视图偶发不切换**（2026-09-16 首次报告，09-17 S4b 验收复现一次）：点侧栏其他会话再点回时，主视图第一次不切换（多点一次恢复）。推测与 SSE 事件流消费 / DOM 状态竞争有关，未定位根因。触发信号=用户再次报告或前端框架化时一并排查。
