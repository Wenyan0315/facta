# ADR 078：记忆块硬上限 + sleep-time 整理——常驻注入有界，墓碑离线回收

- 状态：**已落地**
- 日期：2026-10-05
- 立项出处：[competitive-roadmap P2-2](../internal/competitive-roadmap.md)（记忆块硬上限 + sleep-time 整理）
- 关联：032（增长基线实测 + 裁定二 v2 信号）、074（tombstone——本案回收其堆积）、076（reflect 异步化——本案挂点的来源）、077（决定节轨迹）

## 背景

032 的增长基线（2026-09-23 实测）：learned 三桶 24 条 ≈ 1.2K 字，活跃期
2~3 条/天，**每条 ~50 字，200 条时全量注入 ≈ 1 万 token/轮**——成本曲线
真实且必然，只是增长维度是「条目/注入成本」而非「文件数」。而注入链
（`agent._learned_block` / `_user_memory_block`）每轮全量快照、无任何
护栏。Letta/MemGPT 的块上限（block cap）与会话间离线整理 pass
（sleep-time compute）是现成药方，roadmap 点名引进——但带一条硬边界：
**不引入 Letta 式「每次记忆操作都花推理」的在线自写循环**。

## 决策

### 甲：硬上限——预算分账装填，超限截断最老（注入侧）

- **计量单位 = 字符**（`len(render(entry))`）：无本地 tokenizer，中文
  1 字 ≈ 1 token 量级——字符数是保守上界，护栏语义宁紧勿松（提前触发
  不会漏拦）。`FACTA_MEMORY_BUDGET` env 可配，默认 6000（≈120 条
  50 字条目，约为 032 基线的 5 倍余量）。
- **装填优先级分账**：user → decisions → constraints → other（桶间
  按序领预算，前桶吃满后桶吃零）；桶内**从新到旧**装填——user 记忆
  是「相处知识」量小权重高优先保，decisions 类别优先级最高；条目级
  新的更可能相关（老的已服务过多轮）。保留条目按原文件序渲染
  （prompt 字节稳定，P2-4 prefix-cache 的伏笔不受扰）。
- 超预算：装到为止 + `logger.warning`（观测信号）；prompt 内不加
  「已截断」提示行——被截断条目对模型不可见是接受的边界，等真实出现
  「模型不知道被截断条目」的摩擦再议检索桥（挂触发信号）。
- **不做**：Letta 式在线自写循环（每次记忆操作都花推理重写记忆块）——
  roadmap 明文负决策；模型驱动的 active 条目自动删改——自动删用户
  资产是危险动作，「整理」只做规则层安全动作（见乙）。

### 乙：sleep-time 整理——超限才触发，零模型（settle 侧）

`maintain_memory` 挂在 `settle_session` 尾部：076 之后 settle 本就是
异步后台（会话结束、不占在线延迟），与 roadmap「会话结束后跑」的字面
严丝合缝。动作序列：

1. 计量当前常驻注入总量（`memory_footprint`：全部在用条目 render
   字符和，user + 三桶）；
2. **总量 ≤ 预算 → 零动作**（「超限触发」的字面，平时零成本）；
3. 超限 → **物理回收「事件时间 > 7 天」的 tombstone**（`sweep_tombstones`，
   LEARNED_LOCK 内读改写，与 delete_line 同款）：074 边界点名「tombstone
   行堆积 → 议 sleep-time 物理回收（P2-2）」在此兑现；
4. 报告随 settle 报告返回；无老墓碑可清时如实说「超限来自在用条目，
   请人工整理记忆面板」——**不在用条目上自动动刀**。

**7 天保留期的语义**：tombstone 的防复活价值在于被 `_load_known` 读到
（固化时模型看见「这条已撤回」才不复活）；7 天 = 至少数轮固化的可见
窗口，超期物理删不再损失防复活性。`FACTA_TOMBSTONE_KEEP_DAYS` env
可配。

### 丙：分层落点（依赖方向零循环）

- `learned.py::sweep_tombstones(path, keep_days)`——文件操作的家，
  锁内读改写复用既有模式；
- `consolidate.py`——`memory_budget_units()`（env 读取，单一真值源：
  注入侧与整理侧同一份预算）、`memory_footprint()`、`maintain_memory()`
  （CATEGORIES 的家，agent 已 import consolidate，方向不变）；
- `agent.py`——装填改造（`_fit_entries` 纯函数 + 两块的分账）；
- `assemble.py::settle_session` 尾部——挂点。

## 边界（诚实登记）

- **截断丢弃的条目模型完全不可见**：learned 没有运行时检索通道
  （075 的 recall x-ray 只是影子仪表），被截断的老条目只有记忆面板
  （人）能看。检索分层挂 032 裁定二 v2 信号（learned 全量注入成本越
  阈值时议 retrieval）。
- **footprint 是「字符」不是「token」**：验收口径的「token 有界」按
  字符上界兑现；真实 token 计数要 tokenizer，护栏场景上界即可。
- **整理不解「在用条目超限」**：sweep 只减 tombstone（它们本来就不
  注入——回收的是文件臃肿与 `_load_known` 噪音，不是注入尺寸）；
  在用条目超限的正解是人工整理或（触发信号后）检索分层，本案权限
  刻意止步。
- **user 块超预算时挤占 learned 到零**：user 优先的分账方向，现实中
  user.md 量小（M6.5 注记「全量注入零压力」），此边界极端情况罕见。

## 验收

三门全绿；对应 roadmap 口径——**常驻记忆 token 有界且可配置**
（`FACTA_MEMORY_BUDGET`，超预算注入必截断）+ **整理 pass 有场景防
回归**（超限触发 sweep / 不超限零动作 / 墓碑老删新留在用不动 / settle
集成报告）。

## 触发信号

- 在用条目超限成为常态（sweep 报「无老墓碑可清」频发）→ 议 032
  裁定二 v2 的检索分层（learned 走 retrieval 而非全量注入）
- 截断丢条目造成真实摩擦（「模型不知道某老条目」实发）→ 议截断区
  的检索桥（把被截断条目挂进 recall 通道）
- 需要 true-token 精度的护栏 → 议本地 tokenizer 估算（当前字符上界
  足用）
