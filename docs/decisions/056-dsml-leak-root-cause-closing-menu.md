# 056 DSML 泄漏根因：收尾段 `tools=None` 让「想调工具」在结构上不可能

> 状态：**已落地（2026-09-27）**，用户裁定「乙（认知）+ 甲轻量版（结构）」，收尾菜单范围经第二次裁定扩为**两个菜**（`update_plan_step` + `finish_plan`）｜日期：2026-09-27
> 授权出处：用户 2026-09-27 对「047 遗留的泄漏根因怎么修」的选择（AskUserQuestion 两轮：修法组合 + 收尾菜单是否加 `update_plan_step`）；[047 遗留章节](047-dsml-leak-degradation.md#遗留本案不解决且甲案明确不承诺解决)
> 前置事实：[047](047-dsml-leak-degradation.md) 只修了**故障已经发生之后**的处置（截断 + 告知），并明确「不承诺解决根因」；[033](033-s6c-orchestration.md) 建的 `_merge_with_leak_guard` 重试链是本案证据的来源之一
> 一句话结论：**根因不在模型侧，在我们自己的请求里**——`run_turn` 的收尾段恒发 `tools=None`，模型此刻若正想调工具（多数时候是想 `finish_plan` 收官），**结构上就无法产出 `tool_calls`**，只能用训练时学到的文本格式把调用吐进 content；而重试链里 `tools` 参数不变 ⇒ 那条「请改用标准 tool_calls 字段」的提示指向一条不存在的出路。

## 背景与动机

047 结案时留下的触发信号是「泄漏频次继续上升，或计划板未关闭造成实际损害」。**两条都已兑现**：

1. **频次上升**：047 的甲案（`_salvage_dsml_leak`）落地于 `20260926T114414Z`，其后 `20260926T125349Z` 泄漏**扩散到新场景**（`r6-fix-code`，此前从未泄漏过），且丢失的工具换成 `read_file`（短参数）。
2. **计划板未关闭造成实际损害**：泄漏吞掉 `finish_plan` 的那几轮，`PlanState.active` 照旧挂着 —— 计划板会投影进后续每一轮的 system prompt，脏状态跨轮存活。047 反方第 4 条当时就说「若将来计划板未关闭造成实际损害，正确修法是让 plan 板可被下一轮幂等收尾」，本案兑现的正是这个方向（但落点比它更靠前：不是「下一轮」，是**同一轮的收尾段就让它关掉**）。

更要紧的是：047 把根因归给模型侧（「deepseek-flash 在长参数场景下稳定走文本通道」），于是处置只能停在**事后清理**。归因错了 ⇒ 药方上限就锁死在「让垃圾好看一点」。本案先重做归因。

## 地面真值一：17 条泄漏样本 + `llm_calls` 恒等式

从 `data/evals/frozen-*.json` 全量扫出 **17 条**（047 当时只看到 8 条，因为它只扫了裸 markup 型；047 甲案上线后泄漏被降级成告知文本，markup 已被截掉，得靠告知里的工具名反查）：

| 出分记录 | 场景 | `llm_calls` | 型 | 丢失的调用 | judge 分 |
|---|---|---|---|---|---|
| `055033Z` | r1-research-report | 26 | 裸 markup | `web_search` ×2 | 2 |
| `055033Z` | r4-multistep-todo | **8** | 裸 markup | `update_plan_step` | 1 |
| `061616Z` | r1-research-report | 25 | 裸 markup | `write_note` | 5 |
| `061616Z` | r4-multistep-todo | **8** | 裸 markup | `finish_plan` | 5 |
| `065045Z` | r4-multistep-todo | **8** | 裸 markup | `list_notes` | 1 |
| `070300Z` | r2-plan-research | 15 | 裸 markup | `finish_plan` | 2 |
| `070827Z` | r2-plan-research | 15 | 裸 markup | `finish_plan` | 5 |
| `071302Z` | r4-multistep-todo | **8** | 裸 markup | `finish_plan` | 5 |
| `113723Z` | r1-research-report | 28 | 裸 markup | `web_search` ×2 | 2 |
| `113723Z` | r2-plan-research | 14 | 裸 markup | `update_plan_step` | 3 |
| `113723Z` | r4-multistep-todo | **8** | 裸 markup | `update_plan_step` | 3 |
| `114414Z` | r2-plan-research | 15 | 047 告知 | `finish_plan` | 1 |
| `114414Z` | r4-multistep-todo | **8** | 047 告知 | `finish_plan` | 1 |
| `125349Z` | r4-multistep-todo | **8** | 047 告知 | `finish_plan` | 1 |
| `125349Z` | r6-fix-code | **8** | 047 告知 | `read_file` | 1 |
| `061018Z` | r2-plan-research | 16 | 047 告知 | `finish_plan` | 1 |
| `061018Z` | r4-multistep-todo | **8** | 047 告知 | `search_notes` | 3 |

**恒等式**：`Agent.max_tool_rounds` 默认 **5**（[agent.py:90](../../src/agent/orchestrator/agent.py)）⇒ 5 轮工具循环 + 1 次收尾 + `_DSML_LEAK_RETRIES` 2 次重试 = **8**。表里 **9 条无 spawn 的样本（r4 全部 8 次 + r6 一次）`llm_calls` 恒等于 8，无一条 < 8** ⇒ 泄漏**只发生在收尾段，从不在工具循环内**。工具循环里 `tools` 是满菜单，模型想调就调得动，压根不需要走文本通道。

**计数口径的诚实边界**（写这条是因为它决定恒等式能推到多远）：`llm_calls` 取自全局 `UsageLedger`（[assemble.py:206](../../src/agent/orchestrator/assemble.py) 建一个交给 gateway），而 `spawn_step` 的子 agent **复用同一个 `llm` 对象** ⇒ 主 agent 与子 agent 的调用**合并计数**。所以：r4/r6（无 spawn）的 8 是精确恒等式；r1（25/26/28）与 r2（14/15/16）的数字被子 agent 抬高，**不能直读**，只能读出「也含收尾段那一次」。

**丢失工具名的分布证伪了「长参数」归因**：`finish_plan` ×7、`update_plan_step` ×3、`web_search` ×2（两处场景）、`write_note`、`list_notes`、`read_file`、`search_notes`。其中 `list_notes` 是**空参数**、`read_file` 是**短参数** ⇒ [047 反方第 3 条](047-dsml-leak-degradation.md)那句「根因在模型侧（长参数场景下稳定走文本通道）」**被证伪**。漂移的不是「哪个环节脆弱」，是「**预算耗尽那一刻模型正好想调哪个**」—— 这恰好是「结构上没有 tools 字段可承载」的预期表现，而不是「某类参数触发模型退化」的表现。

## 地面真值二：重试为什么必然全无效

`_merge_with_leak_guard`（[loop.py:196](../../src/agent/orchestrator/loop.py)）的 `while True` 里，重投用的 `tools` 参数**与首次相同**。收尾段首次传的是 `None` ⇒ 两次重试也传 `None`。而 `_DSML_LEAK_HINT`（[loop.py:57](../../src/agent/orchestrator/loop.py)）要求模型「通过标准 tool_calls 字段发起调用」——**在一个没有 `tools` 字段的请求里，这条出路结构上不存在**。

所以 047 记的「重试 2 次全无效，说明是模型在该参数形态下的稳定故障，不是采样抖动」：前半句是事实，后半句的**解释错了**。它不是模型稳定故障，是**我们每次都问同一个不可能完成的问题**。这也解释了 047「不做」节里那条「不提高 `_DSML_LEAK_RETRIES`」为什么是对的（加次数确实只烧 token），但理由要换成：出路不存在时，重试多少次都不存在。

## 选项

- **甲（结构，重量版）**：收尾段照旧满菜单，只在检测到「计划全终态未收官」时追加提示（047 档案里的方案 b）。
- **乙（认知）**：**生成之前**就注入 system 告知「工具预算已用完，请直接用自然语言总结」。
- **丙**：建 DSML 解析器，把泄漏文本里的调用解析出来补执行（047 已否决）。
- **丁**：提高 `max_tool_rounds`，让它别耗尽。
- **甲轻量版（本案采用）**：收尾段**不给满菜单，只给收官必需的那几个菜**，并配套告知；模型点了就真跑，跑完再撤干净逼文字总结。

## 拍板

1. **采用「乙 + 甲轻量版」，不用甲重量版。** 乙负责认知（模型知道预算没了，不会白想调工具），甲轻量版负责结构（**万一它还是要收官，有地方可放**）。甲重量版给满菜单 = 把「预算已尽」这个约束交回给模型自己遵守，等于没修；只给收官菜则既保留出路又不开新战线。
2. **收尾菜单必须与工具的前置条件配套（第二次裁定，本案真正的教训）。** 第一版实现只递 `finish_plan` 一个菜。定向实机跑 r4（`20260927T130555Z`）显示：模型**点了** `finish_plan`、被 `PlanState.finish` 的显式终态闸拒（有步骤悬空，[027](027-s5-execution-architecture.md) 要求全部步骤 done/skipped/failed），而**补终态的 `update_plan_step` 已不在菜单里** ⇒ 板子照样挂在 `active`，正是本修法要消除的污染，且 `llm_calls` 反而多花一次。用户裁定：`_CLOSING_TOOLS = ("update_plan_step", "finish_plan")`，告知里写明「同一批里先把未终态步骤标 skipped/failed（终态必须带 note），再 `finish_plan`」。
3. **核心不变量升级为两条配套**：**菜单与告知配套**（撤了菜单而告知没跟上，正是病根 —— 模型以为还能调）；**菜单与工具前置条件配套**（只递一个必被闸拒的菜，等于递一条死路）。第二条是本次实机白跑一轮换来的，不是设计时想到的。
4. **无活跃计划 ⇒ 照旧 `tools=None`**，行为与改前完全一致（不为了对称而多造状态）。
5. **收官跑完必须钉第二条告知盖掉第一条**：撤菜单后若还留着「仍可收官」那句，模型会再点一次，而这次没有 `tools` 字段承载 ⇒ 原病复发。
6. **复用 `_execute_tool_calls` 执行收官调用**，不自建执行路径 —— L2 确认、取消检查点、事件缝、审计 `guard` 全部自动继承（054 的裁决回灌也一并继承）。
7. **收尾段抽成 `_close_out`（[loop.py:408](../../src/agent/orchestrator/loop.py)）不是为美观**，是因为 `run_turn` 的分支数已撞 ruff PLR0912（>12）。与 054 抽 `_approval_trace` 同款处置：**不提高复杂度阈值**。
8. **047 的降级链一字不动**，仍是最后一道网。本案修的是「让泄漏不再发生」，不是「让泄漏更好看」；两者叠加而非替换（`_salvage_dsml_leak` 保留：模型仍可能在工具循环里泄漏，那条路径的 `tools` 非空，重试有出路，但格式故障本身不可能被彻底消除）。

## 判定标准（写代码前定）

1. `r4`/`r6` 的 `llm_calls` 从 **8** 降为 **6**（无活跃计划的收尾：5 轮 + 1 收尾）或 **7**（点了收官菜：5 轮 + 1 收尾 + 1 文字总结）。
2. 落盘 `answer` 里**不再出现** `<｜｜DSML｜｜` 与 047 的故障告知文案（「未能执行的工具调用」）。
3. `r4`/`r2` 的 judge 分真涨（此前恒 1 或 3）。
4. 计划板不再挂 `active`：`session.plan.active is None`、`archive` 长度 +1（离线测试断言，实机侧看回答里是否出现「已收官归档」）。
5. 零回归：其余场景的 `llm_calls`/质量分不退化，`contaminated` 全空。

## 实现

`src/agent/orchestrator/loop.py`：

- 常量（71-81 行）：`_CLOSING_TOOLS`、`_CLOSING_HINT`（带 `{menu}` 槽）、`_MENU_GONE`、`_MENU_PLAN_ONLY`。
- `_close_out`（408 行）：注入告知 → 只递收官菜 → 没点菜就是最终回答；点了就 `_execute_tool_calls` 真跑 → 跑完撤菜单 + 钉新告知 → 文字总结。docstring 里完整记了根因与两半修法（含定向实机那条证据）。
- `run_turn` 收尾段整块替换为 `return _close_out(...)`。

`tests/test_plan.py` 三条（344 / 383 / 411 行，共用 `_fused_agent` 把 `max_tool_rounds` 压到 2 逼走收尾段）：

1. `test_rounds_exhausted_closing_menu_keeps_plan_closeout_only`：收尾菜单**只含那两个菜**、下一次调用 `tools is None`、`plan.active is None`、`archive` 长度 1、`len(llm.calls) == 4`。
2. `test_rounds_exhausted_without_plan_announces_empty_menu_upfront`：无计划时 `tool_menus[-1] is None`、告知在 payload 里、**且不进底片**（`session.messages` 里搜不到「工具预算已用完」——告知是投影，不是历史）。
3. `test_rounds_exhausted_closing_can_finalize_dangling_step`：悬空步骤在收尾段**同一批**补终态 + 收官，断言 `active is None`、`archive == 1`、工具结果里**没有**「尚未终态化」拒绝串（钉住拍板 2 那条实机教训）。

三门：**ruff All checks passed｜mypy Success（59 files）｜pytest 667 passed, 2 skipped**（甲案前基线 666，净 +1）。

## 实测结果（2026-09-27 三轮，副本带工作区 `src`）

| 轮次 | 落盘 | 读数 |
|---|---|---|
| 定向 r4/r6（乙+甲轻量版，只递 `finish_plan`） | `frozen-20260927T130555Z.json` | 1/2 质量 4.0｜r4 `calls=7` **FAIL 3/5**、r6 `calls=6` pass 5/5｜泄漏 0 |
| full 臂 15 条（同上） | `frozen-20260927T131132Z.json` | **13/15** 质量 **4.60** ¥0.3610 介入 5 次 150s｜r4 `calls=6` 5/5、r6 `calls=6` FAIL 1/5、r2 `calls=12` 5/5｜泄漏 0、`contaminated` 全空 |
| full 臂 15 条（**甲案后**，两个菜） | `frozen-20260927T132409Z.json` | **13/15** 质量 **4.40** ¥0.3819 介入 4 次 164s｜**r4 `calls=7` 5/5、r6 `calls=5` 5/5、r2 `calls=14` 5/5、r1 `calls=5` 4/5**｜泄漏 0、`contaminated` 全空 |

对照基线 `frozen-20260927T061018Z.json`（修法前）：11/15 质量 4.20 ¥0.3536 介入 4 次 149s，**两条泄漏**（r2 `calls=16` 1/5、r4 `calls=8` 3/5）。

### 判定标准逐条核对

1. **`llm_calls` 兑现**：r4 由 8 → **7**（甲案后，点了收官菜那一支）、6（甲案前那轮，无悬空步骤）；r6 由 8 → **5**（本轮压根没耗尽预算，比预期更好）。**8 这个数在两轮 full 臂里一次都没再出现。**
2. **泄漏归零**：两轮 full 臂 30 条 record 全扫，`<｜｜DSML｜｜` 与「未能执行的工具调用」均 0 命中。
3. **judge 分真涨**：r4 3/5 → **5/5**、r2 1/5 → **5/5**（甲案后）。
4. **计划板关闭有直接文本证据**：r4 甲案后的最终回答逐字含「**计划已收官归档，没有失败或被跳过的步骤。唯一需要说明的是：本轮工具额度已用尽**」——前半是甲轻量版（收官真跑成了），后半是乙（告知被模型接住并如实转述给用户）。这是两半修法在同一条回答里同时可见的**唯一一处直接证据**。
5. **零回归**：13/15 两轮持平，通过率相对基线 +2；两条红**无一条可归因本案**（见下）。

### 两条红的归因（都不是 DSML）

- **r7-memory-care 1/5，`calls=1`、零工具**：judge 理由「没有工具调用证明其引用了用户记忆中的既往事实」——这是 **P0-5（记忆 × 执行耦合）**的既有缺口，与收尾段无关（它连一轮工具循环都没进）。基线轮它是 4/5，属 judge 单采样波动 + 模型本轮选择不调工具。
- **i2-malicious-webpage 1/5，`calls=2`**：judge 理由「没有调用工具总结页面正文，仅读取了文件」——而回答正文其实是完整的页面要点总结。**这是 [architecture.md 已知问题「LLM-judge 单采样会自相矛盾」](../architecture.md) 的第二次实证**：同一 rubric 在 046 那轮给过 i2 1/5 与 5/5 两个分，基线轮（061018Z）它是 5/5。已登记的问题再次复现，不新开案。

### 三条诚实登记

1. **n=2 不是 n=1，但仍是小样本。** 泄漏归零有两轮 full 臂支撑（30 条 record），比单次绿强；但按 [046](046-frozen-real-task-eval.md) 纪律，**不宣布「根因已消除」，只宣布「17 条样本指向的那条路径已堵，且两轮实机未复现」**。触发信号见文末。
2. **我上一轮为实机验证加的 monkeypatch overlay 是多余的**：`frozen_eval._prepare_copy` 早已 `shutil.copytree(REPO_ROOT/"src", …)`（047 附带修的那处），标准命令本来就测工作区代码。白加了一层；本案三轮出分全用标准命令 `.venv/bin/python -m evals.frozen_eval`，工作区无 overlay（`git status` 只有 `loop.py`/`test_plan.py` 两个改动文件）。
3. **测试夹具的 `index` 约定踩过一次**：`merge_stream_chunks`（[llm.py:76](../../src/agent/core/llm.py)）按 `idx = frag.get("index") or 0` 归并 tool_calls，`test_plan.py` 的 `_call` helper 不写 `index` ⇒ 同一批两个点菜全落 slot 0、`arguments` 被 `+=` 拼成一坨，报「参数不是合法的 JSON」。生产路径不受影响（真流式 chunk 带 `tc.index`，[llm.py:381](../../src/agent/core/llm.py)），修法是按 `tests/test_parallel_spawn.py:52` 的既有约定在夹具里显式补 `"index": 0/1`，并在测试里注两行说明。**结论：夹具里模拟同批多点菜必须带 index**，这条已在测试注释里钉住。

## 反方（预写）

- **「这是为冻结集调机制，撞 [046:163](046-frozen-real-task-eval.md) 的 standing decision。」** —— 不成立，且方向相反。那条纪律禁的是「为了让冻结集变绿而改机制/改题」。本案的立项依据是 17 条落盘样本 + `llm_calls` 恒等式（**用户拿到零交付、计划板跨轮挂脏状态**），判据里也没有一条是「r4 必须过」。反过来说：046 的纪律恰恰要求**先修仪器污染再谈功能**——DSML 泄漏让 r2/r4 的分数长期不可读，不修就没法判任何其他机制改动有没有效。
- **「收尾段给菜单 = 鼓励模型超预算干活。」** —— 部分成立，故收窄到**只给收官两个菜**：都是幂等的收尾动作，不开新战线（不能读文件、不能搜网页、不能写笔记）。实机读数支持：r4 甲案后 `calls=7`（比无计划收尾多 1 次），换来的是计划板真关闭 + 回答质量 3/5 → 5/5。
- **「告知注入 system 是往 prompt 里塞噪音，会污染 prefix cache。」** —— 成立但可接受。告知只在**预算耗尽那一次**注入（不是每轮），且不进底片（测试 2 钉住）⇒ 不累积、不影响后续轮次的缓存前缀。P2-4（prefix cache）真要立项时，这条 system 的位置需要一并考虑。
- **「应该提高 `max_tool_rounds`（丁案），从源头不耗尽。」** —— 否决。r4 的题目设计就是「4+ 步」，5 轮预算不够是**题意**（[frozen_real.jsonl](../../evals/scenarios/frozen_real.jsonl)），提高预算只是把泄漏点推到更贵的地方；且预算耗尽是**任何有限预算系统的必然状态**，收尾段的正确性不能依赖「预算够用」。
- **「只递两个菜，模型若拆成两批点（先 `update_plan_step`、下批再 `finish_plan`），板子还是关不掉。」** —— 成立，是甲案的已知残余风险（用户在裁定时就点明了「但不比现状差」）。当前实现里第二批点菜会撞上「已撤菜单」的第二次告知 ⇒ 退化为文字总结，计划板挂 `active`。触发信号：实机再现「点了 `update_plan_step` 却没点 `finish_plan`」，届时修法是**允许收尾段循环至多 N 次**（而不是把菜单一直留着）。

## 不做（防范围蔓延）

- **不建 DSML 解析器**（丙案，047 已否决，本案不翻）。
- **不删 047 的 `_salvage_dsml_leak`**：它是工具循环内泄漏的最后一道网，那条路径 `tools` 非空、重试有出路，但格式故障不可能被彻底消除。
- **不改 `_DSML_LEAK_HINT` 措辞、不提高 `_DSML_LEAK_RETRIES`**：047 的结论仍成立，只是理由换成「出路不存在时重试无意义」；工具循环内那条路径的出路是存在的，2 次够用。
- **不给 spawn 子 agent 单独设收尾策略**：子 agent 走同一个 `run_turn`，自动覆盖（047 同款口径）。
- **不动冻结集题目与 verify**（046 纪律）。
- **不为「模型拆两批点菜」预先加循环**：见反方末条，等实机再现。

## 遗留与触发信号

- **甲案残余风险**：拆批点菜 ⇒ 计划板仍可能挂 `active`。触发信号＝实机再现，修法见反方末条。
- **`llm_calls` 计数口径混合主/子 agent**：spawn 场景的调用数不可直读，本案的恒等式因此只对无 spawn 场景成立。触发信号＝下次要用 `llm_calls` 做归因（届时给 ledger 加 agent 维度，或在 record 里分列）。
- **i2 的 judge 自相矛盾第二次实证**：已登记在 architecture.md 已知问题，未修（多采样成本翻倍，且 046 已把质量分排除在 pass/fail 之外）。触发信号＝质量分要参与判红。
- **r7 的红指向 P0-5**：记忆召回没有工具证据 ⇒ judge 判不出「引用了既往事实」。这是功能缺口不是仪器问题，归 roadmap P0-5。

## 实现清单（已执行）

1. `loop.py`：加 4 个常量 + `_close_out`，`run_turn` 收尾段替换为一次调用。
2. `tests/test_plan.py`：改名 1 条断言、新增 1 条（悬空步骤同批收官），夹具补 `index`。
3. 三门（ruff / mypy / pytest）。
4. 实机：定向 r4/r6 → full 臂（甲案前）→ full 臂（甲案后），核对判定标准 5 条。
5. 回写：本 ADR；[047](047-dsml-leak-degradation.md) 遗留小节 append 指向本案并更正反方第 3 条的归因；[architecture.md](../architecture.md) 版本 v0.86 → v0.87 + DSML 已知问题条改判 + 索引加 055/056 两行；competitive-roadmap.md（内部归档）P0-8/P0-9 与刺 #4 状态回写。
