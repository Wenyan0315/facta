# 决策记录 · 记忆体系调研裁定（2026-09-22）

> 记忆系统业界调研（Trae / Qoder / Pi 三系）对照 cortex 现状的方向裁定。返回 [architecture.md](../architecture.md)

- **调研范围**：Trae（两层本地 Markdown——全局 `user_profile.md` + 项目 `project_memory.md`，治理规则=一次性指令/模糊偏好/敏感信息不自动存）；Qoder（自进化引擎 5 大类 26 小类 + SearchMemory 三模式 + `memory_overview` 注入 + Proactive Memory Agent）；Pi 系（`claude-mem`/`engram` 混合检索、`pi-persistent-intelligence` 分层治理 + Tombstone、Mycelium 反设计）。cortex 现状：learned 三桶（decisions/constraints/other）+ 固化四段管线（萃取→审查→硬校验→append）+ 记忆面板 v1 编辑侧。

- **裁定一：不搬 5 大类分类**。Qoder 首类 `user_preference`（user_info/hobby/communication/behavior）全是用户级信息；cortex 三桶**刻意不含 preferences** 的红线（仓库外用户级位置未建，开桶=引导隐私写进可能公开的 repo，能力跟着位置走，见 [consolidate.py](../../src/agent/memory/consolidate.py) M6.4 注记）——照抄即撞红线。26 小类对个人 agent 教学规模过度（YAGNI）。**执行记录（2026-09-23）**：红线漏网实证——constraints 桶混入 2 条行程类条目（阳澄湖行程，用户级信息绕过铁律固化）；已清除，并给萃取铁律 2 补「含行程安排、饮食起居等生活类内容」显式举例防再犯（管线免疫，非新增桶）。

- **裁定二：借「目录树先行 + 按需检索」，挂触发信号（v2 修订，2026-09-23）**。`memory_overview` 注入目录树 + 按需 SearchMemory 取全文，是「元记忆分层加载」的完整形态；cortex 现状是 `_load_known` 把全部 learned 整段塞进写侧 prompt 做去重、S5a 起三桶快照全量注入 system prompt。
  - ~~v1 触发信号「learned 文件数 >10」（废弃，2026-09-23）~~：**死信号**——文件数=分类数=3 是结构常数（一桶一文件，M6.4 设计），不加分类永远不涨，信号永不触发（v1 照抄了 M6.4 原始注记的同款缺陷）。
  - **v2 触发信号（本质表述）**：①learned 全量注入的 token 成本越过阈值（如 >5K token/轮）——连续可观测的缓坡信号，一露头即可介入，不存在「晚了」；②出现跨会话检索记忆的真实摩擦。届时按手写原理件路子先目录树、retrieval 再评估，不照抄 SearchMemory 三模式（fetch/search/explore）与 keywords 字段（向量检索下全文够用，keywords 是 FTS 多余键，等入检索再定）。
  - **增长基线（2026-09-23 实测）**：三桶合计 24 条（decisions 2 / constraints 10 / other 12），活跃期约 2~3 条/天；每条 ~50 字，200 条时全量注入 ≈1 万 token/轮——成本曲线是真实且必然的，只是增长维度是「条目/注入成本」而非「文件数」。

- **裁定三：显式区分 append-only 与可删记忆**。Qoder `task_summary` 只追加不删，与 cortex 决策记录 append-only 同源；但 learned 三桶的记忆面板 v1 是「可编辑可删」。现靠 `docs/decisions/`（append-only）与 `data/learned/`（可删）两个目录暗暗区分——小改动作机：把「决策/任务归档类 vs 普通教训类」的删除语义显式化，下一个碰记忆层代码的站顺手做。
  - **对外出口的删除语义约束（2026-09-25，[044](044-memory-layer-mcp.md)）**：本裁定从「内部整洁问题」升格为**对外能力边界**——记忆层 MCP 化时只暴露两个只读工具，写路径 `memory_add` 挂触发信号「tombstone 落地」。理由：当前删除语义只有物理删除（`learned.delete_line`，面板的编辑权），没有撤销机制；把写权交给外部进程 = 改错了撤不回，且可能触发下方待确认项①（被删条目遭固化管线重新提炼「复活」）。**故 tombstone 不再是「先确认痛点再动」的潜在项，而是写路径开闸的硬前置**；内部面板的可删可编辑不受影响（面板在信任边界内，且是修正入口）。

- **裁定四：反思必须保持异步旁路**。Qoder 实测四缺陷之首=同步阻塞（每轮先跑意图识别+预召回拖慢响应）；cortex 现有的固化管线在**退出复盘时**触发、不挡主响应，已做对。若引入「用户说"又忘了"→反思」，触发点可加但执行必须延续退出时/异步旁路，不进主链路。
  - **条目腐烂实证（2026-09-23，增长之外的第二维度）**：other.md 已出现时效性条目——「run_turn 定义在 loop.py 第 113 行，该文件共 227 行」（代码已改，事实过时）、「笔记库当前共 14 篇」（时点快照，必然过时）。此类条目会从「记忆」变成「假知识」，与 learned 曾被幻觉污染的同族风险——反思维护与 Tombstone 的真实需求证据，已非假设。

- **不归记忆层的借鉴**：Qoder 的 Knowledge / `knowledge_module_tree`（Knowledge Cards，模块边界/架构）属**知识层**，对应 cortex 的 architecture.md + S7 图谱方向，不混进记忆层。

- **三系调研结论速记**（供后续引用）：
  - Trae：最朴素，两层本地 Markdown + 三条「不自动存」治理；与 cortex learned 三桶治理同源。
  - Qoder：最完整工程化（5 大 26 类 + 七步流水线 + 六路召回 + 记忆 Agent）；最大教训在「流水线 → 记忆 Agent」演进动机——同步阻塞、召回主动性低、命中率低、反思弱四缺陷。本记录裁定一~四即取其教训、弃其重装。
  - Pi 系：三方向并存——混合检索（claude-mem/engram，FTS5+Chroma）、分层治理 + Tombstone（persistent-intelligence）、反设计（Mycelium 赌「强模型 + 通用文件工具 > 专用记忆设施」）。Mycelium 不适用：cortex 走性价比 flash 档，模型判断力撑不起 agent 全权自组织；但「记忆对人是普通可 diff 文件」这条 cortex 以决策记录 Markdown 已部分踩对。
  - 待确认/潜在项（不立项）：①Tombstone 防「删除的记忆被 consolidate 重新提炼复活」——先在 cortex 确认是否真有此痛点再动（**2026-09-25 升级**：已成为对外写路径 `memory_add` 的开闸前置，见裁定三注记与 [044](044-memory-layer-mcp.md)；内部痛点确认仍是独立议题）；②DeepSeek 前缀缓存（pi-reasonix：system prompt + tool 定义 byte-stable 时命中 94%+，否则 <20%）——M10 主力 deepseek-flash 的成本优化潜在项，与记忆层无直接关系，挂 M10 注记。