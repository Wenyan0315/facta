# 067 人设工具名错写修正：read_note → read_notes

> 状态：**已批准（2026-09-29，用户拍板「这个做」）**，同日落地｜日期：2026-09-29
> 立项出处：手动测试用例首轮（[manual-test-cases.md](../manual-test-cases.md)）发现③；[055](055-agent-roles.md) 硬伤 #5 早在 2026-09-28 就前瞻过「示例角色写了不存在的 `read_note`（真名 `read_notes`）」——但当时只当角色文件的错，没查到**源头在 DEFAULT_SYSTEM_PROMPT**；[063](063-eval-harness-answer-sheet-isolation.md) 双臂重跑的 m1 轨迹是评测侧实证。
> 一句话结论：**人设里报错的工具名会沿三条链路放大成真实故障——口误、泄漏 markup、计划 tools 声明（057 范围闸拦正确名调用逼出修订烧预算）；修法＝改对 4 处 + sha256 锁显式过卡。**

## 证据链（2026-09-29 一天内五处，跨生产与评测）

1. **A3 人设口误**：问「你是谁」，回答按 prompt 逐字报出 `read_note`——prompt 是唯一源头，模型只是复读。
2. **Run 3 泄漏 markup**：DSML 泄漏文本里模型想调的就是 `read_note`（训练时从 prompt 学到的名）。
3. **Run 5 计划声明连锁**：`make_plan` 的 `tools` 声明写了 `read_note` ⇒ [057](057-plan-tool-scope.md) 范围闸把**正确名** `read_notes` 的调用拒掉 ⇒ 模型被迫 `make_plan` 修订（吃一轮预算 + 一次人审）才走通。
4. **063 评测侧实证**（上午，早于手动测试）：m1 的 `make_plan` 修订理由逐字自陈「原计划 tools 声明里写的 `read_note` 工具名有误（实际是 `read_notes`）」——冻结集里同样烧掉一轮修订。
5. **语料回灌**：两篇笔记（`政策自动复查落地手册.md`／`一致性检查-第2_3层实操示例.md`）的工具列表示例里也是 `read_note`——检索召回时把错名再喂回模型，形成第二个污染源。

## 修法（4 处代码/语料 + 1 处锁）

- `src/agent/orchestrator/agent.py:37`：prompt `read_note` → `read_notes`（根因）。
- `src/agent/tools/web.py:36`：注释「与 read_note 同量级纪律」→ `read_notes`。
- `data/notes/` 两篇：JSON 示例 `"name": "read_note"` → `"read_notes"`。
- `tests/test_agent.py` sha256 锁第三次更新（`5fa79c0b…` → `c6b9bc49…`），更新史注释记明缘由——锁的语义是「改动必须显式过这里」，本案正是显式改动。

## 边界

- **历史文档不改**：[055](055-agent-roles.md)（硬伤 #5 证据）、[057](057-plan-tool-scope.md)（设计示例 `tools=["read_note",…]`）、[063](063-eval-harness-answer-sheet-isolation.md)（m1 修订理由引文）及 ADR 索引行里的 `read_note` 都是证据引用，append-only 纪律保持原样。
- **冻结集可比性注记**：prompt 变更影响 full 臂读数（方向是减少无谓的闸拦与修订）；两篇语料的 verify 均为文件计数（`-eq 15`），内容改动不影响判定。不为分数改题，只为改对事实。
- **不查存量会话**：历史会话底片里模型曾说过的 `read_note` 不追溯——它只是复述当时的 prompt，无副作用。

## 验收

- 三门全绿；`test_prompt_move_is_byte_identical` 用新哈希过（锁更新即验收物）。
- 全库 grep `read_note[^s]` 仅剩历史文档证据引用（3 份 ADR + 2 处索引/活清单行）。

## 触发信号

- 错名再现（人设口误/计划声明/泄漏 markup 任一处）→ 查语料与 learned 是否还有其他回灌源（本案已清语料，learned 无命中）。
