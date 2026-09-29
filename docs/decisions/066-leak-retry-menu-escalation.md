# 066 DSML 泄漏重试升菜单：tools=None 的第二位置（M10 路由 direct 分支）

> 状态：**已批准（2026-09-29，用户拍板甲案「泄漏重试升菜单」+ hint 文案改动保留）**，同日落地｜日期：2026-09-29
> 立项出处：真实使用首例泄漏降级（会话 `20260929-095616.json`，2026-09-29 09:56「今天天气怎样」→「上海」3 连泄漏）；[manual-test-cases.md](../manual-test-cases.md) K 区把 DSML 泄漏登记为「根因层面是模型故障」——本案证明这个口径不完整
> 一句话结论：**056 立的「tools=None 时想调工具结构上不可能、重试必然全无效」不止收尾段一处——M10 路由把上下文依赖的跟话判成 direct 是同型洞的第二位置；修法＝泄漏重试时把 None 菜单升到全量，让 hint 指的「通过标准 tool_calls 字段发起」结构上成为可能。**

## 背景与动机

2026-09-29 09:56 真实使用首例（此前泄漏只出现在冻结评测，8 条全在收尾段）：用户问「今天天气怎样」→ agent 正常调 `web_search`（single_tool 路由）→ 返回两个错误城市并问「告诉我城市名」→ 用户答「上海」→ **3 连 DSML 泄漏（1 + 2 次重试）→ 047 降级告知**。用户拿到的就是那句「自动重试 2 次未恢复，请重新下达指令」。

056 的结论是「泄漏只发生在收尾段，从不在工具循环内」——依据是 17 条评测样本的 `llm_calls` 恒等式。本轮实机样本**推翻了它的普适性**：泄漏发生在轮首第一次调用（工具循环第 1 轮、预算全满），只是那一轮的菜单被 M10 路由判成了 direct（`tools=None`）——与收尾段共享同一个结构条件，位置不同。

## 地面真值（全部读码/实机取证）

1. **token 账目证 `tools=None`**：泄漏那次调用 prompt=2328 ≈ system + 历史、无 schema；同会话对照——single_tool 首调（1 个 schema）1708、全量菜单二调（~30 个 schema）8157。三个数互证。
2. **路由决策实锤两次**：路由器只看单条消息（`jev.py` route 的 state＝`用户对个人助手说：「上海」`，无上文）；2026-09-29 实机复现日志 `场景路由：direct（纯生成，不递菜单）`。单词城市名被判 direct 实属必然——它不知道上一句是「告诉我城市名」。
3. **056 机制原文适用**：`_merge_with_leak_guard` 的 while 里重投用的 `tools` 与首次相同；`_DSML_LEAK_HINT` 要求「通过标准 tool_calls 字段发起」——`tools=None` 时该字段结构上不存在，「出路不存在时重试多少次都不存在」。今早 3 连泄漏是这条的死锁复演，不是采样噪声。
4. **`tools=None` 的全部三处来源**：M10 路由 direct（本案）；`_close_out` 无活跃计划时 `closing=None`（056 原文「无活跃计划 ⇒ 照旧 tools=None」）；收官菜跑完后的 final 调用恒 None（撤菜单逼文字总结）。
5. **final 调用点不能升（实现期钉死）**：`_close_out` 尾部 `final` 的 tool_calls **没有执行路径**（直接入底片返回）——升菜单会让「模型又点菜」变孤儿 tool_calls（配不上工具结果，下次启动 API 400）。撤菜单 + `_MENU_GONE` 告知是 056 的成套设计，泄漏残余走 047 降级。
6. **泄漏路径零审计痕迹**：泄漏的调用从未执行、不落 `data/audit/`，只有会话文件里的降级告知可查——今早归因靠的是 token 账目而非审计行。登记为归因盲区，不预建仪器（触发信号见文末）。
7. **泄漏本身仍是随机的**：复现轮「上海」同样被判 direct 但没泄漏（上文已含天气，模型无需调工具）——结构条件确定，是否吐 markup 由模型侧决定。本案修的是结构死锁，不是消灭泄漏。

## 裁定（用户两项，AskUserQuestion）

1. **甲：泄漏重试升菜单**（否决乙「路由器带对话上文」——改 M10「路由器与会话无关」设计、每次路由多 token、Jev 是 LLM 误判仍会发生，结构洞不封；否决「只登记不修」——根因与修法都已明确）。
2. **hint 文案改动保留**（此前在错误假设下做的改动：去掉 `<｜｜DSML｜｜` 字面量换描述性指代——按 047 拍板 4「告知文案不得含标记字面量」的同款纪律，hint 是漏网的同款；纯文案卫生，与根因修复不冲突）。

## 设计

`_merge_with_leak_guard` 加 `fallback: list[dict] | None = None` 参数；检测到泄漏且 `tools is None` 时，重投前把菜单换成 fallback 全量。**只升 None**：

- 窄菜单（single_tool 收窄 / 056 收官菜）不升——已有 tools 字段承载点菜，收窄设计不被推翻；
- fallback 缺省＝旧行为（final 调用点依赖，见地面真值 5）；
- 空注册表自然不升（`schemas or None` 折叠，与「空菜单折叠回 None」的 API 纪律同源）。

三个调用点的落点：工具循环传 `full_tools`（轮首被路由判 direct 时生效——本案）；`_close_out` 调 1 传 `schemas or None`（无活跃计划时 `closing=None` 生效；有计划时 closing 非空不升）；final 调用**不传**（孤儿风险）。

正常路径零成本：升菜单只发生在泄漏重试时（罕见）；direct 流量的语义缓存命中区不受影响（首调仍 tools=None）。

## 反方意见（强制小节）

1. **「误判该修路由器（带上文），不该在重试里打补丁」**——部分成立：乙案治的是误判本身，本案治的是误判的后果。但 Jev 是 LLM choice，带上文后误判仍会发生（只是概率下降），而任何 tools=None 的调用电位都是泄漏形状；重试升菜单是结构性兜底，乙是准确率优化。乙挂触发信号（见文末），不否决只缓议。
2. **「收尾段 final 不升是漏修」**——不成立：升了会造孤儿 tool_calls（地面真值 5），那比泄漏更糟（污染底片、下次启动 400）。047 降级链兜住该处残余。
3. **「为 n=1 实机案例修机制，撞 046 纪律」**——不成立：046 禁的是「据单次出分调机制/题目/口径」；本案根因是 056 已立案的结构死锁换位置复发，证据三件（token 账目、路由日志、056 机制原文）互相咬合，修的是确定性结构不是统计噪声。
4. **「升菜单后模型点菜，direct 流量多付 schema token」**——只在泄漏重试时发生；不泄漏的 direct 轮一次都不多付。

## 验收标准与实现（2026-09-29 落地）

- `test_dsml_leak_with_none_menu_escalates_retry_menu`：direct 路由下泄漏 → 重试菜单非空 → 模型点菜 → 工具真执行（底片 assistant/tool 配对完整，无孤儿）。
- `test_dsml_leak_with_menu_retries_same_menu`：有菜单时泄漏重试沿用同一份菜单（只升 None）。
- `test_merge_with_leak_guard_without_fallback_keeps_none`：fallback 缺省＝旧行为（final 调用点语义）。
- `test_close_out_none_menu_leak_escalates_but_final_does_not`：收尾段菜单序列「全量 → None（无计划）→ 升全量 → None（final 不升）」，升级后点的菜真执行。
- 三门：ruff `All checks passed!`｜mypy `Success: no issues found in 59 source files`｜pytest **711 passed, 2 skipped**（基线 707，净 +4）。
- **附带修复（不同根因，同轮发现）**：`tests/test_learned.py` 的 `_client` 夹具漏隔离 user 桶——`/api/learned` 把 user 当第四个伪 category、路径走 `paths.user_memory_path()` 读真 home 文件，今早首条真实用户记忆（「用户所在地是上海」，复现测试的 `/new` 固化产物）落盘后 `test_api_roundtrip_edit_and_delete` 翻红。修法＝夹具补 `CORTEX_USER_MEMORY` 注入（041 的现成注入点，同文件其他测试已有惯例）。这也是 041 立过的隐患形态（「测试覆写 env 而面板仍盯着真 home 的隐私文件」）的实爆。
- 手动用例清单 [manual-test-cases.md](../manual-test-cases.md) K 区首条口径更新：「根因层面是模型故障」→ 结构性根因已修两处（056 收尾段 / 066 路由 direct + 收尾无计划），模型侧残余随机泄漏由 047 salvage 兜底。

## 触发信号与边界

- 路由 direct 下泄漏仍出现**且升菜单后重试仍 3 连失败** → 乙案（路由带上文）重开。
- 需要泄漏的归因仪器时（如再次出现需审计取证泄漏）→ 把泄漏事件落审计（现零痕迹，靠会话文件）。
- final 调用点若实机出现泄漏降级 → 届时议「final 的 tool_calls 给执行路径」而不是给它菜单。
- 不做：路由带上文（乙案缓议）、提高重试上限（047 已否）、DSML 解析补执行（047 丙案已否）。
