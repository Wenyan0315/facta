# Personal Agent 架构图

> 版本：v0.30（2026-09-13）｜随着里程碑推进持续迭代此文档
> 更新规则：架构有变更（新增层/模块/决策）时，同步更新本文件并提升版本号

## 设计原则

1. **接口与实现分离** —— `LLM` 是接口，mock/DeepSeek/本地模型随便换，上层不动
2. **分层解耦** —— 每层只做自己那件事，任何一层都能单独替换
3. **从简到真** —— 每个模块先手写"教学版"跑通原理，再换"工业版"（词袋→真embedding，mock→DeepSeek）
4. **渐进演化** —— 阶段一"学习助手"的地基，正好是阶段二"coding agent"的积木
5. **活 spec（方向提前定，细节临期定）** —— 路线图只锁方向（一行一里程碑）；详细方案在开工时才写（最后责任时刻决策）；实现后必回写决策记录。spec 与代码同生命周期，不做预言式大设计——MCP 三次顺延、M6.3 因 M6.2 翻车而丰富，皆是实证

## 一、系统总体架构（目标全貌）

```
┌──────────────────────────────────────────────────────────────┐
│                        用户交互层                              │
│          CLI（现在）→ Web UI / API（以后可选）                  │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                     入口  __main__.py                         │
│              组装各层依赖，启动 agent（依赖注入）                │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│  ★ core/  Agent 主循环（大脑）                                 │
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
| core 主循环 | ✅ ReAct 雏形 + M5.5 Agentic RAG：决策→执行→观察→再决策（5轮保险丝）；检索权已移交模型，主循环不再直连 kb | 并行工具调用 / 更复杂的规划策略 |
| LLM 接入 | ✅ OpenAI兼容统一类+配置表(deepseek/siliconflow) + 进程内网关(M7.5：记账/重试超时/精确+语义缓存/熔断三态/降级链+优雅兜底) | 更多供应商 + 多模型路由 |
| knowledge | ✅ Embedder接口+词袋/BGE双实现 + loader(数据外置) + VectorStore接口+双实现(M7：InMemory教学版/Chroma工业版落盘) + 增量同步(内容指纹差集) | 知识图谱 |
| memory | ✅ 会话内记忆 + 跨会话 JSON 持久化（M6.1）+ 摘要压缩（M6.2）+ 温层检索 search_history/read_history（M6.3）+ Session 状态整体持久化（压缩缓存随底片落盘，重启不再重压）+ 记忆固化 data/learned（M6.4：萃取→审查→硬校验→落盘） | 多会话隔离（另排期）、用户级记忆仓库外位置 |
| tools | ✅ Tool+ToolRegistry+6内置工具(时间/清单/读/写/检索/检索+摘要)；write_note 安全栅栏+查重闸门；search_notes=Agentic RAG 入口；search_and_summarize=复合工具(内部调LLM，Sub-agent原型) | 更多工具 + MCP + skills |
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
| MCP | 外部工具动态发现（三次顺延的集结号，coding agent 最直接积木） | 协议、动态工具 |
| streaming | 流式输出（首字延迟 / tool_calls 分片重组 / 用户取消）｜独立交互层 | 增量协议 |
| 阶段二 | coding agent：skill 系统 + 安全 + 工具访问 + 多 agent 协作 | 实战整合 |
| 〔支线·可跳〕 M8 | 知识图谱：实体关系结构化可视化 | 结构化知识 |
| 〔支线·可跳〕 M9 | 论文推送：cron 触发 + headless 任务 + arXiv 接入 + 语义过滤 | 外部API、无头任务、信息流过滤 |
| 〔另排期〕 多会话管理 | 会话隔离 + /new 一等公民 | 状态管理 |

## 四点五、企业级考量（贯穿性约定，2026-09-05 起）

终点是企业级 agent 开发，所以**不只是 M7.5 一个节点，而是贯穿每个里程碑的持续视角**：

- **每个里程碑收尾时，补一节"企业视角"**：这个模块进了大厂会面对什么（规模/并发/成本/合规）——已讲过的例子：错误分类与容错四件套（重试退避/超时/熔断/降级链）、模型质量分级降级、语义缓存
- **教学版 vs 工业版的差距永远点名**：M7 前 KnowledgeBase 每次 add_document 全量重建向量（O(N) 重算），M7 已用 Chroma 增量索引解决——现在的教学版 = InMemory 存取 + 词袋向量（离线零成本），工业版 = Chroma 落盘 + BGE-M3（增量 + 语义）；换件不换衣服是这套打法的验收标准
- **LLM 应用特有考点**：成本控制（token 计费随轮数增长）、可观测性（没有度量就没有熔断）、评测流水线（evals 已是雏形）
- **安全**：M9 前安排一讲——提示词注入（工具越权）、key 最小权限、审计日志

## 五、关键架构决策记录

- **LLM 接入（M4 重构）**：不做"每家供应商一个类"，而是 `OpenAICompatibleLLM`（一个类）+ `PROVIDERS` 配置表；环境变量约定 `{PREFIX}_API_KEY / {PREFIX}_BASE_URL / {PREFIX}_MODEL`；加供应商 = 配置表加一行。命令行 `python -m agent <provider>` 切换。
- **Embedding 接入（M4 后半，2026-09-05）**：与 LLM 层同构——`Embedder` 接口 + 双实现（`BagOfWordsEmbedder` 教学版 / `SiliconFlowEmbedder` BGE-M3）+ `get_embedder()` 工厂；`KnowledgeBase(embedder)` 依赖注入。**阈值跟着实现走**：`default_min_score` 是 Embedder 的类属性（词袋 0.35 / BGE 0.55，量纲不同不可混用），调用方不再硬编码。选型依据：业界 RAG 直接上 embedding（无 TF-IDF 过渡），BGE-M3 在硅基流动免费。
- **BGE 阈值校准法**：先跑探针看"相关/垃圾"问题的分数分布（相关 0.645~0.784，垃圾 ≤0.491），阈值卡在两者之间的沟里（0.55），两侧留余量——数据定参，不拍脑袋。
- **Embedding 层对称重构（2026-09-05 Q1）**：`SiliconFlowEmbedder`（供应商焊死在类名）→ `OpenAICompatibleEmbedder`（通用类）+ `EMBED_PROVIDERS` 配置表，与 llm.py 完全对称。要点：①`min_score` 进配置表跟着**模型**走（换模型必须重校准）②embedding 的模型覆盖环境变量用 `{PREFIX}_EMBED_MODEL`，与聊天的 `{PREFIX}_MODEL` 分离——同供应商的聊天/embedding 是两个独立开关，共用会互相覆盖 ③工厂键名统一为供应商名（`get_embedder("siliconflow")`）。回归验证：BGE 指标与重构前逐项一致。
- **工具调用闭环（2026-09-05 M5）**：模型只决策不执行——返回 tool_calls（name+arguments JSON），程序执行后以 role="tool" 消息回填再问。核心组件：tools/registry.py（Tool四要素：name/description/parameters给模型，func给程序；ToolRegistry：schemas()生成菜单、execute()执行且错误也返回字符串→模型可自我纠正）、tools/builtin.py（get_current_time/list_notes）、agent_loop 工具循环（for...else 检测不收敛，_MAX_TOOL_ROUNDS=5 保险丝）。入史策略：工具轮完整入史（tool消息必须与tool_calls配对，否则API报400；且模型记住工具结果有长期价值）；纯聊天轮保持历史干净（RAG资料不滞留）。接口演进两手法：Message只加带默认值的可选字段、generate只加带默认值的可选参数——老代码零改动。
- **数据与代码分离（2026-09-05 Q2）**：笔记从 `SAMPLE_NOTES`（焊在代码里）迁到 `data/notes/*.md`（每条一个文件，中文名语义化），`knowledge/loader.py::load_notes()` 负责加载（sorted 保序 + 显式 utf-8 + 快速失败）。__main__ 与 evals 共用同一 loader → 评估与线上永远同一份语料。判定标准：会被 import 的是代码进 src/，只被读取的是数据进根目录。当前全量加载（9 条 <10KB，"内存不是瓶颈时简单就是性能"）；M7 库变大再长出索引+按需读取。加笔记=丢 md 文件，零改码。
- **key 安全**：key 只存 `.env`（已被 .gitignore 排除），代码只读环境变量，绝不硬编码。
- **网关**：不做独立部署网关服务；`get_llm()` 工厂演进为进程内 Router（按任务路由模型、重试/限流/成本统计）
- **工具写入与知识库治理（2026-09-05 M5 尾声）**：write_note 工具（agent 第一次能改文件系统）带双重防线——①安全栅栏：路径穿越(resolve+is_relative_to)/.md后缀/禁子目录/覆盖保护；②查重闸门：写入前 kb.search(content, min_score=0.85)，高度重复拒绝并提示先读后合并（知识库治理第1层=写入时把关；第2层维护工具、第3层元数据血缘留 M6/M8）。依赖注入新姿势：kb 通过闭包注入 write_note（register_builtin(registry, kb)）——工具层开始依赖知识层。溯源缺口：KB 只存文本块不记"块来自哪个文件"，查重只能返回相似片段而非文件名，M8 图谱补元数据。自我进化闭环已端到端验证：agent 读旧笔记→综合→write_note 新笔记→重启后可检索。已知边界：0.85 只拦"几乎照抄"级重复，"同主题不同措辞"的语义重复需 LLM-as-judge（M6 候选）。
- **Agentic RAG（2026-09-06 M5.5）**：kb.search 包成 search_notes 工具，检索权从程序移交模型——主循环删掉自动检索段（121→87 行，复杂度塌缩：代码更少能力更强），run_chat 不再依赖 kb，知识层完全经工具层介入对话。连带修订：①三级信息政策从"每轮拼进消息"升级进 SYSTEM_PROMPT（提示词即政策，一次设定全程生效）②M5 意图守卫（len>=3 粗筛）随之废除——短输入误中由模型自主决策天然解决，粗筛本就是 Agentic RAG 落地前的临时占位 ③schema description 承担新职责：教模型何时该查、查询词要提炼、指代性话语要改写。search_and_summarize 复合工具（检索+内部再调 LLM 摘要）= Sub-agent 原型；内部调用绝不传 tools，防"工具调工具"无限递归。遗留：write_note 查重 0.85 只拦照抄级，语义查重留 M6 LLM-as-judge。
- **MCP**：M5 工具层做成 ToolRegistry（内置 Python 工具 + MCP 客户端动态发现），MCP 顺延待排期（M5.5 已被 Agentic RAG 占用）
- **记忆持久化（2026-09-06 M6.1）**：memory/store.py（asdict→JSON 存，Message(**d) 读回，往返无损已验证）。窄 except 原则：只捕 FileNotFoundError（「文件不存在=新会话」是正常场景，返回 [] 静默降级）；文件损坏(JSONDecodeError)必须大声崩——静默吞掉会让历史蒸发而无感知。接线架构（控制反转再应用）：**落盘策略归组装层**——__main__ 启动时 load 注入、quit 退出时 save；run_chat 只管内存（messages 进参 + return 归还，不碰文件 IO），换存储介质主循环零改动。关键细节：条件种人设用 `if not messages` 而非 `is None`——load 首跑返回 []，空列表也要种 system prompt，否则第一次运行的 agent 无人设。已知 tradeoff（简单优先，接受不处理）：①system prompt 入盘，改 SYSTEM_PROMPT 后旧会话仍用旧人设 ②Ctrl+C 不保存（input() 直接抛异常）③全量记忆零筛选——「记住暗号」不是智能行为是全量录像，token 随轮数线性增长，正是 M6.2 摘要压缩的动机。session.json 是运行时数据（非资产），进 .gitignore——与 data/notes（语料资产，进库）的判定同「数据与代码分离」条款。
- **摘要压缩（2026-09-06 M6.2）**：三件套架构——底片（messages，append-only，落盘）/ 投影（payload，每轮 llm.generate 前由 build_payload 现切，纯函数）/ 缓存（summary+summarized_upto 覆盖进度，触发式重算平时复用）。agent_loop 工具轮双写（底片+投影同步 append），最终回答只入底片。**覆盖不变量（验收翻车换来的铁律）**：每条消息必须可见——要么已被摘要覆盖、要么原文在 payload；第一版有死区（窗口外未达触发阈值的积压两头不沾），暗号恰好落进去 → 模型看不见原文 → 当场编了个假暗号（幻觉标准机制：模型看不见时不会说看不见，会编）。修复一行：`start=min(window_start, summarized_upto)`；副作用是未触发摘要前 payload=全量直发。**窗口边界规则**：左边界必须落在 user 消息上——裸切会把 role=tool 消息与它的 tool_calls 爹切分家，孤儿 tool 消息被 API 400 拒收（mock 不校验，本地全绿真模型间歇炸）。**触发策略**：早触发（窗口6+余量6，消息条数为粗代理，50K 贴文只算1条的局限已知），拒绝「快满才压」（自指挤压+悬崖风险）。滚动摘要=旧摘要+新积压喂给内部 LLM（tools=None 防递归，search_and_summarize 同款）；摘要器 prompt 点名必须保留暗号/数字/承诺等关键事实。连带修复：①Ctrl+C/Ctrl+D 捕获后 trim_incomplete_round（掐掉孤儿工具轮）再保存，M6.1 tradeoff② 就此关闭 ②SYSTEM_PROMPT 增加记忆自我认知（历史自动保存、摘要等同亲历记忆），修掉「我技术上做不到持久记忆」的过时自画像 ③摘要完成即打印（可观测性，保真度当场可审）。验收教训：合成测试验证了「边界合法」，用户验收验证了「不变量成立」——不变量必须显式写下来才能被测到。
- **架构五问裁定（2026-09-06 M6.2 后评审）**：①用户取消——Ctrl+C 保存已随 M6.2 落地；完整取消（流式中打断）是 streaming 的副产品；长工具的协作式取消留阶段二 ②意图识别——无需建设：M5 意图守卫的生死已证明 LLM+工具即意图识别器；显式意图模块只在多模型路由/guardrails 场景复活（M7.5/阶段二）③状态管理——文件即状态是本规模正解（无状态计算+状态外置=重启安全）；状态机框架等阶段二长任务 checkpointing ④streaming——排期 M6.4；难点是 tool_calls 分片重组；接口用 generate() 可选参数演进 ⑤数据库——JSON 现在正确；SQLite 是 M6.3 的自然台阶（触发信号：跨会话搜索要遍历 N 个文件、防写坏）；MySQL/PG=多用户服务端、Redis=热数据层非归档层，选型跟着访问模式走。
- **M9 重定义（2026-09-06 架构讨论）**：能力 vs 触发之分——MCP/skill 提供「被调用才执行」的能力，但死进程不会自己醒，定时触发只能外置：归系统 cron/launchd（唤醒 agent，不 daemon 化、不自建调度器）。M9 的学习原子重排：headless 任务模式（run_chat 之外的第二入口，无人对话跑完即退，阶段二 coding agent 前身）＞ arXiv 外部 API 接入（M7.5 容错四件套练习场）＞ 推送通道。杀手锏：知识库语义过滤——新论文与笔记库算相似度、过阈值才推（RAG 反向应用：语料当过滤器给信息流打分，复用 BGE-M3）；去重状态与过滤逻辑是 agent 侧资产，外部工具替不了。arXiv 手写接入优先（from-scratch），MCP 替换留作届时选项。
- **search_history 与三温度层（2026-09-07 M6.3a）**：温层开门——底片一直在内存（append-only），模型看不见只因投影切掉了。search_history 闭包注入 history 列表（第三个闭包依赖；__main__ 装载顺序：history 必须先于 register_builtin）。**关键词子串匹配而非语义检索**：历史每轮生长，语义检索要每轮重 embedding（贵且慢）；智能活（提炼关键词）给模型，蛮力活（扫列表）给工具。角色过滤只搜 user/assistant（system=人设、tool=可重生的检索产物，非"对话原话"）；返回带 #编号+历史总数分母（位置核验材料）。已知局限（五轮验收逼出）：①词组查询按整短语子串匹配，模型爱组词组（搜"PHP 工具"漏掉只含"PHP"的 #1）→ 待分词匹配；②"第一句话"类问题是**位置查询**（ORDER BY id LIMIT 1），关键词是**内容查询**（LIKE），方向相反，模型被迫三跳间接推理（摘要线索→猜词→检索→取最小编号），最后一跳位置核验常失败 → read_history 位置读取工具排 M6.3b（与知识库 list/read/search 三件套对称）。**read_history 已于次日落地并验收通过（一跳直达，start=1 即第一句原话）**：与 search_history 正交互补（位置查询 OFFSET/LIMIT vs 内容查询 LIKE，"知道什么缺什么"决定走哪条），编号体系跨工具一致是硬契约（同一数据的多个视图必须共享坐标系）；组合拳=search 定位（命中 #N）→ read 取景（start=N-1, count=3 看上下文）；tool 结果截 300 字防灌爆（user/assistant 逐字全量——逐字引用是使命）。
- **列表身份陷阱**：闭包捕获列表**对象**而非名字。run_chat 原写 `messages = [system]` 是 rebind（换对象），闭包里还是旧空列表 → 首跑会话 search_history 静默失明不报错。修法两段式：None→新建；空→**原地 append** 种人设。rebind vs 原地变异，一切共享可变状态的代码必考。
- **验收五轮 saga（洋葱逐层剥）**：1.0 编假暗号（数据不可见→幻觉）→ 2.0 照抄问题原句当关键词+单条命中即宣布答案 → 3.0 污染叙事劫持（元问题×6+历轮律师辩词被滚动摘要吸收，"检索偶发不命中需兜底"毒条目劝退工具调用）→ 4.0 术语漏网（用户说"这轮对话"不在 SYSTEM_PROMPT 枚举表，按语感读成"当前片段"）→ 5.0 位置核验缺失（自纠角色后搜到 #8/19 即当"第一句"，未验证是否最早）。每轮失败浅一层，五个修复各自被后续轮次确认生效。
- **元教训四条**：①**测试污染**——持久状态的系统验收必须 fixture 重置，不能在上一轮残骸上加固提示词（等于在洒油的赛道调赛车）；②**进程复活文件**——运行时真相在内存，session.json 只是退出快照；重置状态=先杀进程再删文件，顺序反了文件被内存复活；前置检查（ps）有 TOCTOU 时间差不作数，**后置断言**（启动横幅无"已恢复"）才是门禁——断言实际行为，别断言意图；③**自证预言**——摘要里的元叙事（"用户在测试边界"）真的改变了模型行为：记忆不只被动存储，还会 steering 后续行为；④**停止规则**——打地鼠有终点，知道何时带着已知局限收官与修好一样是工程判断。
- **真实环境污染的分层防御**（用户问"真实环境也这样怎么办"）：层0 底片不可摧毁（session.json 永远全量，最坏可恢复——给 #0 打补丁即实战恢复）；层1 摘要器纪律（本次落地）；层2 会话隔离（Part B 的 /new，把"新开一个聊天"做成一等公民，污染爆炸半径=单会话）；层3 诚实残差（prompt 纪律降概率不根除，设计目标是优雅降级+逃生舱存在+档案可恢复）。
- **摘要纪律与措辞**：_SUMMARY_PROMPT 增"档案员不是评论员"——禁止写入对自身能力的评价、对用户意图的猜测、策略性元叙事（自证预言的源头防线）。摘要消息措辞"更早对话"→"本会话较早内容"（同会话压缩区≠另一个会话，措辞即语义）；SYSTEM_PROMPT 声明跨重启恢复=同一持续会话。
- **防递归检查点精确化**：风险不在"工具边界"，在**每次 llm.generate 调用现场**——拿着菜单的模型才可能点菜。search_and_summarize 是全项目唯一内部调用现场（tools=None 铁律）；kb.search 里的 embedder（BGE-M3）是神经网络但无菜单无意志，非风险。判据："是不是模型"不重要，"有没有菜单"才重要。
- **Skill**：本质是 prompt 模板 + 资源包，后续做 `skills/` 目录按需加载，不提前设计
- **多智能体**：阶段二做（Orchestrator 编排 + 子 agent 实例化组合），依赖 M5 扎实后才做
- **校验 guardrails**：不单独分层，横切在 core 循环和工具层——M4 结构化输出校验+重试；M5 工具参数校验+自我纠错（2026-09-12 补全景：校验三维分工=语法层 json.loads / 结构层 execute 按 JSON Schema 最小子集校验 required+基础类型（此前 schema 只用于生成菜单、执行时不 enforcement，记忆库硬约束「工具必须 JSON Schema 参数校验」只兑现一半）/ 语义层工具函数自身抛异常——坏参数在任一层都以错误字符串回给模型，自纠反馈环三层无差别）
- **评估 evals**：独立 `evals/` 目录不进运行链路；检索用 precision@k/recall@k/MRR；M4 后加 LLM-as-judge
- **会话状态持久化（2026-09-08，修复 P0-1）**：M6.1 只落盘底片 messages，M6.2 的 summary/summarized_upto 是 run_chat 局部变量，重启即清零——滚动摘要退化成「启动首轮一次性全量大压缩」（档案越长越接近悬崖式压缩，且暗号跨压缩存活不可复现）。修法：抽 `Session` dataclass（messages + summary + summarized_upto）整体落盘，store 出 `save_session/load_session`（version 预留演进 + 旧列表格式自动迁移 + 游标钳到 [1,len] 防越界）；run_chat 改为注入 Session 原地变异、归还 Session——落盘策略仍归 __main__（控制反转不打折）。连带收口 __main__ 接线（此前半段还是旧 load_messages/save_messages，直接 NameError）。
- **回归测试落地（2026-09-08）**：两份独立评审共同点名「1244 行源码 0 单测、不变量写进散文靠人肉验收」。补 `ScriptedLLM`（按脚本吐 tool_calls，工具链路首次可离线验证；每次 generate 记录收到的 messages 供断言）+ `pytest` dev 依赖 + `tests/` 13 用例，把验收五轮 saga 踩过的坑固化成断言：覆盖不变量（死区防御）、窗口边界（左边界落 user/孤儿 tool）、孤儿清理（trim_incomplete_round）、触发缓存（阈值前零调用）、跨工具编号一致性（search_history/read_history 共享 #坐标系）。铁律：不变量写成断言，不写成注释。此后每个里程碑必带测试。
- **路线重排（2026-09-08，采纳两份评审）**：①MCP 提前——三次顺延的集结号，作为通往 coding agent 最直接的积木提到 M7.5 之后 ②M8 知识图谱 / M9 论文推送降为「兴趣支线，可跳」（对 coding agent 几乎零复用；headless 任务模式挪阶段二复用，不浪费既有设计）③streaming 从 M6 摘出、独立成交互层里程碑（增量协议+UX，与记忆无关）④P1 重构（Message→types.py、ToolContext 收敛、路径注入）排队 M7 开工前置。多会话管理（原 M6.3b）属「会话隔离+状态管理」，另排期，不塞回记忆层。
- **P1-2/P1-3 ToolContext 与路径收口（2026-09-10，M7 前置重构收官）**：治两病——①依赖发散：`register_builtin(registry, kb, llm, history)` 每加工具依赖就膨胀（评审点名「接 MCP 时必炸」），收敛为 `register_builtin(registry, ctx)` 签名永固；②路径写死：`"data/notes"` 在 builtin.py 模块常量与 loader.py 默认参数各藏一份（两个真值源迟早打架），且 load_notes 的默认参数是藏在签名里的第三个真值源。修法三件：`tools/context.py` 新建 ToolContext dataclass（kb/llm/history 可 None 触发条件注册，notes_dir 必填无默认——默认值即真值源）；`__main__` 成为路径唯一真值源（NOTES_DIR 定义一次，经 ctx 流下去；MEMORY_PATH 无工具用，不进 ctx 留在 __main__）；loader 默认参数拔除、evals/demo 离线脚本自带局部路径。**准入标准**（防 ctx 变垃圾抽屉）：工具运行时需要 + 工具自己无权决定的东西才进 ctx。**依赖方向**：ToolContext 放 tools 层而非 core/types.py——它要 import KnowledgeBase（knowledge 层），放 core 会让最底层反向认识上层（P1-1 刚矫正过的病）。新增回归测试 `test_history_is_live_reference`：把 List identity trap 固化成断言（注册后 append 必须可见，手滑 .copy() 当场爆炸）。验证：14 用例全绿 + mock 冒烟 8 工具清单与重构前逐项一致 + 21 条历史往返无损。
- **P1-3 补强：路径真值源上收到 agent/paths.py（2026-09-10，code review 修复轮）**：P1 收官后立即跑 TRAE-code-review 审两个重构 commit，双验证 agent 交叉确认 5 项，其中 **Critical 一项**：evals 以 NameError 状态被提交——`load_notes(NOTES_DIR)` 进了 commit 而 `NOTES_DIR` 常量定义被 IDE 缓冲区回写吞掉；pytest 全绿是盲区（evals 不在测试链路，纯 import 又不执行函数体，必须调 build_kb 才炸，人工复现确认）。修复连带决策：①NOTES_DIR 真值源从 `__main__` 再上收到 `agent/paths.py`——evals/demo/test 原各自再带一份字面量副本，与「__main__ 与 evals 共用同一 loader → 评估与线上永远同一份语料」的不变量冲突（改一处漏一处即静默评估另一份语料）；共享规则=跨模块路径住 paths.py，单消费者路径（MEMORY_PATH）留消费地不提前搬家 ②pytest `pythonpath` 加 `"."`（evals 在仓库根，原配置测不到它）③新增 `test_evals_build_kb_runs` 接线回归（词袋离线搭库 + min_score=0.0 断言语料非空），把「evals 可运行」钉成断言防同类断链 ④demo() 函数级 import 上移、types.py 补行尾换行（Trivial）。**元教训**：验证必须覆盖被改模块的运行路径，不能只跑 pytest——不在测试链路上的入口（evals、demo）改完必须手动执行一次；IDE 缓冲区与磁盘编辑打架时，提交前须 `git diff` 核对实际入库内容（本次事故根因）。已知盲区（记录不修）：`test_history_is_live_reference` 防工具层内部 .copy()，防不住 `__main__` 调用点的 .copy()——调用点保护靠 mock 冒烟，但冒烟不验「新消息可搜」，如需钉死须加组装层测试。
- **M7 向量存取层（2026-09-11）**：把「向量住哪」从 KnowledgeBase 拆成 `VectorStore` 接口（upsert/delete/query/get_all/count/clear）+ 双实现——InMemoryVectorStore（教学版，字典+暴力余弦，测试/词袋用，离线零依赖）/ ChromaVectorStore（工业版，PersistentClient 落盘 + 按 id 增删 + 自带索引）。KnowledgeBase 从「自算余弦」变「委托 store.query」，**search 接口签名零变化**——工具层/evals 一行未动（换件不换衣服）。chromadb 装 pyproject 的 `rag` 可选组（教学路径不需要它），实现内延迟导入（与 OpenAICompatibleEmbedder 同招）。**分数契约**：接口规定 query 返回余弦相似度（越大越像）；Chroma 的 cosine 空间返回距离（=1-相似度），换算封装在 ChromaVectorStore 内——不显式配 `hnsw:space=cosine` 就会拿到 L2 距离（量纲反向），BGE 0.55 阈值全部作废，此坑由 `test_inmemory_vs_chroma_same_ranking`（双实现同排序）+ `test_chroma_score_conversion_and_persistence`（关库重开 + 近似 1.0）钉死。
- **M7 增量同步 sync_notes（2026-09-11）**：`sync.py::sync_notes(kb, notes_dir)` 取代「load_notes→逐篇 add_document」的启动路径（evals/demo 一并转投，评估与生产同一条索引路径）。**身份 = 内容指纹**（sha256 前 16 位，块 id = `指纹:块序`，metadata={source,hashi}）：改名免费（指纹不变→交集）、touch 免费（内容没变→交集）、真改才花钱（旧指纹消失+新指纹出现→删+增）——被否决方案：id 用文件名（改名误判「删+增」全量重算，用户当场否决）；mtime 作判据（touch 误判「改」，且要求另存 manifest=第二个真值源，犯 P1 刚治的病；撤销条件：语料大到读全文件算哈希成为真实成本时，再评估 manifest+mtime 混合方案）。副产品：同内容多文件自动去重（共用指纹，upsert 幂等）。**词袋退化路径**：词袋向量维度=词表长度，增量不成立——`Embedder.supports_incremental` 能力标记（词袋 False→清库全量重建：先 fit 全语料建词表再 embed，漏 fit 则全零向量；BGE True→真增量），sync 据此分支。**混用陷阱**：词袋与 BGE 维度不同，绝不能共用同一 Chroma 集合（维度冲突直接炸）——干脆规定教学组合（词袋+InMemory）不碰 Chroma，`__main__` 按 provider 分流：假模型→词袋+内存（离线不花钱），真模型→BGE+Chroma 落盘。
- **删除安全阀（2026-09-11，测试逼出的设计修正）**：文件消失 ≠ 用户想删（目录误移动/挂载失败会清空全库、下回重建重烧 embedding）。双重护栏：①空目录由 scan_notes 直接抛错（第一道防线）②批量消失按「文件数」判且**修改不算**（改内容是合法替换）：消失篇数 ≥3 且占比 >20% 中止逼人确认。初版用「待删块数/总量」纯比例判据被测试现场打脸（单文件库改一篇=100%拦截、两文件库删一篇=50%拦截——比例阈值对小样本是噪声，必须带绝对下限，与「SQLite 迁移触发信号」的阈值设计同课）。
- **M7 验收（2026-09-11）**：tests 15→29（新增 test_vector_store 4 条 + test_sync 10 条），不变量全部钉成断言：首轮全增/二轮幂等白嫖（零 embed）/改·删·增三路/改名免费/删除安全阀+小删除对照/重启零重算（CountingEmbedder 计数 + Chroma 关库重开）/词袋退化全量重建（且 fit 未漏——搜 PHP 排第一）/空目录中止。真实验收：`python -m agent deepseek` 连跑两遍，第一遍「新增 14」（一次性 BGE 全量嵌入），第二遍「不变 14、零新增」——**重启零重算**用真实数据验证通过；mock 冒烟 8 工具清单不变、21 条历史往返无损。连带治理：load_notes（M7 前入口）所有消费方转 sync 后退役删除；chunk_size/overlap 参数目前定死 200/50（KBsync 未暴露配置，届时需要再加）。
- **M7 评审修复轮（2026-09-11）**：TRAE-code-review + 双验证 agent 审 ec10839，8 项全修（无 Critical）：①docstring 成本模型失实（「不变=不读文件」是错的——算哈希必须读全文，省的是 embed 不是读盘）②两新文件补行尾换行（types.py 同款复发，验证手段固化：提交前 `tail -c 1 | xxd`）③Chroma clear 补测试（原死路径零覆盖，1.5.9 下实测可用后钉进断言）④metadata.source 去重陈旧记入代码注释为已知边界 ⑤chromadb 下限提至实测版本 >=1.5 ⑥InMemory upsert 换 `zip(strict=True)`（静默截断对齐错位 vs Chroma 抛错，双实现行为对齐）⑦词袋路径 unchanged 归 0（账本自洽）⑧「先增后删」崩溃一致性窗口注释记录（刻意选先增后删：宁可短暂重复不要窗口期缺失）。被排除的误报一条：主审怀疑「批量改名误触安全阀」——vanished 有 `hash∈to_remove` 前置过滤，改名指纹不变进不了集合，双验证员一致推翻。元教训：同行评审的价值不在「找到 8 个问题」，在「主审自己的假设被证伪」——改名误报那条就是我拿着错误假设去找证据，被验证员按代码逻辑否决。
- **M7.5 网关四件套（2026-09-11/12）**：`get_llm()` 工厂落实为进程内网关（兑现 2026-09-05「网关不做独立服务」的既定决策），所有 `llm.generate` 调用点（主循环×2/压缩器/工具内子调用）零改动获得四件衣。分段落地：

  **a 记账与可观测**：`Message.usage` 可选字段（接口演进老规矩：带默认值不破坏老代码）；OpenAICompatibleLLM 解析 resp.usage；`core/telemetry.py` 的 `UsageLedger` 进程级账本（LLM+embedding 一本账）；PROVIDERS/EMBED_PROVIDERS 加价目行（示例价注明以官网为准）；退出时 `__main__` 打印账单。**记账位置在 wrapper 不在实现类**——假模型同样过闸（mock 模式 ¥0 也有账）、重试次数只有 wrapper 数得清、嵌入侧因无输出通道而在实现类内部记（两套记账模式并存的不对称已记录，语义档查询 embed 的记账也因此自动覆盖）。

  **b 重试与超时**：可重试异常判型用鸭子方式（getattr status_code——None/429/5xx 重试，4xx 立即抛），不 import openai 保假模型路径轻装；指数退避 0.5s→1s 默认重试 2 次；客户端 timeout 默认 30s（`{PREFIX}_TIMEOUT` 可覆盖）。记账联动：retries/failures 计数进账单。

  **c 缓存两档**：精确档（键=模型身份+完整输入哈希；命中返回 replace 副本；LRU 128 进程级不落盘）；语义档（独立装饰器 SemanticCacheLLM，评审 C 条——吃 embedder 依赖不塞进 RobustLLM；三个保守条件：仅 tools=None 防重放 tool_calls/能定位最后一条 user/相似度≥0.92）。**缓存隔离事故**：未命中路径一度把缓存本体返回给调用方（命中路径隔离了、miss 路径漏了），红测试被管道吞掉退出码后随 commit 推送（`pytest|tail` 退出码是 tail 的），修复为「入库即隔离，返回永远副本」。语义档诚实定位：个人聊天命中率天然低，战场是 FAQ 类高频相似查询。cosine_similarity 第三次搬迁——语义档（core 层）要用它，从 knowledge/vector_store.py 再搬到 `core/vector_math.py` 地基（规律：函数有跨层多个消费者时属于最低公共层）。

  **d 熔断与降级链（评审 F 契约定稿后实施）**：①`LLMUnavailableError` 定义在 llm.py（接口层语言，主循环不 import 网关）②agent_loop 把「摘要→投影→工具循环→收尾」整轮罩进捕获：模型全挂 → 掐半截工具轮 + 用户消息留底片 + 提示语不进历史 + 程序不崩（熔断保护的终点是体验不是崩溃）③`FallbackLLM` 降级链：候选逐个试、切换打印诚实声明、链耗尽才抛；捕获全部 Exception——401 切下一个（主候选没救≠备选没救）④熔断三态挂每个候选自己的 RobustLLM：连续失败≥阈值(默认3)→open 冷却(默认30s)快速失败→冷却期满 half_open 放行一次试探（成败定回路去留；冷启用 time.monotonic）⑤`GatewayConfig` dataclass 收敛参数（评审 B 条，防 ToolContext 病的网关版复发）⑥get_llm 升级为链组装器：主模型→有 key 的备用真模型（没 key 的备选不报错，不是主选）→mock 兜底（用户拍板，降级时打印声明）。测试教训两条：时间相关状态（冷却）必须假时钟（monkeypatch time.monotonic）驱动——cooldown=0 时 open 态时长也是 0，快速失败分支全程不可观测；keyword-only 传参防位置漂移（GatewayConfig 一度被塞进 ledger 参数位）。验收：57 用例全绿，mock（单候选）与 deepseek（真降级链）双链路冒烟正常。
- **时间戳注入投影（2026-09-12，真实使用经验逼出）**：用户按任务类型持续使用同一会话窗口、跨多天恢复——无时间锚点时模型拿上次对话的时间当「现在」，安静地算错一切相对时间（"更新数据"取到半个月前的日期，全程不报错；对照 coding agent B 每轮携带日期）。定位：时间戳属于「本轮视野」不属于「对话内容」→ **投影注入（payload.insert(1)），绝不入底片**——入底片会堆日期垃圾、被摘要吸收；投影每轮现切、随轮作废。格式「今天：YYYY-MM-DD（周X）HH:MM」——星期几与时分让"这周五""下午三点前"等相对说法可计算。位置固定第 2 条（system 之后、摘要/对话之前）；本轮工具循环共享同一时间戳（插一次稳定全程）。get_current_time 工具保留（秒级精度/未来时间点）。成本可见：时间戳 ~40 tokens/轮是恒定税，直接体现在退出账单——这项决策的性价比由账单持续审计。测试两条：格式断言（固定 datetime 注入）+「投影有戳、底片没戳」分离不变量。教训：模型没有「现在」观念，所有跨时间恢复的会话都必须显式喂日期——这是 AI 应用与普通程序最反直觉的差异之一。
- **三方评审修复轮（2026-09-12，external agent 第七轮建议）**：评审实跑 2600 行源码+两份历史评审，确认 65 测试全绿/工作区干净，提 7 条新建议。裁定与落地：①**MCP 演示服务器写文件无沙箱**（任意路径覆盖写入零防护，而 write_note 有四道栅栏）——提升为 MCP-b 学习原子，裁定「栅栏长在**服务器侧**」：能力提供者自守边界，客户端只做菜单层尽力而为；②**语义缓存包住内部调用**（压缩器/search_and_summarize 穿过语义档：白付 embed + 串味路径）——修法拆链：`run_chat` 加可选 `summary_llm`（内部压缩走内部链），ToolContext.llm 改收内部链，语义档只服务用户聊天流量；评审的「顺序反一下」修正为次要收益（主循环精确命中本就罕见）；③evals BGE 全量重 embed——待 MCP 收官时 evals 走 Chroma eval 目录复用增量同步；④三处版本号漂移（__main__ 0.9.0/pyproject 0.1.0/架构文档）——统一为 0.9.0 并注释互指 + 修 `echo repeat` docstring 错例；⑤已知边界两条：replace 浅拷贝 tool_calls 共享引用（注释点名，靠「消息不可变」约定成立）+ McpClient 启动探活（100ms 沉降期防 poll 竞态，秒级报退出码而非 30s 干等）与超时语义注释；⑥**CI 落地**——`.github/workflows/ci.yml`（push/PR 触发，pip install -e ".[dev,rag]" + pipefail pytest），「红测试被管道吞掉」血案的自动化防线；⑦架构文档拆分（ADR 惯例，docs/decisions/ 按里程碑 append-only）——记入 backlog，触发条件=architecture.md 再长大一轮时执行。
- **记忆固化 M6.4 方向（2026-09-12 用户提出，MCP 收官后开工）**：诊断——对话里长出来的「值得跨会话记住的东西」目前只有三条不可靠出路：底片淹没（子串检索）、滚动摘要（会话内、压缩稀释）、write_note（被动靠模型自觉）。补的是记忆层与知识层之间的真空区。**已拍板的三条方向**：①定位 MCP 之后（与「另排期」的多会话管理天然搭档——长时记忆是跨会话资产，多会话隔离后更安全）②触发=退出复盘为主 + 长会话中途兜底（同滚动摘要的阈值机制）③形态=独立目录 `data/learned/{类别}.md`，与 notes 分源分治理，复用 RAG 链路可检索；分类骨架对齐项目记忆库（偏好事实/决定理由/约束教训/承诺待办）。**分类 = 作用域 × 类别 二维矩阵（2026-09-12 用户细化）**：作用域（用户级跨项目 vs 项目级本 repo）× 类别（偏好/决定/约束/待办/代码规范/业务信息…）；两者的原型就是用户自己的 Trae 记忆结构（user_profile.md=用户级、project_memory.md=项目级）。**红线联动**：作用域决定物理位置——用户级记忆住**仓库外**（~/.personal-agent/ 之类，含个人痕迹不能进可能公开的 repo 物件，触发硬约束），项目级住 data/learned/（进 git 须按既有红线过滤措辞）。**类别生长规则**：初始 2-3 个稳定且区分度高的桶+「其他」，某桶条数超 N 才裂变子桶——分类越多、模型分错桶概率越大；结构跟着使用频率走，不预测未来（同 SQLite 迁移/分词匹配的触发信号哲学）。**细节临期定时必须防的三坑**：幻觉污染（只提炼对话说过的、不补全不推断——「档案员不是评论员」的加强版）、矛盾重复（写前检索比对 + 条目带时间戳与出处）、膨胀（「值不值得记」过滤 + 类别上限）。
- **LLM-as-judge 评测落地（2026-09-12，「要不要运行时 critic」讨论的度量先行）**：`evals/answer_eval.py` 离线测回答质量：每题 BGE 检索（与生产同链路同闸门）→ deepseek 有依据生成（被评=线上主模型）→ **siliconflow 当裁判**（不同供应商防自我偏好偏差）→ 忠实度打分 1-5（忠于资料/不编造/答非所问；资料不足时「承认不足」得高分——与三级信息政策同源）。成本 ¥0.0112 跑完 13 题（账本显示 26 次调用/4.8K tokens/27 次 embed——评测成本本身可度量）。**基线：13/13 合格、0% 错误率、平均 5.0**——数据支持「暂不上运行时 critic」的判断。诚实局限（记录）：题库偏简单（检索命中即答对）、不测工具决策链（Agentic RAG 的查不查/查什么）、无答案题只有 2 道。**评测当场抓出数据集过期标注**：题库「AI研发的学习路径」原标 expected=[]（无答案），但 Agent仿制笔记入库后语料已有「三条学习路径」——加语料没同步标注会让检索评估的「闸门应拦截」断言静默失真；已修标注+docstring 写入「标注与语料必须同步」规则。后续触发信号：扩更难的对抗题库（多跳/需判断/诱导编造）复测，错误率数据才值得支撑 critic 决策。
- **评测题库分层原则（2026-09-12，产品化思考）**：「评测系统」与「评测题库」命运不同——系统（LLM-as-judge 管道/P@k/MRR/闸门检查）是**引擎资产**，产品化后更需要（每次改 embedder/切块/提示词必须先验证再发布）；题库按耦合度拆两层：①**语料指纹题**（答案埋在生产语料）——单用户个人 agent 合理（notes 即生产数据=每日回归），产品发布时转**合成语料 fixture 驱动**（测试自带受控迷你语料、对其提问——引擎质量与用户内容解耦，且顺带消灭「标注与语料同步」整类维护债：当天踩的过期标注坑本质就是题库绑死生产语料的症状）②**行为题**（资料不足诚实拒绝/忠于资料不编造/闸门行为/参数格式——内容无关）天然是产品资产，一字不改。与「evals 与线上共用同一语料」不变量不冲突——那条指评估**管道**=生产管道，不是题库必须装生产内容；合成语料仍走同一管道。产品化动作=换题库（行为题保留+指纹题合成化），评测管道原样复用——又一次换件不换衣服。当前无需动手，路标已立。
- **强杀丢数据的已知边界（2026-09-12 记录）**：对话中途终了的三种结局——①quit/exit 正常路径完整保存 ②Ctrl+C/Ctrl+D 经 except 块 trim 半截工具轮后保存（M6.2 已修）③**强杀（kill -9/关终端窗口/断电）丢本次启动以来的全部增量**——写盘只在退出路径发生一次，真相在内存、session.json 只是退出快照；KeyboardInterrupt 是 Python 能接住的优雅信号，kill -9 是直送内核的死刑，任何代码挡不住。补法两条（刻意从简暂不做，触发信号=真在日常使用中丢过一段对话心疼了）：信号处理器（SIGTERM/SIGHUP → trim+保存+退出，救得回温柔 kill 与关窗口，救不回 -9 与断电）；每轮落盘（最保险，但每轮全量 IO 且搅浑「内存是真相」模型）。
- **MCP-b：接线+冲突治理+沙箱（2026-09-13）**：MCP-a 的客户端从「只测不接」变成真实菜单里的外部工具（8 内置 + 3 个 mcp__ 前缀，`python -m agent mock/deepseek` 冒烟菜单 11 项；真模型点菜 mcp__get_server_time 成功回传）。三块设计裁定：①**沙箱围栏住服务器侧**（评审#1 的落地）——`_in_sandbox` 先 resolve 再判根前缀，一举挡住绝对路径（Path 除法吞前缀）、`..` 上跳、符号链接指外三类逃逸；**读也要围**——评审只点名写，但 read_local_file 读 .env 比写更致命；沙箱根 `servers/sandbox/`（gitignore 排除）可由 `MCP_SANDBOX_DIR` 环境变量覆盖——测试指 tmp_path 不污染仓库。②**冲突治理=前缀命名**：registry.register 语义是「重名后者覆盖」，无前缀时后注册的 MCP 工具会顶掉内置 write_note 的四道栅栏；统一 mcp__ 前缀让外部工具装不成内置，连前缀都撞（故意撞名）抛 McpError 拒绝——静默覆盖等于把菜单卖给外部进程。注册名=前缀+真名、调用仍锚真名（服务器不认识前缀）。③**接线**：__main__ 拉起 `servers/notes_server.py` 子进程 → register_mcp_tools → `finally` 无条件 close（正常/异常路径都不留孤儿；save 不放 finally——异常路径写回旧 session 会覆盖好数据）；接入失败（McpError/OSError）只警告不阻断内置工具——MCP-c 完整降级链的雏形。McpClient 新增 env 参数（继承+叠加），测试注入沙箱目录用，将来真实服务器的 API key 也走这条缝。
- **MCP-c：顽健性收官（2026-09-13）**：c 段回答「外面的手断了怎么办」——①**探活快速失败**：`McpClient.is_alive()`（poll 看尸体，非问话）挂在每次点菜前，服务器中途挂掉时秒级回「MCP 服务器已离线」错误，而非撞 `_request` 的 30s 超时才醒（与启动探活同一招，位置从启动挪到每次调用）②**死菜摘牌**：`ToolRegistry.unregister(name)`（新）+ `register_mcp_tools` 内置计数——同一工具连续 DEAD_TOOL_EVICT_AFTER=2 次撞死服务器，自动从菜单摘除并在结果里告知「该工具已从菜单移除」；模型看不到死菜自然不会反复撞墙，自纠环在菜单外继续成立。关键边界用例：服务器**活着**的业务错误（越界 isError）不触犯计数器——工具不被误摘、模型自纠照旧（有测试钉死）。③官方 fetch 服务器验收降级 backlog：无 node/npx 环境，npx @modelcontextprotocol/server-fetch 跑不起来——环境保护主义（同 chromadb 可选依赖纪律）：触发信号=装 node 或找到可信 Python 三方服务器时再上一轮真三方验收，不为一轮验收引入 Playwright 级重依赖。**MCP 里程碑整体收官**：手写客户端（stdio 七层原理）→ 互操作裁判（官方 SDK 服务器）→ 接线（菜单长出外部工具）→ 沙箱+前缀+摘牌（安全与顽健）全链路就位，81→85 用例全绿。
- **MCP-r：远程 HTTP 传输（2026-09-13，Context7 实战验收驱动）**：用户点名测 Context7（真三方 MCP 服务器）→ 官方 SDK 探针验收通过（2 工具：resolve-library-id/query-docs，实测吐回 chromadb PersistentClient 文档）→ 撞出真实边界：手写客户端仅 stdio。用户拍板「客户端长 HTTP 传输」，于是长出手写第 8 层：`mcp_http.py` 的 `HttpMcpClient`（httpx 同步，JSON-RPC 消息体不变——协议层与传输层分离）。协议四新点：①**会话头**：initialize 响应头抓 `mcp-session-id`、后续请求带回（HTTP 无状态，「记住我是谁」全靠它）②**SSE 分帧**：`text/event-stream` 的 `data:` 行解析成消息列表——「流式」首次进项目，streaming 里程碑的远亲；心跳/坏帧忽略不杀通道 ③通知→202 空正文、请求→200 带结果 ④DELETE 收摊尽力而为。**健康语义变体**：HTTP 无进程尸体可 poll——is_alive=最近一次调用没撞连接级失败（网络错/5xx 置死、4xx 不冤杀、成功复活）→ **死菜摘牌计数器原样复用**（远程宕机同样两招摘牌）。接口与 McpClient 同构（list_tools/call_tool/is_alive/close）——`register_mcp_tools` 一行不改：接口同构、实现异构的第三件衣服。`__main__` 里 Context7 **默认不接**（每次启动背网络依赖不值+无 key 有限速），`CONTEXT7=1` 环境变量开（prefix `ctx7__`，与本地工具分家）；mcp_clients 列表统一 finally 关闭。另立自建 `servers/http_demo_server.py`（纯标准库）：**会话头哨卡**——非 initialize 缺 `mcp-session-id` 一律 401，客户端忘回传测试当场炸。验收三层：本地演示服务器 7 条协议测试（SSE 解析/会话头/前缀注册/摘牌复用）+ 真网络 live 测试（`PYTEST_LIVE_NETWORK=1` 才跑，CI 默认跳过——真三方验收的第三块拼图：自建 → 官方 SDK 裁判 → 真实产品）。当日两个测试抓出的坑：①演示服务器模块级读 sys.argv 撞 pytest 参数（撞出 ValueError 后把端口解析挪进 main）②**死而不僵的 socket**：`server.shutdown()` 只停循环不关监听 socket——连接能建立没人应答，客户端白等满超时（从 43.8s 拖到 3.8s 的元凶）；补 `server_close()` 才真死，「看尸体 vs 等回音」的 TCP 版。httpx>=0.28 入 rag 可选组。全量 92 用例 + 1 live 跳过。
- **Context7 三次纠错循环（2026-09-13 实测记录）**：①mcp 2.x 改名潮第三次命中——`streamablehttp_client`→`streamable_http_client`（之前抓的是服务端 FastMCP→MCPServer，这次是客户端 API，同一次大改版的两半）②`inputSchema`→`input_schema`（SDK 全面蛇形化，抄旧文档代码会死）③resolve-library-id 的 schema 是 anyOf 复合体——`query` 与 `libraryName` 一个不能少，服务器端校验逐字拆弹（与 registry 结构层校验同哲学的服务器侧版本；再次坐实「复杂 schema 全量透传」裁定）④返回纯文本带 id（非 JSON）——拿库 id 要正则提取，真实三方工具的输出形态比教科书脏。
- **MCP-config：配置化装配（2026-09-13，用户「agent 都让人配置」观察驱动）**：MCP 形态二维拆解——传输层（stdio/HTTP+SSE/streamable HTTP，协议都是 JSON-RPC 2.0）vs 部署层（写死 vs 配置驱动）。`mcp_servers.json` 清单 + `mcp_config.py` 装配工厂：每台服务器 `{name, command 或 url 二选一, prefix 缺省 f"{name}__", enabled, timeout, headers}`；命令型穿 McpClient、URL 型穿 HttpMcpClient，`register_mcp_tools` 通吃（同构异构第三件衣服的装配版）。四个设计裁定：①**占位符 {python}**——command 首元素替换成 sys.executable，清单可移植（写死 "python" 撞「venv 未激活 PATH 无 python」实测坑）②**enabled=false 只挂名**——远程服务器默认不拉（启动背网络依赖不值），个人配置开 ③**含密钥配置不进仓库**——`MCP_SERVERS` 环境变量指个人文件（.env 纪律延伸）④**单台失败/撞名只警告**——坏一台不拖垮全场，撞名拒绝而非静默覆盖（菜单不卖给外部进程）。验收：8 条配置测试（校验/缺省前缀/占位符真连/死命令跳过/撞名跳过）+ 默认清单冒烟 11 工具 + 个人清单 live 冒烟接上 ctx7。全量 100 用例。
- **官方 GitHub MCP 服务器接入（2026-09-13，用户「再装个 GitHub 插件」驱动）**：排查链——gh 2.100 的 `gh mcp` 已从核心拆出（官方扩展不在 extension search）+ 无 node 无 go，本地部署路（Go 二进制/Docker/npm）全堵。联网查证官方文档后转向**远程托管版**：`https://api.githubcopilot.com/mcp/` 对全体 GitHub 用户开放（无需 Copilot 订阅），认证 = `Authorization: Bearer <PAT>`。PAT 用 `gh auth token` 现取（已有 gh 登录即可）→ **手写 HttpMcpClient 直接接入成功：44 个工具长进菜单（github__ 前缀），get_me 真实调用返回账号信息**。安全红线实务：token 只进临时个人配置与进程环境，不入仓库、不进日志、跑了即弃。测试资产：`tests/test_mcp_github_live.py`（PYTEST_LIVE_NETWORK 门 + gh 未装/未登录自动 skip；断言只信实测工具名——远程版命名与本地版不同），CI 默认跳过。至此 MCP 真三方验收凑齐三家：Context7（社区/远程 HTTP）+ 官方 SDK 裁判 + GitHub 官方（远程 HTTP）。全量 100 用例 + 2 live 跳过。
- **记忆固化 M6.4 落地（2026-09-13，兑现 2026-09-12 方向）**：`memory/consolidate.py` 四段管线——**①萃取**：LLM 档案员读复盘材料（滚动摘要+尾窗 12 条，与投影同构但不带人设）提炼候选条目，提示词铁律：只记对话明确说过的、禁补全推断（「档案员不是评论员」加强版）+ 三问过滤（跨会话成立？/以后用得上？/已知记忆没记过？）②**审查**：第二次 LLM 调用对照原文踢编造——**critic 第一次值班，挂在不可逆写入之前**（呼应 LLM-as-judge 度量「错误率 0% 暂不上运行时 critic」的裁定：批量离线场景先用上）③**硬校验**：程序管形状——类别白名单（非法类别归 other 不炸管道）/批内去重/条数上限 5/单条 200 字/JSON 容错剥围栏；分工铁律「信模型的部分是语义，不信的部分全都交给代码」④**落盘**：按类别 append 到 `data/learned/{类别}.md`，时间戳程序加（出处链条里程序是唯一可信作者）。**三桶刻意不含 preferences**：偏好是「用户级」信息，其仓库外位置未建——留桶等于引导模型把隐私写进可能公开的 repo，能力跟着位置走（红线联动）。**零成本门**：`since=loaded_len` 启动时消息数，无新对话退出时直接跳过、不白烧两次 LLM 调用。批间去重 v1 靠「已知记忆」提示词喂全部已有条目（升级触发信号=learned 条目 >10）。接线：__main__ 退出路径（save 之后、账单之前），内部链调用（拆链原则）。测试 7 条钉死全流程（管线写盘/审查踢编造/非法类别/坏 JSON/已知记忆入提示词/无新对话零调用/上限+去重），真模型冒烟写出 constraints.md 2 条（项目级、红线过滤后进 git）。剪裁记录（各自带触发信号）：用户级记忆→仓库外位置实现；长会话中途兜底→「会话后段记忆丢失」实测症状；learned 入 RAG→learned 文件数 >10 或需跨会话检索；审查升级跨供应商裁判→幻觉率实测 >0。全量 107 用例 + 2 live 跳过。
