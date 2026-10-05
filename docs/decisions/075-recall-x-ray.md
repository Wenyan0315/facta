# ADR 075：recall x-ray——每次召回记「为什么召回」，陈旧率用影子指标量

- 状态：**已落地**
- 日期：2026-10-05
- 立项出处：[competitive-roadmap P2-1](../internal/competitive-roadmap.md)（记忆补全三件套之二：recall x-ray——每次召回记录「为什么召回这条」，并带陈旧率）
- 关联：061（记忆召回跟随 plan 上下文）、074（四态 tombstone——本案陈旧率的数据来源）、060（plan_failures 派生 JSONL 先例）

## 背景

061 让 make_plan 按字面匹配从 learned 三桶召回 ≤3 条相关条目，但召回是
黑箱：不知道哪条是因为哪个键被召回的，也不知道召回池里混进了多少「过期/
被取代」的旧条。roadmap 的补全项点名两件事：

1. **为什么召回**——每个命中条目要记录触发它的匹配键集合；
2. **陈旧率**——top-k 里「已过期/被取代」条目占比。

074 已给四态打好数据底子（状态 tag + `read_learned(include_inactive=…)`），
本项在它之上计数。

## 决策

### 甲：陈旧率＝影子指标，不改变注入结果

061 的召回注入走 `read_learned` 默认参数，天然只回「在用」条目——top-k
里本来就塞不进旧条，直接数注入结果是零，量不到「陈旧」这件事。所以陈旧率
必须是**影子指标**：候选池读 `include_inactive=True` 全量算 top-k，陈旧率＝
「若不过滤，top-k 会混进多少旧条」。注入结果仍只取 `status is None` 的
在用条目——①件（074）的读侧过滤照旧，本项纯仪表，一行不改注入链。

### 乙：陈旧＝已过期 ∪ 被取代（不含「已撤回」）

`_STALE_STATUSES = ("已过期", "被取代")`。「已撤回」是**人主动删除**，是
「这条不要了」，不是「这条过时了」——它与 roadmap 原文「top-k 中过期/被取代
条目占比」逐字对齐，撤回条目不应当污染陈旧率。

### 丙：落点＝派生 JSONL + evalkit 陈旧率函数

x-ray 落 `data/recall_xray.jsonl`，同 060 `plan_failures.jsonl` 先例：派生
索引（真值源＝召回事件史）、可删可重建、读写同居 `plan.py` 单模块、路径常量
住模块内（`_XRAY_PATH`）、`threading.Lock` 互斥 append。每行一条自含 JSON：
`ts` / `keys`（匹配键集合）/ `candidates`（影子 top-k，每条约 category、
matched、status、content）/ `staleness`。

陈旧率计算函数 `staleness_at_k(stale_flags, k)` 落 evalkit `ranking.py`——
纯函数、零 `facta.*` 依赖、可从项目拿走单独用（024 内核约束），随
`__init__.py` + `__all__` 导出。`plan.py` 只负责把影子 top-k 的 status
映射成 `bool` 列表喂给它，不把计数逻辑写在工具层。

### 丁：召回路径单落点改造，结构自含

唯一落点＝ `plan.py::_recall_learned`。命中条目一次排序（`-匹配键数,
桶序, 行号`），从同一次排序结果里同时派生两样东西：影子 top-k（喂
`_write_recall_xray`）与在用 top-k（喂注入串）。记录用 dict 携带 category /
matched（排序后的键列表）/ status / text，避免两套排序漂移。

## 边界（诚实登记）

- **影子不拦、只记**：陈旧率再高也不改变注入，回灌页脚「过时决定不替代
  当前对话」的语义兜底不变。真要用陈旧率去过滤/降权，是触发信号的事。
- **只量字面召回路径**：`_recall_learned` 只覆盖 make_plan 的 learned 召回；
  全量快照注入链（agent._learned_block）不在 x-ray 口径内。
- **匹配面仍是字面**：为什么召回的「键」来自 `_KEY_RE` 的 ASCII 标识符，
  中文标题不产键的老局限（061 反方 2）原样继承。

## 验收

三门全绿；新增 `staleness_at_k` 纯函数测试（占比按实检数、空候选=0）；
新增 recall x-ray 落账测试（含 `_XRAY_PATH` monkeypatch 防仓库污染）。

## 触发信号

- 陈旧率长期非零 → 议「召回前按陈旧度降权/过滤」（把影子转成真过滤）
- x-ray 显示某键命中大量噪声 → 议拓宽/替换字面匹配（061 遗留 2 的中文分词）
- `data/recall_xray.jsonl` 堆积 → 议 sleep-time 物理回收（P2-2）
