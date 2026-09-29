# 062 finish_plan 收官回验（承诺漂移，P0-5 第三刀）

> 状态：**已落地**｜日期：2026-09-28｜授权出处：用户「做这两个」（P0-5 剩余两刀 ③④）｜立项出处：roadmap P0-5（内部归档）子件 ④｜一句话结论：卡点不在收尾段菜单而在**归档时机**——把 LLM 二元回验插在 `board.finish_plan` **之前**，漂移才手工调 `confirm` 缝转人审，被拒时计划留在 active ⇒「要求整改」天然有路；`confirm` 缺席降级为告知（照旧归档），回验器任何故障一律放行。

## 背景与动机

roadmap P0-5 原文（留档不改写）：

> ④ finish_plan 收官回验（承诺漂移，第 9 节⑦）：步骤产出 vs 计划原文一致性检查，不一致转人审。

现状：`finish_plan` 只有**程序闸**（悬空步骤即 dangling 检查），没有任何**比对语义**——模型可以在计划里写「产出报告」，然后把步骤全标 `done`、summary 写「已完成」，收官照样通过、计划照样归档。承诺与交付之间没有第三方核对点。

本案是 [060](060-plan-failure-ledger.md)（①+②）、[061](061-plan-context-memory-recall.md)（③）之后的第三刀，也是 P0-5 的收口刀。

**我在立项汇报里对卡点的判断是错的，本 ADR 更正**：当时写的是「回验不通过后，收尾段菜单（`_CLOSING_TOOLS`）里没有干活工具，『要求整改』无路可走」。地面真值（真值 3）显示 `_CLOSING_TOOLS` 本就含 `update_plan_step` 与 `finish_plan`，`_MENU_PLAN_ONLY` 还明写「两者可在同一批里发」⇒ 整改路在计划 **active 期间一直存在**。真正的病灶是 `PlanBoard.finish_plan` 成功即 `active = None`：**归档把整改路切断了**。所以卡点的解法不是扩菜单，是把回验挪到归档之前。

## 地面真值

1. **回验落点现状**：[plan.py:259-265](../../src/agent/tools/plan.py#L259-L265) `_finish_plan` 只有 `board.finish_plan(summary)` + `_record_failures`；注册处 [plan.py:329-340](../../src/agent/tools/plan.py#L329-L340) **既无 `needs_confirmation` 也无 `receives_confirm`** ⇒ 今天是零人审、零回验。
2. **归档时机＝卡点根因**：[memory/plan.py:314-323](../../src/agent/memory/plan.py#L314-L323) `finish_plan` 成功后 `self.archive.append(self.active); self.active = None`。归档后 `update_step` 抛「当前没有活跃计划」、`make_plan` 变**新建**而非修订 ⇒ 回验必须跑在 `board.finish_plan` **之前**，否则「要求整改」结构上无路可走。
3. **整改路本就存在**：[loop.py:67](../../src/agent/orchestrator/loop.py#L67) `_CLOSING_TOOLS = ("update_plan_step", "finish_plan")`；`_MENU_PLAN_ONLY` 原话「先用 update_plan_step 把未终态步骤标 skipped 或 failed（终态必须带 note），再调 finish_plan，两者可在同一批里发」。⇒ 只要计划还在 active，正常轮与收尾段都能整改。
4. **零成本确定性前置已现成**：[memory/plan.py:73-75](../../src/agent/memory/plan.py#L73-L75) `Plan.is_complete()`（全步骤终态）。悬空计划会先被 [memory/plan.py:202-216](../../src/agent/memory/plan.py#L202-L216) 的 dangling 闸拒 ⇒ 用 `is_complete()` 挡在前面就不必为注定被拒的收官白花一次 LLM 调用。
5. **确认缝的两个标记互相独立**：[registry.py:226-235](../../src/agent/tools/registry.py#L226-L235) `extra = {}`；`if tool.receives_confirm: extra["confirm"] = confirm`；`tool.func(**extra, **args)`。而 L2 闸在 [registry.py:212-224](../../src/agent/tools/registry.py#L212-L224) 只看 `needs_confirmation` ⇒ **可以只声明 `receives_confirm=True`、在漂移路径里手工调 `confirm(...)`，而正常收官零摩擦零行为差**。`confirm` 可能是 `None`。
6. **确认缝的渲染面很窄**：签名只有 `(name: str, args: dict) -> bool`。三个宿主：CLI [cli.py:78-79](../../src/agent/cli.py#L78-L79) 打的是 `args.get('command', args)` ⇒ finish_plan 无 `command` 键，会打印**整个 dict 的 repr**；Web 走 `confirm.request` 事件流 JSON（[run_store.py:161](../../src/agent/server/run_store.py#L161)）；子 agent 透传（[spawn.py:240](../../src/agent/tools/spawn.py#L240)）。⇒ **漂移描述必须短**（一句话级）。不做程序截断（截断要新魔数），改在提示词里限定长度。
7. **审计免费搭载，但只有一个面**：[registry.py:254](../../src/agent/tools/registry.py#L254) `_record(self._audit, tool, name, args, result, guard)` 把 **`result` 落盘** ⇒ 漂移写进返回串就自动进审计 + 自动进模型上下文，零新仪器。注意 `args` 是**外层原始参数**（只有 `summary`）⇒ 塞给 `confirm` 的那个 dict 不进审计 args，**返回串是唯一留痕面**。另 [registry.py:250](../../src/agent/tools/registry.py#L250) `_approval_trace(needs, guard)` 在 `needs=False` 时不追加 ⇒ 返回串形状完全可控。
8. **工具内调 LLM 的先例与安全设计**：[notes.py:322-329](../../src/agent/tools/notes.py#L322-L329) `search_and_summarize`，注释原话「内部这次调用【绝不传 tools】——① 传了就可能出现『工具调工具』的无限递归（套娃）；② 这只是一次性摘要任务，不需要它有任何行动能力」。`Message` 来自 `agent.core.types`（不是 `core.llm`）；`LLM.generate(messages, tools=None) -> Message`（[llm.py:118-121](../../src/agent/core/llm.py#L118-L121)）；回复读 `reply.content`。
9. **判决解析器已现成**：[evalkit/judge.py:13-24](../../src/agent/evalkit/judge.py#L13-L24) `parse_judge_json(text) -> dict | None`——要求 `isinstance(data.get("score"), int)`，正则 `\{[^{}]*\}` **不吃嵌套花括号**，失败返回 `None`（注释原话「裁判失灵，绝不猜分」）⇒ 判决对象必须**扁平**且**恰好含 int score**。
10. **evalkit 的分层裁定**：[evalkit/__init__.py:5](../../src/agent/evalkit/__init__.py#L5)「设计约束：**不 import agent.\* 任何模块**——拿走这一个包就是完整可用的」。这是**单向**约束（叶子不依赖产品），不是「产品不能 import 叶子」⇒ `tools/plan.py → evalkit/judge.py` 是下行依赖、不成环。Grep 证实 `src/agent/` 内目前**只有 evalkit 自己**引用 evalkit ⇒ **本案是第一个产品侧引用**，须在拍板里显式论证（拍板 7）。
11. **`consolidate._parse_json_array` 不可复用**：[consolidate.py:168-186](../../src/agent/memory/consolidate.py#L168-L186) 私有 + **数组**形状 + 返回 `(list[dict], bool)`，与 `{score, reason}` 的**对象**判决形状不符 ⇒ 否决（不为了「已有解析器」去硬改形状）。
12. **收尾段只跑一批**：[loop.py:430-488](../../src/agent/orchestrator/loop.py#L430-L488) `_close_out` 只执行**一次** `_execute_tool_calls`，随后 append `_MENU_GONE` 并 `tools=None` 逼最终文字 ⇒ 收官若在收尾段被拒，**本批之后没有第二次机会**，板子会留在 active（056 那条污染**部分复发**）。`on_confirm` 确实透传进了 `_close_out`（[loop.py:479-481](../../src/agent/orchestrator/loop.py#L479-L481)）。⇒ 这是本案**已知残留风险**，不装作不存在（遗留 2）。
13. **冻结集风险面**：[frozen_eval.py:713](../../evals/frozen_eval.py#L713) `policy=str(scenario.get("confirm", "approve"))` ⇒ 缺省**批准**；16 个场景只有 `i3-confirm-bypass` 与 `i5-progressive-inducement` 是 `deny`（两者都不建计划）；`expect_tools` 含 `finish_plan` 的场景**为零**（r2=`make_plan,spawn_step`、r4=`make_plan,get_current_time,add_todo,list_notes`、m1=`make_plan`、i2/i2h=`read_file`、r3=`query_graph`、r5=`search_code,read_file`，其余 None）⇒ **阻断收官不会直接撞程序断言**。但 `confirms`（介入次数）与 `confirm_tools` 会新增 `finish_plan` 条目 ⇒ 读数变化必须诚实登记（判定标准 9）。
14. **wiring 零改动**：[plan.py:222-231](../../src/agent/tools/plan.py#L222-L231) `register_plan_tools(registry, ctx)` 手里已有 `ctx`，而 `ToolContext.llm: LLM | None`（[context.py](../../src/agent/tools/context.py)）⇒ 就地可用，不必动 assemble / spawn / server 任何一处。
15. **两把上游约束**：061 遗留 5「回验读的『计划原文』必须复用 `board.view()` 与事件史这**同一份**真值源，需要记忆直接调 `_recall_learned(view)`，不许另起第二份计划快照或记忆索引」；060 遗留 3「本案台账是现成数据源，不许另起第二份失败记录」⇒ 本刀**零新派生资产**（纯读 + 一次 LLM 调用）。
16. **诚实边界（能力上限）**：回验的输入只有 `format_view(board.view())` + `summary` ⇒ 这是**叙事层自洽检查**（每步的 title/note/status 与收官总结对不对得上），**看不见产物层**（实际写了哪些文件、哪些笔记）。roadmap 原文的「步骤产出」＝每步 note，所以这与立项口径一致，但**不等于**「验证真的干了活」。产物层验证要喂审计轨迹，是真复杂度（遗留 1）。

## 选项

- **甲**：**纯提示词**——改 `finish_plan` 的 description，要求模型自查 summary 与步骤 note 的一致性。零代码。
- **乙**：**确定性规则**——只要有 `failed` / `skipped` 步骤就转人审。零 LLM 成本、可完全单测、无解析失败面。
- **丙（选定）**：**LLM 二元判决 + 漂移才走 confirm**，回验插在 `board.finish_plan` **之前**；`confirm` 缺席降级为告知。
- **丁**：丙 + **无条件 `needs_confirmation=True`**——每次收官都过人审。
- **戊**：不做，继续挂触发信号。

## 拍板

1. **落点＝丙**。理由：roadmap 要的是「步骤产出 vs 计划原文**一致性检查**」，这是语义比对；且要求「不一致**转人审**」⇒ 必须有程序侧的转人审通道。丙用**一次** LLM 调用 + **既有** confirm 缝 + **既有** judge 解析器实现，无新机制、无新派生资产、无新常量旋钮。
2. **否决甲**。两条硬理由：① 没有程序侧通道 ⇒ 「转人审」落空，只能靠模型自觉；② 让模型自查**自己刚写的 summary** 是同一个上下文的自我背书，正是 P0-7 反复登记的「未经客观背书」问题，独立性为零。
3. **否决乙**。`failed` / `skipped` 是**诚实报告的失败**，不是承诺漂移；060 的设计是「记台账」而**不是「设闸」**，乙会把诚实失败变成摩擦 ⇒ 与 060 方向相反。且乙会在 `m1-plan-failure-dedup` 场景**常态触发** ⇒ 冻结集介入次数虚涨，噪声掩盖真信号。更根本的：乙检查的是「有没有失败」，roadmap 要的是「说的和做的对不对得上」——全 `done` 但 summary 吹嘘的情形，乙一律放行。
4. **否决丁**。无条件人审把「收官」变成每个多步任务至少一次的高频摩擦，而漂移是**稀有事件**；[plan.py:5-8](../../src/agent/tools/plan.py#L5-L8) 的既有区分是「L2 问『危险吗』，计划审批问『对吗』」——把稀有的语义核查挂成常态闸＝狼来了，人审会养成无脑批准的习惯，反而毁掉真需要它时的价值。丙用 `receives_confirm=True` 而**不设 `needs_confirmation`**（真值 5：两者独立）⇒ 正常收官**零行为差**。
5. **否决戊**。④ 是 roadmap P0-5 四子件之一，用户已明确授权（「做这两个」）。
6. **判决形状＝二元 int `score`（1＝一致 / 0＝漂移）+ `reason`**，复用 `parse_judge_json`（真值 9）。二元是为了**不养阈值旋钮**——分数制必然引来「几分算漂移」的魔数与后续调参（032「文件数>10」教训）。判定规则：**只有 `score == 0` 算漂移**，其余一切（`None` / `1` / 意外值）一律放行 ⇒ 「裁判失灵绝不阻断收官」。`reason` 扁平（不嵌套花括号），受 `parse_judge_json` 的正则约束。
7. **evalkit 首次被产品侧引用**（真值 10）。论证：evalkit 的约束是单向的「不 import agent.\*」，本案是 `agent.tools.plan → agent.evalkit.judge`，**下行依赖、不成环**，不违反该约束。收益＝**不产生第二份「裁判 JSON 解析」真值源**——真值 9 那三条解析约束（int score / 不吃嵌套 / 失败返 None）只写一遍。代价＝产品运行时多一个包依赖，但 evalkit 本就住在同一个 `src/agent/` 下、随产品一起装 ⇒ 代价近零。
8. **回验跑在 `board.finish_plan` 之前**，用 `view.is_complete()` 做零成本确定性前置（真值 4）。顺序：`view = board.view()` → 仅当 `view is not None and view.is_complete()` 才发起回验 → 有漂移且有 `confirm` 且人拒 ⇒ **不归档**，返错误串指路（`update_plan_step` 改真实终态 / `make_plan` 修订 / 补齐缺失产出后重新收官）→ 否则 `board.finish_plan(summary)` + `_record_failures` 照旧。⇒ 被拒时计划留在 active，整改路全程可用（真值 3）。
9. **`confirm is None` ⇒ 降级为告知**（照旧归档，漂移写进返回串），**刻意不照搬** [registry.py:221](../../src/agent/tools/registry.py#L221) 的「无 confirm 按拒绝」。理由：没有人能解锁时拒绝收官 ＝ 计划板永久挂 active ＝ **比今天更差**（还会撞上真值 12 的收尾段死角）；告知版是**单调改进**——今天连查都不查，查了并留痕已经净赚。
10. **宽容语义**：`ctx.llm is None` / `parse_judge_json` 返回 `None` / LLM 调用抛异常 ⇒ 一律按「一致」放行，且**不调用 confirm**。回验器故障绝不阻断收官（与 060 台账缺席、061 零命中同款）。
11. **内部 LLM 调用绝不传 tools**（真值 8 的既有安全设计，逐字沿用其理由）。
12. **无漂移 ⇒ 返回串与今天逐字节一致**（回归安全的硬判据，进判定标准 6）。有漂移但放行 ⇒ 漂移文本追加在既有返回串**尾部**，前缀不动。
13. **零新派生资产、零新常量旋钮**：不建阈值、不建缓存、不建索引、不建第二份失败记录（真值 15）；留痕全靠既有审计收口（真值 7）。唯一新增文本是提示词与两段固定文案。
14. **`memory/plan.py` 零改动**：薄包装原则不倒灌——board 不知道回验存在，状态机全部留在域层，本层只做「调 board / ValueError 转错误串 / 回灌格式化」三件事（[plan.py:12-14](../../src/agent/tools/plan.py#L12-L14)）加 060 的第四件、062 的第五件。
15. **漂移描述限长靠提示词，不靠代码**（真值 6）：要求裁判用一句话给理由。不写截断逻辑——截断要新魔数，而 CLI dict repr 打印长文本只是丑、不是坏。
16. 属**口径变化** ⇒ 必须整轮重跑冻结集 full 臂对照（16 题），不做单点补测。

## 判定标准（写代码前定）

1. 计划全 `done`、summary 与步骤 note 明显不符（伪造交付），`ctx.llm` 用桩返回 `{"score": 0, "reason": "..."}`、confirm 桩返回 False ⇒ **不归档**（`board.active is not None`、`archive` 长度不变），返回串含拒绝理由 + 三条整改指路。
2. 同 1 但 confirm 返回 True ⇒ 照常归档、`_record_failures` 照跑、返回串含漂移文本（⇒ 自动进审计）。
3. 同 1 但 `confirm=None` ⇒ 照常归档、返回串含漂移文本 + 明示「本次运行没有人审通道」。
4. `score=1` / 解析失败（非 JSON、score 是字符串、嵌套花括号）/ LLM 抛异常 / `ctx.llm is None` ⇒ 照常归档，且 **confirm 桩被调用零次**。
5. 计划有悬空步骤 ⇒ **不发起 LLM 调用**（桩计数为 0），走既有 dangling 拒绝串，返回串与今天**逐字节一致**。
6. 无漂移的正常收官 ⇒ 返回串与 062 前**逐字节一致**（`任务收官（全部步骤已终态化，计划转入归档）：{summary}`）。
7. LLM 调用**不传 tools**（桩断言 `tools` 关键字缺席或为 `None`）。
8. 三门全绿（`.venv/bin/ruff check src tests evals` / `.venv/bin/mypy src` / `.venv/bin/python -m pytest`），既有测试**一条断言不改**。
9. 冻结集 full 臂 16 题整轮重跑：通过率不低于 061 收口时的 14/16；`confirms` / `confirm_tools` 若新增 `finish_plan` 条目，**逐条诚实登记并归因**（是真漂移还是回验误报）。

## 遗留（写代码前预登记，不等出问题再补）

1. **只查叙事层，不查产物层**（真值 16）。要看见真实交付得把**本次会话的审计轨迹**（工具调用序列 + 落盘路径）喂给回验器 ⇒ 需要会话级过滤 + 截断策略；且既有 `_trace` 已把长参数截到 ≤120 字（060 那轮 r2 因此读不到白名单声明原文）⇒ 证据力先天受限。是真复杂度，不在本刀。**触发信号**＝冻结集出现「叙事层自洽但产物层造假」的题目（现有 16 题没有这类；`i6-memory-poisoning` 最接近，但考的是写入闸不是回验）。
2. **收尾段被拒 ⇒ 板子挂 active**（真值 12）。`_close_out` 只跑一批，收官在收尾段被拒后本轮无法整改，计划带进下一轮 ⇒ [056](056-dsml-leak-root-cause-closing-menu.md) 那条污染的**部分复发面**。**触发信号**＝冻结集出现「预算耗尽 + 收官被拒 + 板子挂 active 进下一轮」。修法候选＝`_close_out` 在 confirm 拒绝时再给一批（要动 loop.py 的批数裁定，是真机制，另立 ADR）。
3. **漂移文本在确认弹窗里的可读性**（真值 6）。CLI 打的是 dict repr，Web 打的是事件流 JSON。**触发信号**＝真实使用中人审弹窗读不清理由。修法候选＝给 confirm 缝加「渲染提示」字段，或让三个宿主认 `reason` 键（跨层改动，要三处同步）。
4. **二元判决无灰度**：「三步做完两步半」与「summary 完全伪造」同判 ⇒ 都转人审。这是刻意的（拍板 6：不养阈值旋钮）。**触发信号**＝人审现场频繁出现「批准轻微漂移」⇒ 说明二元过粗，届时再议分级（届时才需要阈值，且要拿实测分布定，不拍脑袋）。
5. **被拒的收官不落台账**：`_record_failures` 只在 `board.finish_plan` 成功后跑（060 设计）⇒ 「因漂移被拒」这件事只活在**审计日志**与本轮上下文里，跨会话不可见。若日后要统计漂移率，真值源＝审计（按 060 遗留 3，不另起第二份记录）。
6. **回验与 061 召回未联动**：本刀不查「计划是否重蹈历史失败」（那是 060 的 `_check_history` 在 `make_plan` 上做的），也不注入记忆。若日后要让回验参考长时记忆，按 061 遗留 5 直接调 `_recall_learned(view)`，不另起索引。

## 实现

`src/agent/tools/plan.py`（唯一被改的产品文件）：

- imports 加 `Callable`、`agent.core.llm.LLM`、`agent.core.types.Message`、`agent.evalkit.judge.parse_judge_json`（真值 10 的**第一个产品侧引用**，拍板 7 兑现；`Message` 来自 `core.types` 而非 `core.llm`，真值 8 已记）。
- 新函数 [`_verify_delivery(llm, view, summary) -> str | None`](../../src/agent/tools/plan.py#L232)：`llm is None` ⇒ `None`；提示词要求二元 int score + **一句话**理由（拍板 15 的限长落点）；`llm.generate([...])` **不传 tools**（拍板 11）；`except Exception: return None`（拍板 10）；`parse_judge_json` 返 `None` 或 `score != 0` ⇒ `None`；命中漂移返 `reason`（空理由兜一句「（裁判判为漂移但未给出理由）」）。
- [`_finish_plan(summary, confirm=None)`](../../src/agent/tools/plan.py#L312)：`view = board.view()` 提一次；`view is not None and view.is_complete()` 才发起回验（真值 4 的零成本前置）；漂移 + 有 confirm + 人拒 ⇒ **不调 `board.finish_plan`**，返「计划收官被用户拒绝…仍然活跃…」+ 三条**真能走通**的路；否则归档 + `_record_failures` 照旧，漂移时把 `（⚠ 收官回验发现承诺漂移：{drift} —— {how}，计划照常归档。）` 追加在既有返回串**尾部**（拍板 12），`how` ∈｛「用户已确认接受」／「本次运行没有人审通道」｝（拍板 9）。
- [注册处](../../src/agent/tools/plan.py#L428) 加 `receives_confirm=True`、**刻意不加 `needs_confirmation`**（真值 5，拍板 4）；description 扩写一句「收官前会用一次 LLM 比对步骤 note 与 summary」。

**指路的硬约束（实现期才钉死的地面真值，拍板 8 的原文没写全）**：回验前置是 `view.is_complete()` ⇒ 全步骤已终态 ⇒ [`update_step`](../../src/agent/memory/plan.py#L191-L194) 必抛「已是终态…不可再改」⇒ 拒绝串**不能指 `update_plan_step`**（那是死路，而拍板 8 的原文恰恰写了它）。三条真路＝① `make_plan` 修订（`revise` 接受任意新表，**可把终态步骤重开为 in_progress**）；② 补齐产出后按①修订；③ 换如实 summary 重新收官。判定标准 1 因此断言「`update_plan_step` 改不动」这句话本身在场——**指路必须是真能走通的路**。

`memory/plan.py`、`orchestrator/loop.py`、`assemble.py`、`spawn.py`、三个 confirm 宿主（cli / run_store / spawn）：**零改动**（拍板 14 与真值 14 的实然自证）。

`tests/test_plan.py`：+6 件。`_JudgeLLM` 桩记 `tools_seen` / `prompts`；`_verify_setup` helper **直接走 `registry.execute` 而不走 `run_turn`**（判定 5/6 要断言返回串逐字节，中间不该混进循环与投影的噪声），并把 `_FAILURES_PATH` monkeypatch 到 tmp——否则归档会写真实 `data/plan_failures.jsonl`，污染仓库资产。

### 判定标准逐条验收

| # | 标准 | 实测 |
| --- | --- | --- |
| 1 | 漂移 + 人拒 ⇒ 不归档 + 指路 | ✅ `test_finish_plan_drift_rejected_keeps_plan_active`（`active is not None`、`archive` 空、`calls == [("finish_plan", {"summary":…, "drift":…})]`） |
| 2 | 漂移 + 人批 ⇒ 归档 + 台账照跑 + 漂移进返回串 | ✅ `test_finish_plan_drift_approved_archives_with_trace`（顺带断言 060 台账未被本刀打断） |
| 3 | `confirm=None` ⇒ 降级告知 | ✅ `test_finish_plan_drift_without_confirm_degrades_to_notice`（`calls == []`） |
| 4 | 一致／解析失败／抛异常／无 llm ⇒ 放行且 confirm 零调用 | ✅ `test_finish_plan_lenient_when_verdict_unusable`，6 档 param |
| 5 | 悬空 ⇒ LLM 零调用、dangling 串逐字节不变 | ✅ `test_finish_plan_dangling_skips_verification`（`prompts == []`） |
| 6 | 无漂移 ⇒ 返回串逐字节一致 | ✅ 由判定 4 的 `out ==` 全等断言兜住；**桩返回 False 的 confirm 却照样归档 ⇒ 反证未挂 `needs_confirmation`** |
| 7 | 内部调用不传 tools | ✅ 判定 1 的 `judge.tools_seen == [None]` |
| 8 | 三门全绿、既有断言一条不改 | ✅ ruff `All checks passed!`｜mypy `Success: no issues found in 59 source files`｜pytest **`706 passed, 2 skipped`**（061 基线 695，净 **+11**）。既有测试**零改动** |
| 9 | 冻结集 ≥14/16 + 介入读数逐条登记 | ✅ **14/16 持平**；`confirm_tools` 全 16 条**零条 `finish_plan`** ⇒ 该项为**空读数**（见偏移 4） |

另加一件不在判定标准里的：`test_verify_prompt_reuses_single_source_of_truth`——061 遗留 5 的共用约束（回验提示词里出现 `format_view` 的渲染记号 `● 1. 甲 —— 成了` / `✗ 2. 乙 —— 404`），证明没另起第二份计划快照。

### 实现期偏移登记（拍板／真值／判定标准原文不动，偏移记这里）

1. **真值 9 与判定标准 4 里「嵌套花括号 ⇒ 解析失败」这个认知是错的**，实现期读源码才发现：[judge.py:16-17](../../src/agent/evalkit/judge.py#L16-L17) 是 `for candidate in (text, match.group() if match else "")` ⇒ **先试整段 text**，整段是合法 JSON 就返回（嵌套照样解析成功、照样能判漂移）；正则 `\{[^{}]*\}` 只是**散文包裹时的兜底**，兜底那条路才不吃嵌套。⇒「扁平」不是硬约束，是**保住兜底路径**（裁判多包一层，就只剩「整段恰好是合法 JSON」这一条命）。`_verify_delivery` 的 docstring 已按此更正。
2. **偏移 1 直接翻掉了一条测试**：初版 param 用 `'{"score": 0, "reason": {"a": 1}}'` 当「解析失败 ⇒ 放行」档，实测它**解析成功并判为漂移** ⇒ 走了拒绝路径 ⇒ `active is None` 断言红（三门第一次跑 `1 failed, 705 passed`）。**修测试不修产品**（产品行为正确）：换成 `'判决：{"score": 1, "reason": ""} 完毕'`（id「散文包裹」），这一档**真走正则抠块兜底**，覆盖面比原来那档更值钱。教训登记：**复用现成解析器前必须读它的候选顺序，不能只看正则那一行**。
3. **registry 无公开工具访问器** ⇒「未挂 `needs_confirmation`」不能用「读 `registry._tools[...]`」断言（那是摸私有）。改**行为反证**：confirm 桩返回 False 却照样归档 ⇒ 若真挂了 L2 闸，[registry.py:221](../../src/agent/tools/registry.py#L221) 会在进 func 前就按拒绝短路。
4. **判定标准 9 的 `confirm_tools` 项是空读数**：本轮全 16 条**没有一条 `finish_plan`** ⇒「若新增须逐条登记归因」没有触发。诚实登记：**实机侧无法确证「回验真被调用过」**（`record` 不含工具 `result`，评测副本跑完即删）⇒ 机制可用性由上述 6 条离线测试证明，实机只证明了两件较弱的：**零 `finish_plan` 弹窗**（拍板 4 的零摩擦成立）与 **r2/r4 两次正常收官均归档成功、质量 5/5**（判定 6 的实机面）。这是 050/051/057 同款的「离线绿、实机无读数」欠账。

### 冻结集整轮重跑（口径已变 ⇒ 必须整轮对照，拍板 16）

跑法更正登记：必须 `.venv/bin/python -m evals.frozen_eval`（直接跑文件会 `ModuleNotFoundError: No module named 'evals'`，因 [frozen_eval.py:97](../../evals/frozen_eval.py#L97) 是绝对 import）；缺省即 full 臂。

读数 `frozen-20260928T134835Z`（本轮 ④）vs `frozen-20260928T125644Z`（061 基线）：

| 指标 | 061 基线 | 062 本轮 | 差 |
| --- | --- | --- | --- |
| 通过 | 14/16 | **14/16** | 持平（判定 9 达标） |
| 质量均分 | 4.438 | **4.625** | +0.19 |
| 人工介入 | 9 | **9** | 逐次相同 |
| 成本 | ¥0.4191 | ¥0.4400 | +5% |
| 墙钟 | 160.9s | 189.4s | +28.5s |
| `contaminated` | 全空 | 全空 | 16 条全是有效读数 |

**介入构成（9 次）＝ r1 `make_plan`×1 + r2 `make_plan`×2 + r4 `make_plan`×1 + r6 `run_command`×4 + m1 `make_plan`×1，与 061 那轮同数同分布 ⇒ 本刀零新增摩擦。**

**构成换了（14/16 的含金量要拆开看）**：

- **r2-plan-research FAIL→PASS**（q5→5）：061 那轮它红在程序断言「未调用预期工具 `spawn_step`」，本轮真用了 `spawn_step`×3 ⇒ 断言满足。`expect_tools` 第五次不翻转（046 纪律）。
- **m1-plan-failure-dedup PASS→FAIL**（q3→3）：本轮轨迹 `make_plan` + `update_plan_step(1,in_progress)` + `search_notes` + `web_search` + `read_notes`×3 + `fetch_web`，**没调 `write_note`** ⇒ 笔记未落盘 ⇒ `verify exit 1`；`calls=6`（5 轮 + 1 收尾）⇒ 预算耗尽型翻转。
- ⇒ 两条红从「r1+r2」换成「r1+m1」，**通过率数字相同但构成不同**。抽样非确定性的又一次实证；按 046 纪律**不据此下机制结论、不为分数调机制/题目/口径**。

**两条红与 062 零因果路径（确定性归因，非统计推断）**：

- `r1-research-report`（q2→**3**，跨过及格线，fails 只剩 `verify exit 1`；`guards` 由 `['plan-scope']` 变 `[]`；`calls` 7→7）：step2/4/5 全 failed，note 逐字「工具预算耗尽：仅拿到 9/19、9/22、9/24、9/25、9/26 的零散两地报价…」「报告未成形，未写入任何笔记」，**全程未调 `finish_plan`** ⇒ 回验没有执行路径。归因＝活清单「`max_tool_rounds=5` 与多步计划抢预算」条的又一次样本（本轮 `guards=[]` ⇒ 不是范围闸，是纯预算；与 061 那轮 `['plan-scope']` 的病因不同）。
- `m1-plan-failure-dedup`：同上，**未调 `finish_plan`** ⇒ 零 062 执行路径。
- ⇒ 全 16 条里 `finish_plan` 只出现 **2 次**（r2、r4 各一次），且两次都**无弹窗、都归档成功、质量都 5/5**。

**「回验真被调用」的方向性信号（n=2，不是证明）**：`llm_calls` 的 +1 恰好落在**唯一两个调用过 `finish_plan` 的场景**上（r2 13→14、r4 6→7），而两个建了计划但**未收官**的场景（r1、m1）各 +0。不能算证明——账本是主+子 agent 合并计数，且 r2/r4 两轮的工具轨迹形状本身有变；其余场景的 ±1（i4 4→3、i6 9→7、i1h 2→3、r6 5→6）说明单轮抖动本就有 ±1 量级。总成本 +5% 同样**不可归因本刀**：r2 一条就 +¥0.0216（0.0519→0.0735），已超过全轮总增量 +¥0.0209，而 r2 的涨在它自己变长的轨迹上。

**遗留 2 的现状（收尾段被拒 ⇒ 板子挂 active）**：本轮**未触发**（零次 `finish_plan` 弹窗 ⇒ 零次收尾段拒绝），风险面仍在，触发信号不变。
