# ADR 082：编排补遗三小件——取消透传 / 时间戳降精度 / loop.py 拆分

- 状态：**已批准**
- 日期：2026-10-05
- 立项出处：[competitive-roadmap P2-6](../internal/competitive-roadmap.md)（编排层补遗三小件）
- 关联：080（② 为 prefix cache 让路）、076（子 `run_turn` 的取消终态源）、S2a（③ 是分层方向的延续）

## 背景

roadmap P2-6 原文：

> ① **取消通道透传子 agent**——`should_cancel` 透传进子 `run_turn`
> （现状：取消要等子任务跑完一整轮，max_rounds=10 + 慢模型时体感差；
> 代价是定义子会话半截轮 trim 语义）；② **时间戳投影降精度**——`_time_stamp`
> 从分钟级降为「日期 + 上午/下午/晚间」，为 P2-4 prefix cache 让路（分钟级
> 时间戳插在 payload 位置 1，每轮 invalidate 其后全部前缀缓存；秒级需求本就走
> `get_current_time` 工具）；③ **loop.py 继续拆分**——投影装配（`_time_stamp`/
> `_plan_stamp`/build_payload 调用链）与批执行器（`_split_tool_batches`/
> `_run_parallel`/`_execute_tool_calls`）各自独立成模块，内核只剩决策循环
> （S2a 分层方向延续，不改行为）。

三件均为低风险可维护性/体验改进，一次立项合并落地，各自独立可验证。

## 拍板

### ① 取消通道透传：走既有 `receives_confirm`/`receives_event` 同款缝

现状病灶：`spawn.py` 的 `run_turn` 调用只透传 `on_confirm`，`should_cancel`
刻意不透传（`spawn.py:244` 注释「取消等主循环下一检查点」）。后果是用户点
取消后，主循环已在工具执行检查点②命中、但子 agent 正在 `max_rounds=10` +
慢模型里慢慢跑——取消体感延迟一整轮子任务。

修法＝新增 `Tool.receives_cancel` 标记（与 `receives_confirm`/`receives_event`
同款形状），`registry.execute` 注入 `should_cancel` 关键字参数，`agent.execute`
透传，`_run_parallel`/`_execute_tool_calls` 透传，`spawn_subagent` 收下后透传进
子 `run_turn`。取消命中时子 `run_turn` 走既有 `RunResult.CANCELLED` →
`trim_incomplete_round` 掐半截轮 → spawn 返回「子任务被取消」结论串 → 主 agent
的失败反馈环（M5「错误也返回字符串」）原样接手。

**负决策**：不定义新的子会话取消语义——直接复用 `run_turn` 的
`trim_incomplete_round`；子会话是纯内存临时对象、无持久副作用要回滚，worktree
收尾照旧走确认缝，取消不为 worktree 开特殊分支。

### ② 时间戳降精度：日期 + 上午/下午/晚间

080 已把三件 stamp 后置，但 `_time_stamp` 仍带分钟（`15:30`）。分钟级内容每轮
必变 ⇒ 投影尾部每个字节都不稳定 ⇒ 同会话连续轮次的 prefix 命中被最后一截时间戳
反复作废。降到半天粒度后，时间戳在同半天内字节稳定，prefix 缓存得以跨轮命中。
秒级/未来时间点需求本就走 `get_current_time` 工具（`_time_stamp` docstring 已注明
这个分工），降精度不损失能力。

映射：`hour < 12` 上午、`hour < 18` 下午、其余晚间。

**负决策**：不保留分钟（无消费者，且正是 invalidate 源）；不把「让模型自己查时间」
当默认路径（时间戳是便宜锚点，工具是精确通道，两者分工不变）。

### ③ loop.py 拆分：投影装配 projection.py + 批执行器 executor.py

`loop.py` 已 734 行，混杂三类职责：决策循环（`run_turn`/`_close_out`）、投影装配
（stamp 三件 + 轮首路由）、批执行（切批/并行/串行/事件转发）。按 S2a「内核与外设
分离」延续：投影装配搬进 `projection.py`，批执行器搬进 `executor.py`，内核只留
决策循环。纯搬移，函数体与语义逐字不变（②① 的改动落在搬移后的新模块里）。

**负决策**：不改行为（搬移不等于重构）；不把 `run_turn` 也拆走（决策循环是内核，
拆了才是过度设计）；不建立公开 API——两个新模块仍以 `_` 前缀内部函数导出，
`loop.py` 是唯一消费者（测试直接 import 是历史惯例，随搬移同步改路径）。

## 正面（本项落地的位置）

- **`src/facta/tools/registry.py`**：`Tool` 加 `receives_cancel: bool = False`；
  `execute` 加 `should_cancel` 参数并在 `receives_cancel` 时注入 `should_cancel`。
- **`src/facta/orchestrator/agent.py`**：`execute` 加 `should_cancel` 透传给
  `registry.execute`。
- **`src/facta/tools/spawn.py`**：`spawn_subagent`/`_spawn`/`_spawn_step` 加
  `should_cancel` 参数并透传进子 `run_turn`；两个 Tool 标 `receives_cancel=True`。
- **`src/facta/orchestrator/projection.py`**（新）：`_WEEKDAYS`/`_time_stamp`/
  `_plan_stamp`/`_route_stamp`/`_append_stamps`/`_route_first_menu`。
- **`src/facta/orchestrator/executor.py`**（新）：`_SPAWN_TOOL`/
  `_split_tool_batches`/`_run_parallel`/`_execute_tool_calls`/`_forward_plan_events`。
- **`src/facta/orchestrator/loop.py`**：删除搬走的三类函数，改为从两个新模块
  import `_append_stamps`/`_route_first_menu`/`_execute_tool_calls`。

## 边界（诚实登记）

- **① 取消仍受检查点粒度约束**：子 `run_turn` 的取消即时性 = 主 `run_turn` 的
  同款粒度（模型一次生成几十秒仍要等流式检查点③），不是信号级抢占。
- **② 半天粒度丢相对时间精度**：「下午三点前」这类说法失去锚点，需要精确时间
  时模型应调 `get_current_time`——这正是分工本意。
- **③ 拆分引入两个新模块**：物理行数不变（只是搬家），但 import 面多两个文件；
  测试里直接 import 内部函数的路径需同步改（`test_timestamp.py`/
  `test_parallel_spawn.py`）。

## 验收

- ① 取消子任务在一轮内生效（子 `run_turn` 拿到 `should_cancel`）。
- ② 同半天内连续轮次时间戳字节稳定，prefix 命中率可度量上升（挂 P2-4 看板）。
- ③ 冒烟套件全绿；三门通过（ruff / mypy / pytest）。

## 触发信号

- **取消后子 agent 仍长时间不回**：检查子 `run_turn` 的模型调用是否走到了流式
  检查点③之外的长阻塞（如非流式 LLM 实现）。
- **半天粒度不够**（出现「几点前」类频繁失败）：评估恢复「小时」粒度而非分钟。
- **projection/executor 出现第二个消费者**：届时考虑提升为公开模块接口。
