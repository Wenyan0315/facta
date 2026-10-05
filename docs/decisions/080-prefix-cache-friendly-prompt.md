# ADR 080：prefix-cache 友好的 prompt 组装——静态前置、动态 stamp 后置 + 命中率记录

- 状态：**已批准**
- 日期：2026-10-05
- 立项出处：[competitive-roadmap P2-4](../internal/competitive-roadmap.md)（Prefix-cache 友好的 prompt 组装）
- 关联：073（时间/路由/计划三件 stamp 的手法与 `_prepend_stamps` 收拢）、078（常驻记忆注入有界——与本项「动态后置」同向）、M7.5 telemetry（账本＝事后归因的度量尺）、`core/llm.py`（usage 透传层）

## 背景

roadmap P2-4 原文：

> 改动：`orchestrator/assemble.py` 输出做稳定性排序——静态内容（system、
> AGENTS.md 等价物、工具定义）严格前置且字节稳定，动态内容（记忆召回、
> 会话状态）一律后置；记录每轮 prefix 命中率。
> 验收：同会话连续轮次 prefix 命中率可度量并纳入 evalkit 看板；命中率纳入成本回归。

DeepSeek 前缀缓存按**字节稳定的前缀**命中：请求 `messages` 数组的前缀字节
若与上次请求一致，缓存命中则省掉这部分输入 token 的计费（usage 对象返回
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 两字段）。命中率
直接折算成钱——这是成本侧最实的一条优化。

现状病灶（Grep + 目录核对）：

- **动态内容插在头部**：`_prepend_stamps` 把三个动态 stamp（`_time_stamp`
  分钟级变、`_route_stamp` 每轮变、`_plan_stamp` 计划更新时变）插在 payload
  位置 1/2/3——system 之后、history 之前。任何一戳变了，它**后面整段前缀
  全部失效**，缓存等于白建。
- **命中率不可度量**：`llm.py` 的 `generate` 与 `_usage_of` 只采集
  `prompt_tokens`/`completion_tokens`，usage 里的 prefix cache 字段被丢弃；
  `telemetry.UsageLedger` 也无对应字段。看不到命中率 = 这条优化有没有效
  全靠猜。

## 选项

- **A. 静态前置、动态后置**：静态内容（system 人设、工具定义）保持字节稳定
  严格前置；三个动态 stamp 移到 payload **尾部**；llm.py 采集 prefix cache
  字段、telemetry 加字段 + bill 渲染命中率行。
- **B. 顺带做时间戳降精度**（P2-6 ②：`_time_stamp` 从分钟级降为「日期 +
  上午/下午/晚间」）：进一步放大前缀稳定区间。
- **C. 把 session 内冻结的记忆快照也移出 system prompt**：让 system 段
  完全字节稳定。

## 拍板

**A。B 归 P2-6 ②（本项不做）；C 不做。**

### 1. 主论据：把「每轮必变」的内容从头部挪走，是缓存命中的充要条件

前缀缓存的命中断言是「前缀字节连续一致」。三件 stamp 恰恰是**唯一每轮
必变**的成分：时间戳分钟级变、路由决定轮轮换、计划步骤随执行推进。它们
插在头部 = 缓存命中的天花板被钉死在「stamp 之前的那一小段」。把它们移到
尾部，`[system 人设] + [摘要] + [未进摘要的原文] + [stamp ×3]` 的前缀就
稳定到「只有用户新消息追加在尾部」——同会话连续轮次里，除新增消息外的前缀
全部可命中，命中率从接近 0 抬到接近 1。

语义上成立：system 消息在 `messages` 数组里任意位置都是指令，模型不区分
「指令在头还是尾」；且 stamp 内容是**轮内稳定**的（一轮内多步工具导航不改
时间戳、不改路由决定），后置不牺牲「本轮视野」的注入语义。

### 2. 命中的前提是「可度量」——先接住 usage 再谈优化

optimization 分两步：先让命中率**能被看到**，再决定要不要进一步压 miss。
`llm.py` 两处 usage 采集补上 `prompt_cache_hit_tokens`/
`prompt_cache_miss_tokens`（`getattr` 兜底 None——极少数中转商不回这两
字段，防御不加价），telemetry 累加并渲染命中率行。这是 P2-4 验收
「可度量并纳入成本回归」的落地：账单上多一行，下次成本回归时命中率直接
进看板，不用另起炉灶。

### 3. B 归 P2-6 ②：降精度是「再压 miss」的第二步，不是第一步

时间戳从分钟级降到「日期 + 时段」确实能让时间戳本身跨更多轮稳定，但它
改的是 `_time_stamp` 的**输出契约**，`tests/test_timestamp.py` 有断言
`"今天：2026-09-12（周六）15:30"` 的精确格式。P2-6 ② 本就单独立项，且
与 P2-4 强耦合——但耦合是「同一条主线上的先后两针」，不是「必须一次扎完」。
先做 A（结构性后置），命中率度量到位后，B 的收益（还有多少 miss 来自分钟
级时间戳）就**有数据支撑再动**，而不是先猜着改。这是 ponytail「理解问题
在先」：降精度是第二次优化的触发条件，本项不该抢跑。

### 4. C 不做：记忆快照 session 内冻结，对「同会话连续轮次」命中无贡献

记忆快照（078 的常驻注入）在 session 启动时冻结、会话内不变——它属于
「字节稳定」的那一侧，不是「每轮必变」的病灶。把它移出 system prompt
对**同会话连续轮次**的命中率零贡献，反而增加一次 churn（挪动还会动
078 的分账装填结构）。YAGNI：不动它。

## 负决策（明确不做）

- **不做时间戳降精度**（P2-6 ② 专属，本项不抢跑）：`_time_stamp` 保持
  分钟级，等命中率度量到位、确认「分钟级时间戳仍是 miss 主源」后再动。
- **不把 session 内冻结的记忆快照移出 system prompt**：它不是每轮变因，
  移动只增 churn 无收益。
- **不做 assemble.py 新文件**：P2-4 原文点名的 `orchestrator/assemble.py`
  并不存在，现有组装分散在 `compressor.build_payload` + `loop._prepend_stamps`。
  本项只改现有落点，不新建组装层（删优于增）。

## 正面（本项落地的三处）

- **`loop._prepend_stamps` → `_append_stamps`**：三个 stamp 改 `payload.append`
  尾部追加（保持 time → route → plan 相对顺序），同步更新调用点与 docstring。
- **`core/llm.py`**：`generate` 与 `_usage_of` 两处 usage dict 增补
  `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`（`getattr` 兜底 None）。
- **`core/telemetry.py`**：`UsageLedger` 加 `prompt_cache_hit_tokens`/
  `prompt_cache_miss_tokens` 两字段；`record_llm` 累加；`bill()` 渲染命中率行
  （`prefix_total = hit + miss`，仅 `prefix_total` 非零时显示）。

## 边界（诚实登记）

- **命中率只在「同会话连续轮次」有意义**：跨会话冷启动第一轮 prefix 全量
  miss 是物理必然，不算病灶；度量口径 = 会话内的连续轮命中率。
- **记账仍是近似值**：telemetry 既有约定（并发下 `+=` 不精确、账单是参考值
  不是结算账目）对 prefix 字段同样成立，命中率行是「成本回归的观测尺」，
  不是精确账单。
- **「system 消息任意位置均指令」是模型语义假设**：后置 stamp 依赖这一假设。
  若某模型对尾部 system 指令不敏感（表现 = 忽略时间锚点/路由提示），命中率
  换来的成本省不下语义损失——那是这条假设的触发信号，不是本项预判能排除的。

## 验收

- 同会话连续轮次的 prefix 命中率可度量（`UsageLedger` 有字段、账单有命中率行）。
- 三个 stamp 位于 payload 尾部而非头部（`test_stamp_in_payload_not_in_history`
  断言不变——它只查「在投影里」任意位置，后置不破坏）。
- 三门通过（ruff / mypy / pytest）。

## 触发信号

- **时间戳降精度（P2-6 ②）开案**：命中率度量到位后，若 miss 主源仍是分钟级
  时间戳（即「后置之后命中率仍显著低于预期」），据此数据推进降精度。
- **模型对后置 stamp 不敏感**：出现「忽略时间锚点 / 忽略路由提示 / 计划
  失焦」的语义漂移样本——届时评估「尾部 system 指令」假设是否在目标模型
  上成立，再决定 stamp 位置是否需要折中（如仅路由/计划后置、时间戳留头）。
- **命中率纳入成本回归的看板信号**：evalkit 看板接入命中率后，若连续多个
  会话命中率异常低，回查 payload 组装是否被后续改动重新引入了「动态前置」。
