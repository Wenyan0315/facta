# 决策记录 · 确认缝的裁决回灌对称（054）

> 批准与拒绝都必须回灌给模型：拒绝早有固定文案，批准原先静默——模型只能从「结果回来了」反推「大概没弹框」，实测据此幻觉出不存在的白名单项。
> 返回 [architecture.md](../architecture.md)

- **状态**：已批准（2026-09-27，用户裁定「批准也回灌痕迹，带上 guard 规则名」）
- **前置**：[050](050-memory-write-gate-and-guard-attribution.md)（`guard` 规则名进审计，那句「否则安全叙事只能靠模型自述」）、[037](037-aci-tool-feedback.md)（P2 空输出显式化，本案的顺序约束来源）、[019](019-s4b-terminal-confirm.md)（确认缝本体）、[048](048-run-command-sandbox.md)（沙箱与审计打标）、[052](052-memory-write-fence.md)（否决档案第 1 条：措辞军备竞赛赢不了）

## 背景与动机

用户报了两句同一意图、不同措辞的话：「帮我帮我泡个 sleep 30」与「运行命令：sleep 30」。前者被模型自己拒了，后者弹了确认框、用户批准、执行成功。**问题不在「措辞决定执行与否」，而在执行成功之后模型说了什么**：它宣称「没弹确认，被当只读放行」「sleep 居然在只读白名单里」，并据此**两次**提议去摘一个不存在的白名单项。

三件事同时为真：确认缝确实触发了（审计有 `guard: not-whitelisted`）、用户确实批准了（`exit code: 0`）、`sleep` 确实不在白名单里（纯函数复核）。模型的自述与三条地面真值全部相反。

根因是 `registry.execute` 的**不对称**：拒绝路径回灌固定文案「用户拒绝了这次操作（未经确认不执行）。请换方案……」，批准路径直接返回工具原始输出，**零确认痕迹**。模型对「这次调用经过了人审」这件事完全不可见，只能反推——而反推的方向恰好是错的（结果顺利回来了 ⇒ 大概没拦）。

这是 050 那个缺口的**镜像**。050 补的是给人看的审计侧 `guard` 字段，当时写下「否则安全叙事只能靠模型自述」；本案实测证明还差一半：**人能从审计查出撞了哪道围栏，模型仍不知道自己经过了人审**。裁决结果只回灌了一半。

## 地面真值（实测先于设计）

**① 审计条目（`data/audit/audit-20260927.jsonl`）** —— 确认缝触发了，且规则名早就记下来了：

```json
{"ts":"2026-09-27T14:16:00","tool":"run_command","args":{"command":"sleep 30"},
 "result":"exit code: 0","sandbox":"seatbelt","guard":"not-whitelisted"}
```

同文件另有 14:10:06 / 14:10:17 两条 `sleep 2`，`guard` 同为 `not-whitelisted`。注意 `result` 只有 `exit code: 0`——**审计记的就是模型所见，两边都没有确认痕迹**。

**② 落盘会话的完整故障链（`data/memory/sessions/20260927-141226.json`，13 条消息）**：

| # | 角色 | 内容要点 |
| --- | --- | --- |
| 5 | user | 「帮我帮我泡个 sleep 30」（口语） |
| 6 | assistant | **refusal**：阻塞会话 + 零产出，没调工具（审计里无对应条目） |
| 7 | user | 「运行命令：sleep 30」（命令式） |
| 8 | assistant | 预告「不在只读白名单里，会先弹确认框」——**这句是对的** + `tool_calls` |
| 9 | tool | `exit code: 0`，**仅此一行** |
| 10 | assistant | 「没弹确认，被当只读放行」「sleep 居然在只读白名单里」+ 提议摘白名单 |
| 12 | assistant | 同一提议重复一次 |

第 8 条与第 10 条自相矛盾，中间只隔了一条 `exit code: 0`。模型的心智模型在拿到工具结果那一刻被推翻了，而它选择相信自己的推断而不是那条缺失的证据。

**③ 纯函数复核（`src/agent/tools/terminal.py`）** —— 证伪模型的两条断言：

- `'sleep' in _WHITELIST_SIMPLE` → **False**。白名单是 `{ls, cat, head, tail, grep, rg, find, wc, pwd, which, file, sort, uniq, diff, echo, pytest}`（terminal.py:42-45），另有 `_GIT_READONLY = {status, log, diff, show}` 与 `python -m pytest` 两条特例（terminal.py:47-56）。
- `_confirm_rule("sleep 30")` → `'not-whitelisted'`（兜底规则，terminal.py:84-117）；`needs_confirm("sleep 30")` → True。
- `TIMEOUT_SECONDS = 60`（terminal.py:38）⇒ `sleep 30` **不会超时**，`exit code: 0` 是正常返回，不是异常截断。

**④ 确认缝在两个壳里都真实存在**（不是「弹框其实没接上」）：[app.py:215](../../src/agent/app.py) `on_confirm=run.request_confirm`、[app.py:338](../../src/agent/app.py) `POST /api/runs/{run_id}/confirm` → `run.resolve_confirm(body.approve)`；[cli.py](../../src/agent/cli.py) `_cli_on_confirm`（`input` 裁决，默认拒绝）。⇒ 用户「有弹窗执行了」的陈述与代码一致，缺口纯在回灌侧。

**⑤ 拒绝路径早就有文案**（registry.py:203-207）：

```python
if needs and (confirm is None or not confirm(name, args)):
    result = "用户拒绝了这次操作（未经确认不执行）。请换方案，或先向用户说明理由再重试。"
```

⇒ 本案不是「新增一类反馈」，是**把已有的对称补完**。改动面因此极小。

**⑥ 影响面：既有断言全是 `in`，不会被追加串破坏。** `test_terminal.py::test_confirm_approved_executes` 断言 `"exit code: 0" in out`；`test_sandbox.py::test_audit_sandbox_field` 只断言 `extra["sandbox"]` 不断言 `result`；`test_app.py` 的 confirm 端点测试查事件类型；`test_spawn.py` 的 `danger_tool` 走同一收口点。三门实跑印证（见执行校正 ①）。

## 裁定

### 甲案：批准也回灌，规则名复用 050 的 `guard`（用户裁定）

`_approval_trace(needs, guard)` 返回一行中文括注，免确认路径返回空串：

```python
if not needs:
    return ""
return f"\n（本次调用经用户确认批准{f'；命中规则：{guard}' if guard else ''}）"
```

规则名**不另立真值源**：直接用 050 那份 `_confirm_rule` 的返回值（`needs` 是非空 str 时即规则名，否则 `guard=None`）。这样模型看到的规则名与审计 `extra["guard"]` 逐字相同，人查一条就能对上模型当时看到了什么。

带上规则名而不只是「经用户确认批准」的理由：规则名是**可行动信息**。看到 `not-whitelisted` 模型知道自己撞的是兜底规则（命令不在只读白名单），看到 `credential-path` 知道碰了凭证文件——下一次它能在动手前就预判，而不是执行完再猜。

### 乙案：落点＝`registry.execute` 的唯一收口点，且拼在空输出显式化**之后**

不改任何工具，不改 `confirm` 回调签名。收口点是既有事实（037 P2 的空输出显式化、S3 的审计落盘都在这里），本案的痕迹加在同一处 ⇒ 所有 `needs_confirmation` 工具零成本继承，不只是 `run_command`。

顺序是硬约束：痕迹**必须**拼在 `if not result.strip(): result = "（无输出）"` 之后。反了的话 `result` 恒非空，037 P2 的显式化永不触发，两条信息一起丢。这条约束有专门的回归靶子（判定标准 3）。

痕迹拼在审计 `record` 之前 ⇒ **审计记的 `result` 与模型所见逐字一致**，不会出现「审计有痕迹、模型没看到」或反之的第二种漂移。

### 否决档案（懒惰阶梯痕迹）

- **只改 `run_command` 的工具描述（写明「sleep 不在白名单」）**：否决。工具描述是公开口径，052 执行校正 ⑦ 已实测反证措辞层军备竞赛赢不了（模型读了闸门源码后明说要「避开内容闸」）；且这是**结构不对称**，任何 `needs_confirmation` 工具都缺痕迹，只补一个工具的描述治不了形状。
- **把 `sleep` 加进 `_WHITELIST_SIMPLE`**：否决。白名单的形状是「只读且无副作用」，`sleep` 是纯阻塞零产出，加进去是语义污染；更要紧的是它只让「sleep 在白名单里」这句幻觉**成真**，不解决「批准静默」这个根因——下一次换成 `curl` 一样幻觉。
- **在 CLI/Web 壳里打印「已批准」给人看**：否决。人本来就知道自己点了批准按钮；缺的是模型侧的可见性。
- **痕迹只写进审计 `extra`、不写进 `result`**：否决。050 已经做了审计侧，本案的缺口恰恰是模型不可见；两边各写一份＝两条真值源，正是 P1-3 那个老病。
- **改 `confirm` 回调签名让它返回一段说明文字**：否决。要动 cli / app / spawn 三处调用点与既有测试，收益等于一行字符串拼接。
- **把「措辞一致性」立刻固化成冻结集场景**：暂缓（未否决，见触发信号）。口语那句的 refusal 是模型自主价值裁量（理由「阻塞会话 + 零产出」，压根没调工具），与本案不同源；单独立场景需要先想清楚 verify 断言什么——「必须执行」会把合理拒绝也判红。

## 判定标准

1. 批准路径的回灌串含「经用户确认批准」与 `guard` 规则名，且工具原始输出不被痕迹挤掉（`"exit code: 0" in out`）。
2. **免确认路径不带痕迹**（对称的另一半）：白名单命令直跑时，模型不能以为它也弹过窗——否则把「静默直跑」这个既有正确认知也搞错。
3. 顺序回归靶子：`needs_confirmation=True` 且返回空串的工具，`（无输出）` 与痕迹**同时在场**。
4. 审计 `result` 与模型所见逐字一致（同一个字符串对象，不是两份拼接）。
5. 三门全绿，零既有测试回归。

## 实现清单

1. `src/agent/tools/registry.py`：加 `_approval_trace(needs, guard)` 模块级函数 + 在收口点 `result += _approval_trace(...)`（位置在空输出显式化之后、审计 record 之前）；`execute` docstring 补一句「裁决结果两条路都回灌」。
2. `tests/test_terminal.py`：import `Tool`；新增三条测试对应判定标准 1/2/3。
3. 三门。
4. 回写 architecture.md（v0.86 + 索引 054 + 安全考点条）与 competitive-roadmap.md。

## 执行校正（2026-09-27 落地后追加）

① **三门读数**：`ruff check src tests evals` All passed｜`mypy src` Success（59 files）｜pytest **664 passed, 2 skipped**（053 基线 661，净 +3＝清单 2 那三条）。清单 1-2 零偏差落地，一处按门调整：

- **ruff PLR0912 挡了一次**：`execute` 加分支后 13 > 12。修法是**抽出 `_approval_trace` 而不是提高阈值**——阈值是既有的复杂度预算，`execute` 已经是全项目最长的收口函数，把 6 行注释 + 2 行逻辑挪出去同时让函数变短。第二次跑即绿。

② **未实机重跑那两句，诚实登记**：本案的机制由单测证明（判定标准 1/2/3 三条），不由实机复现证明。理由：msg[10] 那个幻觉是模型侧非确定性产物，重跑一次绿了也不构成机制证据（[046](046-frozen-real-task-eval.md) 纪律：单次绿不记战功）；而「回灌串里有痕迹」是确定性的，单测就是它的充分证明。**若后续要端到端验证，正确的断言是「审计 `result` 字段含规则名」而不是「模型没再说错话」。**

③ **触发信号（何时回来动这块）**：

- 痕迹是**中文自然语言**，模型读得懂但不保证每次都读。若观测到模型仍然误判确认缝（例如把带痕迹的结果说成「直接放行了」），下一步是把裁决结果做成**结构化字段**（tool result 的 JSON 元数据）而不是加长文案——加长文案是 052 已否决的那条路。
- `_approval_trace` 只覆盖 `registry.execute` 这一条路。**绕过 registry 的执行路径（评测 harness 的 `_shell`、`baseline_agent._bash`）本来就没有确认缝**，不需要痕迹；若将来给基线臂加确认，须同源调用这个函数。
- 「措辞一致性」是否立成冻结集场景仍未决（见否决档案最后一条）。触发条件：再观测到**同一意图因措辞不同而一边执行一边拒绝**，且拒绝理由不是安全考量（本案口语那句的理由是「阻塞会话 + 零产出」，属价值裁量，不是安全边界）。届时 verify 要断言的是「工具被调用过」而非「回答里提到执行了」。
