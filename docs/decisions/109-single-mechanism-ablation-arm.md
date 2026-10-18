# ADR 109：单机制摘除臂——C_NO_SCOPE（范围闸净贡献归因）

- 状态：**已裁定**（2026-10-18，用户批准执行）｜日期：2026-10-18
- 上游：[046](046-frozen-real-task-eval.md)（评测纪律：双臂同源 / 禁单轮驱动 / 只改仪器不改被测物）、[096](096-real-task-memory-ablation-acceptance.md)（三臂对照）、[099](099-orchestration-review-spawn-step-and-plan-scope.md)（丙案缓议待证据）、[057](057-plan-tool-scope.md)（范围闸本体）；外部参考 delta-agent 的消融臂（`C_NO_SCOPE`/`C_NO_VERSION_CHECK`，本地 `~/Documents/trae_projects/AgentA/delta-agent`，非本仓文件，方法参照）。

## 背景

facta 现有消融全是**整臂摘除**（整子系统开/关），没有一个臂能做**单机制摘除**：

| 臂 | 摘除对象 | 粒度 |
|---|---|---|
| `full` vs `bash`（046） | 整族工具（换成单个 bash） | 整臂 |
| `full` vs `nomem`（096） | 整个记忆层（learned + 用户记忆） | 整个子系统 |
| `memory_ablation.py` | `learned_dir` 注入 | 子系统（闭卷召回） |
| `graph_ablation.py` | 图谱导航块 | 子系统 |

单机制的净贡献**目前无臂可测**，只能靠 `guards` 读数（[frozen_eval.py](../../evals/frozen_eval.py) 的 `_guards` 记哪条围栏出手了几次）与 `expect_tools` 断言归因——那是**读数**，不是**对照臂**：它们回答「机制出手了吗」，不回答「把这一条机制拿掉会怎样」。

具体等米下锅的决策：[099](099-orchestration-review-spawn-step-and-plan-scope.md) 的**丙案**（plan-scope 拦截越界改走 L2 确认）维持缓议，触发信号写死为「plan-scope 拦截再次伴随大面积步骤 failed」。目前只有「拦截 N 次」的正向读数，**反事实（关掉范围闸会怎样）从未测过**——这正是单机制摘除臂的用武之地。

delta-agent 的消融臂（`C_NO_SCOPE`/`C_NO_VERSION_CHECK`）给的方法：**一次只摘一条机制、其余全留**，用「摘除臂 vs 全装配臂」之差给该机制归因净贡献。

## 方案（范围）

### 做的

1. **新增单机制摘除臂 `mode = "no_scope"`**（CLI `--no-scope`，与 `--baseline`/`--nomem` 三者互斥）。
2. **摘除落点 = 甲案（用户拍板）**：`_full_arm` 里 `build_agent` **之后**，把该 agent 的 `registry.scope_check` 置 `None`——[registry.py:335](../../src/facta/tools/registry.py#L335) `if registry.scope_check is None: return None` 即干净关闭范围闸。多轮 `new_session` 重建 agent 后重补一次。**零产品改动**，被测机制代码一字不动（`plan_scope_check` 本体不碰）。
3. **与 full 臂逐字节同源**：同 provider、同 `git archive` 副本、同 `setup`、同 `_grade`/judge。**唯一变量 = 范围闸挂没挂**。
4. **用例一：喂 099 丙案反事实**。`--no-scope --only r2` 跑 plan 类场景，对照 full 臂：若关闸后完成率 / 步骤 failed 数显著改善 → 范围闸在杀任务（丙案有新证据基础）；若持平 → 拦截本就是「拒绝后自纠」的低成本形态（丙案继续缓议）。
5. **复用既有 harness**：`check_mechanism` 照常判（no_scope 只摘范围闸，计划/确认缝/工具族全在，`expect_tools`/`min_confirms` 照判）；落盘前缀 `no_scope-`；汇总标签「无范围闸臂」。

### 不做的（边界）

- **不建通用多机制框架**（`--ablate <名>` 注册表）：YAGNI——先拿 plan-scope 一个机制的净贡献，框架等真有第二个机制要摘再抽。
- **不碰 `plan_scope_check` 本体**：机制代码零改动，摘的是「挂不挂」不是「怎么判」。
- **不改任何被测机制的判定口径**：`expect_tools`/`min_confirms`/`forbid_strings`/交付守全都沿用（046 纪律：不为臂调断言）。
- **不额外处理 worktree 子 registry**：主 registry 在 build 时置 `None`，spawn 的 `_worktree_registry` 继承它 → 自然全关；不额外写代码。
- **不接 CI 定时跑**（真实 LLM 烧钱，046 同款）。
- **不据单轮读数控丙案**（046 偏差 5：≥3 轮同形态）。

## 定案（用户拍板）

- **T1 摘除落点 = 甲**：harness 侧后置补丁（`agent.registry.scope_check = None`）。零产品改动，与 `nomem`「起跑线抹记忆」同口径——摘除是**起跑线配置**，不改被测机制代码。用户拍板。
- **T2 首批摘除集 = 只做 plan-scope**（`C_NO_SCOPE`）。YAGNI：单一机制、证据链最干净，正好喂 099 丙案。用户拍板。

## 验收（已执行）

- [x] 三门全绿：ruff / mypy / pytest —— **912 passed, 7 skipped**（较基线 908 +4 条新增）。
- [x] 单机制摘除臂落盘口径：`mode="no_scope"` 走 `_full_arm`，落盘前缀 `no_scope-`、顶层 `arm`、汇总标签「无范围闸臂」。
- [x] **摘除生效可证**：`_ablate("no_scope", agent)` 置 `agent.registry.scope_check = None`；`full` 臂原值不动（`test_ablate_no_scope_clears_scope_check`）。
- [x] **换会话不复活**：`new_session` 重造 agent 后 `_ablate` 重做（`test_ablate_no_scope_survives_new_session`）。
- [x] **同源可证**：除 `scope_check` 挂否外，两臂装配/判分/judge 逐字节同源；`check_mechanism` 纳入 `no_scope`（计划/确认缝/工具族全在，不静默变题）。
- [x] 三臂互斥：`--baseline`/`--nomem`/`--no-scope` 两两组合被拒（`test_arms_are_mutually_exclusive`）。

**验证结果（2026-10-18）**：4 条测试全离线（假 agent / 假 ctx / monkeypatch，一次 LLM 不花）。档 A 摘除落点在 harness 侧 `_full_arm` 装好后置补丁——**`src/` 零改动**（被测机制 `plan_scope_check` 一字未动）。

### 真机验证结果（2026-10-18，deepseek，plan 三场景 r2/r4/m1 × 3 轮 × 2 臂，总耗 ≈¥1.14）

| 轮 | full 臂 | no_scope 臂 |
|---|---|---|
| R1 | 3/3，q5.00；r2 ✅ `guards=[plan-scope]` | 2/3，q4.00；r2 ❌ 无 `spawn_step` |
| R2 | 3/3，q5.00；r2 ✅ `guards=[plan-scope]` | 2/3，q4.67；r2 ❌ |
| R3 | 2/3，q5.00；r2 ❌ `guards=[]` | 2/3，q4.67；r2 ❌ |

- **摘除生效（验收项坐实）**：full 臂 `guards` 命中 `plan-scope` 共 3 次（r2×2、r4×1）；no_scope 臂 6 次运行全部 `guards=[]`——范围闸真被摘掉，非静默失效。
- **反事实读数**：关闸未改善任何场景——r2 完成率 `2/3→0/3`（0 次 `spawn_step`）、m1 质量 `5/5/5→4/4/4`、r4 持平；full 臂 r2 的两次 pass 均伴随 `plan-scope` 出手。
- **保留**：n=3 偏小；r2 的 full 臂自身亦翻车一次（R3）；m1 两臂 `guards` 皆空仍稳定差 1 分，不放大解读。
- **结论**：否证「plan-scope 拦截大面积杀任务」——范围闸非净杀手、略偏净正。作为喂 [099](099-orchestration-review-spawn-step-and-plan-scope.md) 丙案的反事实证据，丙案维持缓议。

## 反方（预写）

1. **还是自己考自己（046 反方 1）**——成立。单机制摘除提供的是**相对**读数（摘臂 vs 全装配），题目偏向会让两臂一起受益/受损，相对比较仍有效。但天花板仍是「自证不一致」，非「证明正确」。
2. **单轮读数能信吗**——不能。046 偏差 5 明说禁止单轮驱动机制改动；丙案证据须 ≥3 轮同形态。
3. **后置补丁算不算「改被测物」**——不算。046 的「只改仪器不改被测物」针对的是「改机制代码使数字无法归因」；本臂是**起跑线配置**（关掉一个闸的挂载），与 096 的 `_wipe_memory` 同性质——机制代码零改动，声明的差异就是「闸挂没挂」。
4. **摘了范围闸，其他断言会不会误判**——不会：no_scope 计划/确认缝/工具族全在，`check_mechanism` 照判；唯一变化是越界调用不再被拒，这正是要测的变量。

## 实现清单（已执行）

1. `evals/frozen_eval.py`：`ChildState.mode` 增 `no_scope`；`--no-scope` flag（与 `--baseline`/`--nomem` 互斥）；`_ablate()` 摘除落点（`_full_arm` build 后 + `new_session` 重建后）；`check_mechanism` 纳入 `no_scope`；落盘前缀与汇总标签。
2. `tests/test_frozen_eval.py`：4 条离线测试（`_ablate` 语义 / 换会话不复活 / 子进程路由 / 三臂互斥）。
3. 文档回写：本 ADR 定稿 + [architecture.md](../architecture.md)（v0.127→v0.128，索引加行）+ README ADR 计数 108→109。
