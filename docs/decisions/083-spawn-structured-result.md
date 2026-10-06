# ADR 083：spawn 成败判断结构化——RunResult 枚举替换字符串前缀

- 状态：**已批准**
- 日期：2026-10-06
- 立项出处：[competitive-roadmap P1-8](../internal/competitive-roadmap.md)（v0.5 编排补遗）
- 关联：033（`_FAILURE_PREFIXES` 成败判据的原始出处）、082（同批编排补遗、`spawn.py` 同区域）、055（与取消透传的 rebase 提示）

## 背景

roadmap P1-8 原文：

> 编排层代码评审。`spawn_step` 靠 `_FAILURE_PREFIXES` 字符串前缀判断
> done/failed——子 agent 结论措辞撞前缀即误判，而内部本有 `RunResult` 枚举。
> 改动：`spawn_subagent` 返回结构化的 `(status, conclusion)`，`spawn_step`
> 按枚举回写；字符串前缀仅作兼容兜底，过渡期后删除。

病灶：`spawn_subagent` 把 `run_turn` 的 `RunResult` 终态压平成一个结论字符串
（失败语义段），`spawn_step` 再用 `result.startswith(_FAILURE_PREFIXES)` 把字符串
还原成成败——枚举 → 字符串 → 前缀匹配，中间态不可靠：子 agent 正常完成但结论
恰好以「子任务失败：」开头（如「子任务失败：旧方案，改用新方案后成功」）会被
误判 failed；反过来，任何拼错前缀的失败也会漏判为 done。

## 拍板

`spawn_subagent` 返回 `tuple[RunResult, str]`（`(status, conclusion)`），
`spawn_step` 直接按 `status is RunResult.COMPLETED` 回写 done/failed，不再
`startswith`。工具层（`_spawn` 闭包）解包只暴露 conclusion 字符串——工具 func
契约（返回 str）不变，主底片 / 事件流 / 评测口径逐字节不变。

- `status` 三种取值直接复用 `run_turn` 的 `RunResult`（COMPLETED / CANCELLED /
  FAILED），spawn 不新造状态域。
- 提前 return 的参数/装配错误（task 空、worktree 缺 ctx、工具子集空、worktree
  建箱失败）归 `RunResult.FAILED`——诚实回写 failed，不与 CANCELLED 混淆。
- `_FAILURE_PREFIXES` 直接删除（结构化一次性覆盖全部调用方，无外部依赖前缀，
  不留过渡态）。

**负决策**：

- 不引入新的 dataclass/NamedTuple 返回类型——`tuple[RunResult, str]` 最短可用，
  两个消费者（`_spawn` / `_spawn_step`）就地解包，多一层类型是过度设计。
- 不保留 `_FAILURE_PREFIXES` 作「兼容兜底」——结构化返回后无任何调用方还吃前缀，
  兜底对象不存在，留着是死代码。
- 不改工具 func 契约——`_spawn` 仍返回 str，spawn 工具的 description / 参数 /
  事件流零改动。

## 正面（本项落地的位置）

- **`src/facta/tools/spawn.py`**：`spawn_subagent` 返回类型 `str` →
  `tuple[RunResult, str]`；四类提前 return 补 `RunResult.FAILED` 前缀；删
  `_FAILURE_PREFIXES`；`_spawn` 解包返回 conclusion；`_spawn_step` 解包按
  `status` 回写。

## 边界（诚实登记）

- **工具层结论串不变**：`_spawn` 仍回灌原结论文本，主 agent 看到的内容与改前
  逐字节一致——本项只改 spawn_step 的成败判据来源，不改 spawn 的对外文案。
- **参数错误现归 failed**：改前 `task` 空等装配错误串不撞 `_FAILURE_PREFIXES`，
  会被 spawn_step 误标 done；改后统一归 failed（诚实修正，附带收益）。

## 验收

- 对抗性用例：子 agent COMPLETED 且结论以「子任务失败：」开头 → spawn_step
  标 done（改前误判 failed）。
- 现有 spawn / spawn_step / worktree 测试全绿；三门通过（ruff / mypy / pytest）。
