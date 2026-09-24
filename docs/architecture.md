# Personal Agent 架构图

> 版本：v0.68（2026-09-24）｜随着里程碑推进持续迭代此文档
> 更新规则：架构有变更（新增层/模块）时，同步更新本文件并提升版本号；架构决策（v0.37 起）写进 docs/decisions/ 并在本文件索引表加行
> 产品定位（v0.50 起，见 [product.md](product.md) v2）：个人执行助手——执行主轴 + 记忆护城河 + 通用外延（[021](decisions/021-direction-decisions.md)）

## 设计原则

1. **接口与实现分离** —— `LLM` 是接口，mock/DeepSeek/本地模型随便换，上层不动
2. **分层解耦** —— 每层只做自己那件事，任何一层都能单独替换
3. **从简到真** —— 每个模块先手写"教学版"跑通原理，再换"工业版"（词袋→真embedding，mock→DeepSeek）
4. **渐进演化** —— 阶段一"学习助手"的地基，正好是阶段二"coding agent"的积木
5. **活 spec（方向提前定，细节临期定）** —— 路线图只锁方向（一行一里程碑）；详细方案在开工时才写（最后责任时刻决策）；实现后必回写决策记录。spec 与代码同生命周期，不做预言式大设计——MCP 三次顺延、M6.3 因 M6.2 翻车而丰富，皆是实证
6. **Human on the Loop（阶段二起，业界调研吸收）** —— 执行成本归零后，人的注意力是唯一稀缺资源：产品让人「定义、掌舵、评判」（定义任务、设定边界、确认结果），agent 负责执行。状态跟踪、单点批准、交付摘要不是附加功能，而是产品核心
7. **能力建设优先（2026-09-23 产品定调）** —— 范围裁剪的判据是「这能力是不是产品一等公民」，不是「当前数据/语料/测试量用不用得上」。产品不是 demo：按它该有的样子设计，数据会追上来。砍某能力时自问——是 YAGNI（完整形态也永远不需要 → 砍），还是数据怯懦（现在用不上 → 建）？「当前数据少」不构成第三种答案。与「从简到真」不冲突：那条管实现路径（教学版→工业版分步建），本条管设计范围（不因数据小而砍能力）。S7 图谱草案曾以「14 篇笔记才 289 行」论证砍范围——被纠正后按一等公民规格建（别名对齐/增量同步/删除安全阀一次做全）

## 一、系统总体架构（目标全貌）

```
┌──────────────────────────────────────────────────────────────┐
│                        用户交互层                              │
│  ✅ CLI 壳 ｜ ✅ Web：对话视图 + 任务视图（列表+计划面板，S5c）  │
│            + 记忆面板（025）+ 知识图谱面板（S7b 力导向图）        │
│            ｜ 以后：IM 渠道（S8）                                 │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│  入口 __main__.py（CLI）/ server/__main__.py（Web）             │
│    装配唯一真值源：orchestrator/assemble.py（依赖注入，          │
│    条件装配：无 key/假模型 → 不挂路由，kb/llm 缺席 → 不注册）    │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│  ★ orchestrator/ 编排层：run_turn 主循环（大脑）                │
│                                                              │
│  ✅ Agent 对象（S5a，行为定义与引擎分离）                        │
│     system_prompt（learned 三桶快照注入）/ 工具子集 /           │
│     预算 / router（M10 场景路由，None=原生路径）                │
│     spawn_subagent（S5c）：子 Agent 实例化+临时会话            │
│           → 只回传结论 = 噪声隔离（S6b 并行/S6a worktree）      │
│     spawn_step（S6c）：计划步骤派发+自动回写 = 真编排          │
│                                                              │
│   ┌─────────────────────────────────────────────────┐        │
│   │ ✅ 感知 → 决策 ⇄ 行动(工具) → 观察（ReAct）        │        │
│   │ ✅ 轮首一针：场景路由（M10，Jev 判 direct/          │        │
│   │    single_tool/complex → 控制首次菜单形状）         │        │
│   │ ✅ 投影注入：时间戳 + 活跃计划（S5b 轮首快照）        │        │
│   │ ✅ plan-then-act（S5b）：make_plan 人审掌舵 →      │        │
│   │    执行 → update_plan_step 回写 → finish 终态收官   │        │
│   │ ✅ 切批执行（S6b）：连续 spawn 段线程池并行          │        │
│   │ ✅ 运行时校验：L0/L1 白名单 + L2 确认缝（S3/S4b）    │        │
│   │ 以后：跨进程隔离 / 步骤级并行（触发信号在案）          │        │
│   └─────────────────────────────────────────────────┘        │
└──────┬───────────────────────────────────┬───────────────────┘
       │                                   │
       ▼                                   ▼
┌────────────────┐          ┌────────────────────────────────┐
│  knowledge/    │          │  core/ 地基                    │
│  知识层         │          │  ✅ types / 向量数学            │
│ ✅ RAG 检索     │          │  ✅ 网关四件套（RobustLLM：      │
│ ✅ BGE-M3      │          │    记账/重试超时/双档缓存/熔断    │
│ ✅ 向量库+增量  │          │    + FallbackLLM 降级链）        │
│   data/notes/  │          │  ✅ LLM 接口+OpenAICompatible    │
│   （数据外置）  │          │  ✅ Jev 决策模型（M10：JevClient  │
│ ✅ 知识图谱     │          │    + ScenarioRouter 三态）       │
│   （S7：抽取   │          │  ✅ evalkit 纯函数（024）        │
│   +增量同步    │          │ 以后：更多供应商+多模型路由      │
│   +查询原语）  │          │                                │
└────────────────┘          └────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│  memory/  记忆层                                               │
│   短期: ✅ messages 列表（会话内）                             │
│   跨会话: ✅ JSON 持久化 + 多会话（S1 active+archive）          │
│   压缩: ✅ 滚动摘要+原文窗口（投影，底片不动）                  │
│   固化: ✅ data/learned 三桶（萃取→审查→硬校验，也是 S5a        │
│         Agent prompt 的注入源——AGENTS.md 式读取侧已兑现）        │
│   用户级: ✅ ~/.personal-agent/user.md（M6.5 分流：scope=user    │
│         条目仓库外落盘+主 agent 注入【用户记忆】段+敏感凭证      │
│         硬校验禁令；子 agent 不注入）                            │
│   计划: ✅ plan 域（S5b：PlanBoard 事件史，随 session 落盘）    │
│  以后：记忆面板用户级分栏；检索分层（032 v2 信号触发）           │
└──────────────────────────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│  tools/  工具层（M5 起，全项目灵魂）                             │
│  ✅ ToolRegistry（审计收口 + L2 确认缝 + receives_confirm）     │
│  ├── ✅ 八族：time / history / notes / files（S4a）            │
│  │        / terminal（S4b）/ web（015）/ todo（016）           │
│  │        / plan+spawn（S5b/S5c 编排工具）                     │
│  ├── ✅ MCP 客户端（stdio+HTTP，配置化接入）                   │
│  └── 以后：skills/（技能包格式，S6）                            │
└──────────────────────────────────────────────────────────────┘

══════════════ 独立于运行链路 ══════════════

┌──────────────────────────────┐  ┌───────────────────────────┐
│  server/  Web 壳（可选组）    │  │  evals/  离线评估           │
│  ✅ Run 三接口 + SSE 断线重放 │  │  ✅ evalkit 内核（024）     │
│  ✅ Run Store 单锁状态机      │  │  ✅ 检索指标三路并评+归因    │
│  ✅ waiting_approval 挂起     │  │  ✅ LLM-as-judge           │
│  以后：Run Store 外置（多实例）│  │  以后：对抗题库回归          │
└──────────────────────────────┘  └───────────────────────────┘
```

## 二、各层状态一览

| 层 | 现状 | 建成后 |
|---|------|--------|
| core 地基 | ✅ types / llm 接口+实现 / 网关四件套 / 账本 / 向量数学 / **Jev 决策模型接入（M10：JevClient+ScenarioRouter，urllib 零依赖、三态生命周期、选项空间封闭注入免疫）**——最底层不反认上层（S2a 依赖方向拨正） | 更多供应商 + 多模型路由 |
| orchestrator 主循环 | ✅ ReAct 雏形 + M5.5 Agentic RAG：决策→执行→观察→再决策（5轮保险丝）；检索权已移交模型，主循环不再直连 kb；streaming 流式消费（分片边收边打→merge 拼回复；内部调用照旧非流）；S2a 内核/外设分离：run_turn 零 input/print，I/O 走 on_text/on_event/should_cancel/on_confirm 四条缝（S4b 加确认缝）；S2b 协作式取消两检查点；run_turn 返回 RunResult 三态枚举（COMPLETED/CANCELLED/FAILED，取消/模型全挂不再靠 None 二义反推，S4 评审修复轮）；**S5a Agent 对象**：行为定义与执行引擎分离（agent.py 六字段 frozen；SYSTEM_PROMPT 外置为 DEFAULT_SYSTEM_PROMPT，learned 三桶 AGENTS.md 式快照注入 prompt 尾）；run_turn agent 化（registry 参数退场、空菜单折叠回 None 防「不传≠空」API 坑、幻觉点菜 assert 炸→错误串反馈环）；**S5b plan-then-act 两针**：活跃计划投影注入（`_plan_stamp`，时间戳同款手法，无活跃零开销）+ 计划事件 drain 转发（工具执行后、tool_result 前，因果序，零新缝）；**M10 场景路由轮首一针**：`_route_first_menu`（direct 藏菜单进语义缓存命中区/single_tool 单工具菜单 LLM 只填参数/complex 全量自决；半路由——工具结果回灌后循环尾归还全量菜单，循环决策权归还模型） | 并行工具调用 / 更复杂的规划策略 |
| LLM 接入 | ✅ OpenAI兼容统一类+配置表(deepseek/deepseek-flash/siliconflow，M10 备用链按 prefix 去重——同供应商同故障域不陪葬) + 进程内网关(M7.5：记账/重试超时/精确缓存+语义缓存默认关(029：跨上下文串味，env 开关)/熔断三态/降级链+优雅兜底)；主力默认 deepseek-flash（M10，bench 题86%+ECE 0.042） | 更多供应商 + 多模型路由 |
| knowledge | ✅ Embedder接口+词袋/BGE双实现 + loader(数据外置) + VectorStore接口+双实现(M7：InMemory教学版/Chroma工业版落盘) + 增量同步(内容指纹差集)；检索命中带溯源（SearchHit=块+相似度+来源面单，query 全链路透出，S4 评审修复轮）；**知识图谱（S7a）**：graph.py 三层结构（值对象/GraphStore 三道校验闸门/查询原语 resolve-neighbors-path-overview）+ extract.py 封闭抽取（关系白名单+实体挂靠+禁推断；出处签名强制）+ sync_graph 指纹差集增量（graph.json 知识资产进 git）+ GRAPH_LOCK 图级并发锁（S7b：跨线程共享资产串行） | 图谱质量迭代轮（触发信号在活清单） |
| memory | ✅ 会话内记忆 + 跨会话 JSON 持久化（M6.1）+ 摘要压缩（M6.2）+ 温层检索 search_history/read_history（M6.3）+ Session 状态整体持久化（压缩缓存随底片落盘，重启不再重压）+ 记忆固化 data/learned（M6.4：萃取→审查→硬校验→落盘）+ 多会话管理（S1：active+archive，/new 归档重开）+ restore_session 原语（S2a：归档写回 active 后删除，move 语义）+ **plan 计划域（S5b）**：PlanBoard/PlanState/事件史（append 修订、fold 视图、显式终态制），生命周期=会话随 session.json 落盘，老文件宽进 + **用户级记忆（M6.5）**：固化管线 scope 分流→~/.personal-agent/user.md 仓库外落盘（不进任何 git），主 agent prompt 注入【用户记忆】段（项目桶之后，元记忆首轮必加载），敏感凭证正则硬禁令，审查对 user 条目从宽（跨项目污染代价高），子 agent 不注入（执行器非陪伴者） | 记忆面板用户级分栏；检索分层（032 v2 信号） |
| tools | ✅ Tool+ToolRegistry+内置工具按家族分件（time/history/notes 三族，S4a 拆分）；write_note 安全栅栏+查重闸门；**read_notes 越界读修复(029：与 write_note 同款 resolve+is_relative_to 防线)**；search_notes=Agentic RAG 入口；search_and_summarize=复合工具(内部调LLM，Sub-agent原型)；联网工具 web_search/fetch_web（015：Tavily Provider+SSRF 栅栏+条件注册）；MCP 外部工具配置化接入；文件四件 read_file/search_code/list_dir/write_file+diff（S4a，workspace 围栏：__file__ 锚定+敏感黑名单读都不行，**search_code rglob 也挡 .env(029)**）；终端执行 run_command（S4b：白名单只读免确认/L2 确认缝 registry 收口/批准拒绝都落审；评审修复轮加参数级拦截——find -exec/sort -o 等「只读命令名+危险参数」也弹确认）；计划三件（S5b）：make_plan（人审掌舵点，复用确认缝）/update_plan_step（状态回写+回灌带最新视图）/finish_plan（显式终态程序闸）；spawn_subagent（S5c：子 agent 分派只回传结论=噪声隔离，禁止单硬编码防递归，**_FORBIDDEN 含历史工具(029：防父会话泄漏)**，receives_confirm 确认缝透传，worktree 沙箱+并行(S6a/S6b)）；spawn_step（S6c：计划步骤派发——标 in_progress→派子任务→自动回写 done/failed，make_plan→spawn_step→finish_plan 编排链，_FORBIDDEN 封死子 agent 编排）；**图谱双件（S7a）**：query_graph（三原语 neighbors/path/overview，错误串指路，L0 只读）/sync_graph（界面可操作增量抽取+force 全量重抽+落盘回报；ctx.llm 缺席不上菜单；子 agent 禁用）；write_note 回灌带 sync_graph 闭环提示 | 更多工具 + skills |
| server Web 壳 | ✅ S2b：FastAPI+SSE（web 可选组）——Run 三接口分离（创建 202/事件订阅/取消）+ 内存 Run Store（状态机单一终态、事件 append-only 带 seq、单锁 create_if_idle 原子、**广播模型(029)：订阅者独立队列+终态补发哨兵，聊天页+任务页同时订阅不竞争**）+ Last-Event-ID 断线重放 + 会话列表/新开/切回 + 静态三件零构建链；只绑 127.0.0.1。验收修复轮：人设保证（ensure_persona 装配不变量）、每轮落盘、取消检查点③（流中即时）、任务视图最小版（/tasks+GET /api/runs）、历史回放（GET /api/messages）、md 渲染（marked vendored，**escape-before-parse+协议白名单(029)**）、Enter/Esc 键盘。S4b：waiting_approval 挂起态（进单锁口径、取消视拒、confirm.request/resolved 事件断线重放重弹）+ confirm 裁决端点 + 前端确认弹窗。S5c：任务视图 Run 详情展开（计划面板——EventSource 消费 plan.*/tool.*/run.* 事件，**点分命名(029)**，fold 与后端 PlanState.view 同构，断线重放免费）。S7b：知识图谱面板（/graph 页面伺服 + GET /api/graph 全量 nodes/edges/stats + POST /api/graph/rebuild force 重抽——与对话内 sync_graph 工具共用 GRAPH_LOCK 图级锁，常驻进程下面板重建端点（请求线程）与工具调用（worker 线程）互斥） | 主聊天视图迁移 FW 站（渐进）；Run Store 外置（多实例触发） |
| evals | ✅ **evalkit 内核**（src/agent/evalkit：指标/指纹判定/归因/judge 解析纯函数，零依赖可拿走，024）+ evals 壳（三路并评+miss 归因，30 题形态分层，8 万字混合语料，022 混合检索裁定不立项）+ LLM-as-judge 回答质量(基线 13/13 合格, 0% 错误, 全轮 ¥0.011) | 更难的对抗题库 + 回答质量回归 |
| data | ✅ data/notes/*.md 笔记库(与evals/线上共用同一语料)；agent 可自主写入(自我进化闭环已验证)；data/memory/session.json 对话记忆(M6.1，gitignore 运行时数据)；data/learned/*.md 项目级长时记忆(M6.4，进 git；S5a 起兼任 Agent prompt 注入源——AGENTS.md 式读取侧)；**data/graph.json 知识图谱（S7a，进 git——notes 的结构化投影，知识资产可审查可 diff）** | 长文档、多来源 |

## 三、三条演进主线

```
主线1：模型分工    单一主力 ──M10──→ 决策/生成分离（Jev 路由 + deepseek-flash 生成）
                  ──以后──→ 更多供应商 + 多模型路由
主线2：能力循环    只说话 ──M5──→ 会用工具(ReAct) ──M5.5──→ 自主决定查不查(Agentic RAG)
                  ──S5c──→ 派分身(spawn 噪声隔离) ──以后──→ 多agent协作(S6)
主线3：执行架构    纯反应式 ──S5a──→ Agent 对象(行为定义分离) ──S5b──→ plan-then-act
                  (蓝图+掌舵+回写) ──以后──→ 并行工具调用 / S6 真编排
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
| M7.5 ✅ | 生产加固：网关四件套（记账+重试超时+精确缓存/语义缓存默认关(029)+熔断）｜降级链 + 优雅兜底 | 容错、可观测性、成本控制 |
| M6.4 ✅ | 记忆固化：对话→分类萃取→审查→硬校验→长时记忆沉淀（data/learned/*.md，退出复盘） | 分层记忆、萃取纪律、防幻觉污染 |
| M6.5 ✅ | 用户级记忆位置（[034](decisions/034-m6.5-user-memory.md)：021 护城河从项目级扩到个人级）：固化管线 scope 分流（铁律从「用户级不记」→「分流仓库外」）、~/.personal-agent/user.md 单文件（同款行格式零翻译）、主 agent 注入【用户记忆】段、敏感凭证硬禁令、审查从宽、子 agent 不注入；位置 env 注入化（CORTEX_USER_MEMORY） | 作用域分离、隐私位置设计、Write-Path 权衡 |
| MCP ✅ | 外部工具动态发现（三次顺延的集结号，coding agent 最直接积木）｜stdio+HTTP 双传输、配置化装配、真三方验收 | 协议、动态工具 |
| streaming ✅ | 流式输出：generate_stream 接口（默认伪流，老实现零改动）+ 真流实现 + 分片重组纯函数 + 网关四衣流式语义；用户取消复用中断通道 | 增量协议、生成器惰性 |
| 阶段二 | —— coding agent 产品化（2026-09-13 规划拍板，方向提前定、细节临期定）—— | 实战整合 |
| S1 ✅ | 多会话管理：/new 会话隔离（active+archive：位置固定无指针、文件名=身份/标题=标签、先存后清、原地清不 rebind） | 状态管理 |
| S2 ✅ | 产品骨架 v1 对话视图：S2a 清账（agent_loop 正名 orchestrator 编排层、run_turn 内核/外设分离、assemble 装配单一真值源）→ S2b Web 壳（FastAPI+SSE：Run 三接口分离、内存 Run Store 单锁状态机、断线重连重放、会话切回、零构建链前端三件）；任务视图当时按双视图预留未实现（后由 FW 站 v2 + S5c 计划面板兑现） | 前后端、SSE、Run 状态机 |
| S3 ✅ | 安全底座：工具权限分级（L0 只读/L1 写/L2 确认留 S4）+ 注入界碑（外部内容包裹声明+免疫条款）+ 审计日志（registry 收口 append-only jsonl 按天滚动，分级截断） | 安全 |
| S4 ✅ | 工具访问三件套：文件读写 / 代码定位 / 终端执行（coding agent 的腿）；工业版目标=LSP 反馈环（Shadow Workspace 思路）。**S4a ✅ 文件四件（read/search_code/list_dir/write_file+diff）+ builtin 拆分（time/history/notes 三族）+ workspace 围栏（__file__ 锚定+敏感黑名单）**；**S4b ✅ 终端执行 run_command + L2 确认机制（只读白名单免确认/registry 确认缝第四条缝/waiting_approval 挂起/Web 弹窗+CLI input/批准拒绝都落审）** | 文件系统、进程调度 |
| FW ✅ | 前端基建（021 裁定兑现，见 [023](decisions/023-fw-preact-pilot.md)）：Preact+Vite 脚手架（frontend/ 源码 → static/fw/ 产物，产物进 git）；任务视图 v2 试点完成（状态驱动替代 innerHTML 同步矩阵，JSX 自动转义结构性免疫注入）；构建链第一课：子路径部署必须 base:"/fw/"。**记忆面板 v1 ✅（[025](decisions/025-memory-panel.md)，护城河可视化）：learned 三桶查看/编辑/删除 + 行号定位协议 + 坏行宽容（幻觉清理入口）**。**知识图谱面板 ✅（S7b，[036](decisions/036-s7b-graph-visualization.md)，第三入口）：自研 SVG 力导向 + 进阶交互（路径高亮/搜索定位/类型过滤）**。下一步：主聊天视图渐进迁移 | 声明式渲染、状态驱动 |
| S5 | 执行架构（2026-09-18 重定义，[021](decisions/021-direction-decisions.md)）：**Agent 对象抽象**（独立 system prompt/工具子集/预算/记忆——吸收原 skill 站的 SYSTEM_PROMPT 外置与 AGENTS.md 式 learned 读取侧，二者本就是 Agent 对象的属性）+ **plan-then-act**（轻量规划，plan 即 Human on the Loop 掌舵点；plan 载体=独立最小结构挂 Run 事件流）+ **spawn_subagent**（子 agent 只回传结论=噪声隔离，S6 判据提前兑现）；技能包格式后置并入 S6。对抗机制（critic）不内置——Agent 对象落地后从机制变配置，触发信号见 [026](decisions/026-adversarial-critic-deferred.md)。**S5a ✅ Agent 对象**（[027](decisions/027-s5-execution-architecture.md)：三拍板定 S5b 形状——模型自判/append 修订/显式终态制；六字段 frozen、菜单与执行分离、两段式验收 sha256 锁死搬家等价、learned 快照三原则）。**S5b ✅ plan-then-act**（[027](decisions/027-s5-execution-architecture.md)：三层结构 值对象/PlanState/PlanBoard，事件溯源最小版——append 修订+fold 视图；双视图 轮首快照+工具结果回灌导航；修订复用 make_plan 状态机分叉；人审掌舵复用 S4b 确认缝）。**S5c ✅ spawn_subagent + 计划面板**（[027](decisions/027-s5-execution-architecture.md)：噪声隔离=主底片只多一条结论消息，临时会话不落盘不受单锁；禁止单程序侧硬编码防递归；receives_confirm 显式通道人审不分主子；任务面板=任务视图 Run 详情展开，SSE 消费 plan.* 事件 fold 与后端同构）。**S5 全站收官** | 执行架构、规划 |
| S6 ✅ | 多 agent 协作：worktree 隔离机制 + 真编排（技能包格式自 S5 后置并入；spawn_subagent 已在 S5 兑现噪声隔离判据；2026-09-22 优先级拍板 B 先行已落地——冒烟套件作 S6 安全网，spawn 内部重构外部行为自动把关）。**S6a ✅ worktree 隔离**（[030](decisions/030-s6a-worktree-isolation.md)：WORKSPACE_ROOT 注入化+确认缝合回+子 registry 重锚+残留回收；跨进程挂触发信号）。**S6b ✅ 并行 spawn**（[031](decisions/031-s6b-parallel-spawn.md)：连续 spawn 段线程池并行+确认缝 _confirm_lock 串行化+结果按点菜顺序回填）。**S6c ✅ 真编排 spawn_step**（[033](decisions/033-s6c-orchestration.md)：计划步骤派发自动回写 done/failed，make_plan→spawn_step→finish_plan 编排链；步骤级并行挂触发信号）。**S6 全站收官**——拆→派→隔离→并发→汇总最小闭环；技能包格式与跨进程仍挂触发信号 | 编排 |
| M10 ✅ | 场景路由与快慢分工（2026-09-21，[028](decisions/028-m10-scenario-routing.md)，model-bench 评测结论落地）：**决策/生成分离**——Jev choice 管意图识别（bench 路由 19/20，成本 1/20）挂 Agent.router 轮首一针；deepseek-flash 管生成（题库轨道 86% 第一、ECE 0.042）；参数填充归 LLM、循环决策归 harness（bench 三层分解）。三态生命周期硬约束（无 key 条件装配/单次故障 fail-open/持续故障熔断同款参数），route() 永不抛、返回 None=原生路径（无 Jev=cortex 功能完整）。确认闸门程序侧硬编码不动（bench：五模型安全确认无一全对，DS-V4-Pro 唯一裸奔）。（S5c 已于其后收官，`ee4efd4`） | 决策外包、fail-open、选项封闭注入免疫 |
| S7 | 知识图谱：实体关系抽取 + 图可视化（M8 支线并入；RepoWiki 为工业形态参照，v0.1 够用即止）。**S7a ✅ 数据层**（[035](decisions/035-s7a-knowledge-graph.md)：graph.py 三层结构+extract.py 封闭抽取+sync_graph 指纹差集增量+query_graph/sync_graph 双工具+实机闭环验收——孤岛诊断→补笔记→缺口自愈）。**S7b ✅ 图可视化**（[036](decisions/036-s7b-graph-visualization.md)：前端第三入口 /graph——自研 SVG 力导向+进阶交互（拖拽/缩放/邻居高亮/路径高亮/搜索定位/类型过滤）+重建图谱按钮；GET /api/graph 全量+POST /api/graph/rebuild（force 重抽）；GRAPH_LOCK 图级并发锁）。**S7 全站收官** | 结构化知识、图可视化 |
| S8 | 多入口 Gateway：IM 渠道（飞书/Telegram 等）消息归一化接入 agent_loop，与 headless 合并（gateway 常驻进程——重审「不 daemon 化」原则） | 事件驱动、常驻服务 |
| 〔另排期〕 L1 | 本地模型接入：Ollama（OpenAI 兼容端点零代码接入，Qwen3 档起步；触发信号=需要零成本/离线验收链路时排期） | 本地推理 |
| 〔支线·可跳〕 M9 | 论文推送：cron 触发 + headless 任务 + arXiv 接入 + 语义过滤 | 外部API、无头任务、信息流过滤 |
| 〔远期〕 IDE 形态 | Web 内核装进 VSCode 插件/桌面壳（类 Cursor/Trae 形态，内核复用不动） | 套壳工程 |

## 四点五、企业级考量（贯穿性约定，2026-09-05 起）

终点是企业级 agent 开发，所以**不只是 M7.5 一个节点，而是贯穿每个里程碑的持续视角**：

- **每个里程碑收尾时，补一节"企业视角"**：这个模块进了大厂会面对什么（规模/并发/成本/合规）——已讲过的例子：错误分类与容错四件套（重试退避/超时/熔断/降级链）、模型质量分级降级、语义缓存
- **教学版 vs 工业版的差距永远点名**：M7 前 KnowledgeBase 每次 add_document 全量重建向量（O(N) 重算），M7 已用 Chroma 增量索引解决——现在的教学版 = InMemory 存取 + 词袋向量（离线零成本），工业版 = Chroma 落盘 + BGE-M3（增量 + 语义）；换件不换衣服是这套打法的验收标准
- **LLM 应用特有考点**：成本控制（token 计费随轮数增长）、可观测性（没有度量就没有熔断）、评测流水线（evals 已是雏形）
- **安全**：已落地 S3（注入界碑+审计收口）与 S4（权限分级+确认缝）；持续考点=工具越权面随工具族扩张而生长（每加一族工具过一遍白名单/围栏检查）

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
| [020-s4-review-hardening](decisions/020-s4-review-hardening.md) | S4 评审修复轮 | 外部评审 38 条分类消化（6 硬伤即修/12 已知边界按触发信号/3 方向分歧入待讨论）；结构化日志分层；RunResult 三态；降级显式化；参数级白名单；SearchHit 溯源；mypy+ruff 进 CI（首轮抓到 rename 端点漏 import 真 bug） |
| [021-direction-decisions](decisions/021-direction-decisions.md) | 方向定稿 | 定位 v2（执行主轴+记忆护城河+通用外延）；S5 重定义（Agent 对象吸收 SYSTEM_PROMPT 外置+learned 读取侧，+plan-then-act+spawn_subagent）；编排文本编辑+mermaid 阅读（拖拽否决）；前端框架化翻案（Preact+Vite，任务视图试点） |
| [022-hybrid-retrieval-eval](decisions/022-hybrid-retrieval-eval.md) | 混合检索评估 | 三路并评+miss 归因（8 万字混合语料，30 题形态分层）；BGE 生产 miss 5/26、grep 补救率 0% → 不立项；洞察：grep 增量随向量能力增强而衰减、中×英 query 语料是 grep 死区、miss 主因是闸门截断 |
| [023-fw-preact-pilot](decisions/023-fw-preact-pilot.md) | FW 前端基建 | Preact+Vite 脚手架（frontend/→static/fw/，产物进 git）；任务视图 v2 试点（状态驱动）；构建链第一课：子路径 base:"/fw/"；dev 工作流（uvicorn+HMR proxy） |
| [024-evalkit](decisions/024-evalkit.md) | evalkit 评测内核化 | 评测纯函数抽 src/agent/evalkit（指标/指纹/归因/judge 解析，零依赖拷走即用）；不引 Ragas/DeepEval 的裁定（原理件手写，工程件可引，触发信号记档）；搬家当天 mypy 抓出 re.search 潜伏 bug；升格触发信号=第二个真实消费者 |
| [025-memory-panel](decisions/025-memory-panel.md) | 记忆面板 v1 | learned 三桶查看/编辑/删除（护城河可视化）；行号定位协议（append-only 下稳定）+ 坏行宽容（幻觉清理入口）+ 编辑保留日期（时间戳归程序管）；FW 新栈第二入口，多入口共享 chunk |
| [026-adversarial-critic-deferred](decisions/026-adversarial-critic-deferred.md) | 对抗机制裁定 | S5 不内置 critic（2026-09-12 悬案结案：基线 0% 错误无痛点；人审已是更强对抗者；Agent 对象落地后 critic 从机制变配置）；三条触发信号挂档（plan 挑刺/执行监督/对抗题库） |
| [027-s5-execution-architecture](decisions/027-s5-execution-architecture.md) | S5 执行架构 | 三拍板（plan 触发模型自判/修订 append/完成显式终态制）；S5a Agent 对象：六字段 frozen、菜单与执行分离（子集=菜单视图非第二 registry）、两段式验收（sha256 锁死搬家等价+行为升级单列）、空菜单折叠 None（不传≠空）、assert 退场反馈环接管、learned 快照三原则（落盘格式零翻译/空桶跳过/锁老文加新文）；S5b plan-then-act：三层结构与 _pending 挂 board（finish 事件不丢）、fold 事件史+孤儿事件跳过、双视图（轮首快照+回灌导航）、修订复用 make_plan 分叉、掌舵复用确认缝、事件转发零新缝；S5c spawn+计划面板：噪声隔离（临时会话不落盘不受单锁）、禁止单硬编码、receives_confirm 人审不分主子、前端 fold 同构、prompt 能力扩张 hash 更新史 |
| [028-m10-scenario-routing](decisions/028-m10-scenario-routing.md) | M10 场景路由 | model-bench 证据摘要（适用域=原子决策）；三类场景 direct/single_tool/complex；Jev 挂 Agent 轮首一针不进 gateway；半路由（循环尾归还全量菜单）；single_tool 不用 tool_choice（逃生门）；三态生命周期（无 key 条件装配/fail-open/熔断同款参数）；direct 进语义缓存命中区；主力切 deepseek-flash + 备用链 prefix 去重；确认闸门程序侧硬编码（bench 正名） |
| [029-review-round-sonus](decisions/029-review-round-sonus.md) | 评审修复轮（sonus） | 外部评审 8 项坐实硬伤分类消化；R1 读边界不对称（read_notes 越界+search_code .env 泄漏）；R2 XSS（escape-before-parse+协议白名单）；R3 事件名契约（点分统一）；R4 plan 生命周期（/new 清+换血恢复）；R5 spawn 禁止单加历史工具；R6 SSE 广播模型（单队列竞争→订阅者独立队列+终态哨兵）；R7 归档保留手工名；R8 语义缓存默认关（env 开关）；四视角吸收（已有设施覆盖） |
| [030-s6a-worktree-isolation](decisions/030-s6a-worktree-isolation.md) | S6a worktree 隔离 | 四拍板（WORKSPACE_ROOT 注入化/确认缝合回/生命周期+残留回收/跨进程不进本轮）；_worktree_registry（五件重锚+其余共享+审计同源）；merge_worktree 确认缝（批准合回拒绝整棵丢弃）；commit 显式带 cortex-agent 身份（CI 环境无关）；三实踩入档（默认参数固化 monkeypatch 无效/reset --hard 误伤未提交/CI git 身份差异） |
| [031-s6b-parallel-spawn](decisions/031-s6b-parallel-spawn.md) | S6b 并行 spawn | 四拍板（只并行 spawn/确认缝 _confirm_lock 串行化/结果按点菜顺序回填/描述中性引导）；切批+ThreadPoolExecutor 并行（IO-bound 无需进程）；_execute_tool_calls 抽函数；实踩入档（merge_stream_chunks 按 index 归并、测试 _call 缺 index 导致同轮多 spawn arguments 拼接） |
| [032-memory-system-principles](decisions/032-memory-system-principles.md) | 记忆体系调研裁定 | Trae/Qoder/Pi 三系记忆调研对照 cortex 现状：不搬 5 大类（user_preference 撞隐私红线）；借「目录树先行+按需检索」挂 v2 触发信号（注入 token 成本阈值——v1「文件数>10」是死信号已废弃：文件数=分类数=3 结构常数）；显式区分 append-only vs 可删（下一站顺手做）；反思保持异步旁路（Qoder 四缺陷教训）+ 条目腐烂实证（行号/时点快照类条目变假知识）；Knowledge 归知识层不混记忆层；Mycelium 反设计不适用（flash 档判断力不足）；Tombstone/DeepSeek 前缀缓存列为待确认不立项 |
| [033-s6c-orchestration](decisions/033-s6c-orchestration.md) | S6c 真编排 | 焊点认知（make_plan 与 spawn_subagent 两条平行线靠模型临场手工桥接——会忘/错位/重复，spawn_step 把手工桥变程序焊缝）；四拍板（新工具不加参数/自动回写保留步骤间掌舵/串行为主+步骤级并行挂触发信号/子 agent 禁 spawn_step）；_FAILURE_PREFIXES 成败判据；S6 全站收官（拆→派→隔离→并发→汇总最小闭环） |
| [034-m6.5-user-memory](decisions/034-m6.5-user-memory.md) | M6.5 用户级记忆 | 位置 ~ /personal-agent/user.md 单文件（Trae 两层参照，仓库外不进任何 git）；scope 分流（铁律 2 从「不记」→「分流」，M6.4 红线解扣）；敏感凭证正则硬禁令（宁误杀不漏放）；审查对 user 从宽（跨项目污染代价）；子 agent 不注入（临期修正：执行器非陪伴者）；面板分栏挂可选子项 |
| [035-s7a-knowledge-graph](decisions/035-s7a-knowledge-graph.md) | S7a 知识图谱数据层 | 设计原则第 7 条首个实证（曾以语料小砍范围被纠正，按一等公民规格建）；五拍板（封闭抽取三防线/出处签名强制/graph.json 进 git/查询三原语/界面可操作 sync_graph）；实机闭环全通（孤岛诊断→补笔记→增量抽取→缺口自愈——知识地图告诉你哪里没学透）；三实踩入档（patch 生命周期对齐/并行会话暂存范围/ScriptedLLM 多调用备脚本） |
| [036-s7b-graph-visualization](decisions/036-s7b-graph-visualization.md) | S7b 图谱可视化面板 | 两选型（自研 SVG 力导向零依赖/进阶交互——用户再纠「基础版」推荐，原则 7 条二次实证）；图数据一次全量拉回前端（搜索/路径 BFS/过滤全在内存算）；重建端点复用抽取管线；GRAPH_LOCK 图级并发锁（面板重建 vs 对话内 sync_graph，常驻进程新并发面，锁属数据层）；AppContext 挂 graph；实机抓首帧 bug（派生数据初始化必须早于首帧消费渲染——useEffect 是渲染后） |
| [veto-archive](decisions/veto-archive.md) | 否决档案（活清单） | 被否决方案+原因+重新考虑触发信号，持续追加 |

## 已知问题（活清单）

> 非阻塞但已登记的病灶，随里程碑推进逐个消除。

- **任务视图偶发不切换**（2026-09-16 首次报告，09-17 复现一次）——**已结案（2026-09-21）**：触发信号「前端框架化时一并排查」已兑现——任务视图 v2 整体重写（Preact 状态驱动替代 v1 vanilla 的 DOM 状态同步矩阵），重写后两轮浏览器验收与实机使用均未复现。若再次报告则重新开案（v2 语境下定位成本远低于当年）。
- **flash 价目待校准**（2026-09-21，M10）：PROVIDERS 里 deepseek-flash 暂按 chat 档占位，账单成本略高估——有官方价目时修正（028 注记）。
- **deepseek-flash DSML 泄漏致收官丢失**（2026-09-23 发现，**已结案 2026-09-24**）：模型把内部函数调用格式（`<｜｜DSML｜｜ calls>...`）裸文本吐进 content，tool_calls 为空——调用意图丢失（S6c 实机验收：计划全终态但 finish_plan 未执行）。**修法（方案 a）**：loop.py 泄漏守卫 `_merge_with_leak_guard`——merge 后「无 tool_calls 且 content 含全角标记」判定为泄漏；泄漏消息不入底片、提示注入投影重试（上限 2 次，独立计数不吃 rounds 预算——格式故障不扣行动额度）；超限按普通回答诚实降级。工具循环与 max_rounds 收尾段两处共用；spawn 子 agent 走同一 run_turn 自动覆盖。方案 b（收尾检测「计划全终态未收官」给提示）仍在档，触发信号=收官丢失再现。
- **图谱抽取三个已知边界**（2026-09-23，S7a 实机验收，[035](decisions/035-s7a-knowledge-graph.md)）：①`.md` 文件名被抽成实体（「Agent.md」）——抽取提示词可加「文件名不是实体」规则；②抽取建模口径随机（同一篇笔记直连边 vs 桥接概念，语义等价——LLM 抽取固有特性）；③单句定义型笔记天然孤岛（结构性：产品闭环=补笔记→sync_graph 重抽→连通，实机验收已演示此闭环自愈）。触发信号=图谱质量迭代轮（S7b 可视化会让人眼更容易发现脏数据）。

## 待讨论（产品方向，未定档）

> 来自日常使用与外部评审的方向性议题，尚未展开设计评审。记录在此避免遗忘，临期讨论时补草案。
> 清单状态：2026-09-18 方向定稿会出清四条（见 [021](decisions/021-direction-decisions.md)）；2026-09-19 混合检索数据裁定关闭（见 [022](decisions/022-hybrid-retrieval-eval.md)）。2026-09-21 新增多进程演进议题（AgentTeams 调研触发）。2026-09-22 S6 优先级议题当日提出当日拍板落地（B 先行，冒烟套件 dd9dc31）。新议题随使用生长。

- **多进程演进**（2026-09-21 提出，源自 [AgentTeams](https://github.com/agentscope-ai/AgentTeams) 调研 + 架构问答；同日概念修正：三分法拆开「多进程」直觉）：判据一句话——**单进程对应「人在场的一次对话」**；场景变成「人走开了任务还在跑」「任务们互不干扰」「没人在场也要跑」才离开单进程。概念三分（防误开药方）：**①并发**（多任务同时在飞）线程/异步即可——agent 负载 IO-bound，GIL 不碍事，Web worker 线程已是雏形；**②隔离**（崩溃域/文件系统视图互不干扰）只有进程能给；**③独立生命周期**（cron 拉起/IM 随到随答）需要常驻进程。关键修正：**「长任务期间继续聊」是并发需求不是进程需求**——多 session 并发（第二个 Session 对象+第二把锁，session 层改动）即可解，别为它上进程。三径按概念映射排期：**A. headless 任务进程**（③，M9/S8 自然发生，零改造，「不 daemon 化」原则 S8 本有重审条款）→ **B. spawn 跨进程+worktree 隔离**（②，S6 正题：并行改文件不互踩、崩溃不连坐；spawn 返回值变 IPC）→ **C. Run Store 外置**（①的彻底解锁，SQLite/Redis，多 Run 并行闸门，动它才解单锁）。已有伏笔（非巧合是课件路线）：Run 事件流 SSE 协议天然跨进程、Session JSON 可快照、MCP stdio 已是子进程先例、Agent frozen 可序列化；硬约束：session 共享可变对象（单锁口径）、工具闭包抓对象本体（列表身份陷阱）、缓存/Run Store 内存态。附带调研注记：AgentTeams（Manager-Workers 容器编排）验证了 S6 方向，其最小形状启示=「plan 步骤驱动 spawn + 产物走共享文件 + 子过程折叠可见」——隔离的是主 agent 上下文，不是人的眼睛，两个「看见」要分开。排期信号：S6 开工设计评审时并入；S8 重审 daemon 化时引用。

- **前端配置界面**（2026-09-17 提出，编排部分已定稿见 [021](decisions/021-direction-decisions.md)）：①MCP 粘贴配置（替换手动编辑 JSON）②Skill 配置。共享同一设计问题——配置从「写文件」升级到「填表单」，前端框架化已裁定（Preact+Vite，FW 站）扫清地基障碍；③编排已定稿为「文本编辑 + mermaid 阅读」（拖拽进否决档案）。原依赖已齐：Agent 对象 S5a 已定型（skill 的配置对象有了）、FW 站已跑通三入口（任务视图/记忆面板）——两件剩余的其实只是排期，随时可开工；建议排期信号=「手动编辑 mcp_servers.json 出现真实摩擦」或 S6 技能包立项时一并做。

- **S6 优先级：多 agent vs 先补验证闭环**（2026-09-22 提出，评审修复轮触发）——**已拍板 B 先行并当日落地（2026-09-22）**：方向分歧在于 **A. S6 先做多 agent 隔离**（spawn 跨进程化、worktree 隔离、并行改文件不互踩，技术纵深深但实机使用尚未踩中「并行改文件」场景）vs **B. 先补验证闭环**（端到端验收仍是手工跑，评审修复轮 8 项硬伤有 5 项是集成路径 bug——事件名契约断裂/信封未解包/plan 泄漏/广播竞争/语义缓存串味，说明测试网漏的是「集成路径」而非「单元正确性」）。拍板 B：tests/test_smoke.py 四条核心路径冒烟回归（事件契约/plan 生命周期/spawn 分派+噪声隔离/路由降级），HTTP SSE 端到端自动验证——既是验证闭环，又是 S6 的安全网（spawn 内部实现从进程内→跨进程时，外部行为不变由冒烟套件自动把关）。S6 安全网就位，可开工。
