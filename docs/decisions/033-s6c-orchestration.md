# 决策记录 · S6c 真编排 spawn_step（2026-09-23）

> S6 第三站：plan 步骤驱动 spawn——把 S5b 计划与 S5c/S6b 派发焊接起来。返回 [architecture.md](../architecture.md)

- **焊点（核心认知）**：S6 前三站各补一块拼图，但 S5b 的 `make_plan`（拆任务）与 S5c/S6b 的 `spawn_subagent`（派任务）是两条平行线——「第 2 步派谁做」这个动作靠模型**临场手工对上**（点 spawn 后再另点 update_plan_step 回写），会忘、会错位、会重复。S6c 新增 `spawn_step` 工具，把这条手工桥变成程序焊缝。

- **四拍板**：
  - **P1 新工具而非给 spawn 加参数**：`spawn_subagent` 保持「裸派发」（派独立子任务，无计划上下文）、`spawn_step` 是「带计划归属的派发」。职责分开，谁都不背对方包袱；给已用得顺的 spawn 加 `step_id` 会让它变脏。
  - **P2 自动回写**：spawn_step 一步到位 `in_progress → 派子任务 → done/failed`（按成败）。「记得回写步骤」的认知负担收回程序——这正是编排 vs 模型临场点菜的本质区别。但保留步骤间掌舵权（模型每步后看结果、能修订计划），plan-then-act 的「计划是活的」不变。
  - **P3 串行为主，步骤级并行挂触发信号**：spawn_step 回写 `PlanBoard`（共享可变：`_pending` 队列 + 事件史），并行会撞它——比 S6b 的确认缝更微妙（`_pending` 并发 append 会丢计划事件）。且 S5b 语义本就是「逐步执行、边做边看」。无计划的裸 spawn 并行已由 S6b 解决，两条路径各司其职。触发信号=「真使用中计划相邻步骤明显独立、串行太慢」。
  - **P4 子 agent 禁 spawn_step**：`_FORBIDDEN` 加 `spawn_step`——子 agent 是执行者不是编排者（它已禁计划三件，加了 spawn_step 才能彻底封死「子 agent 再编排」）。

- **实现**：`_spawn_step(step_id, task, worktree, confirm)` 闭包——①校验（board.update_step 标 in_progress 时统一拦：无活跃计划/step_id 不在计划/已终态，ValueError 转错误串回灌，薄包装与 plan.py 工具同款）②复用 `spawn_subagent` 全套（噪声隔离/工具子集/worktree/确认透传）③成败回写 done/failed（成败判据=`_FAILURE_PREFIXES` 固定失败串，worktree 收尾把结论拼在前、前缀判断稳定）④回灌 `format_view` 最新视图（S5b 回灌导航同款）。

- **验收**：pytest 400 passed（+6：自动回写 done/failed + 无计划/bad id/已终态拒绝 + 子 agent 权限）；冒烟回归 4 条全过（spawn_subagent 外部契约不动，spawn_step 是新增）；ruff/mypy/CI 全绿。

- **S6 全站收官状态**：S6a 隔离（worktree）→ S6b 并发（并行 spawn）→ S6c 编排（plan 驱动 spawn）。三站拼齐「多 agent 协作」的最小闭环：拆（make_plan）→ 派（spawn_step 绑定步骤）→ 隔离（worktree）→ 并发（裸 spawn 并行）→ 汇总（finish_plan）。技能包格式（skill package）与跨进程（崩溃域隔离）仍挂触发信号，未在本轮。