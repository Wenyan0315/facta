# 059 spawn 事件缝透传：子 agent 过程以 `sub.*` 进父事件流

> 状态：**已落地（2026-09-28）**｜日期：2026-09-28
> 授权出处：用户 2026-09-28「按照你的计划依次进行」（批准甲 057 → 乙 058 → 丙本案的三步排期）
> 立项出处：[architecture.md 活清单](../architecture.md)「**`_full_arm` 事件流不含子 agent（产品侧同源缺陷）**」条的触发信号原文——「触发信号=任务视图要展示子过程，或**下一次 spawn 相关改动顺手补透传**」；后半句在 [057](057-plan-tool-scope.md) 动 `spawn._worktree_registry` 时已具备条件，本案是把它兑现
> 一句话结论：**隔离的是主 agent 的上下文（底片），不是人的眼睛**——子事件走 `sub.*` 点分命名空间，人看得见委派出去的过程，而 checkpoint 账本、冻结集评测轨迹、前端订阅三处口径**逐字不变**（三个消费方都是精确匹配 / 白名单，前缀即隔离）。

## 背景与动机

[033](033-s6c-orchestration.md)/[030](030-s6a-worktree-isolation.md) 把 spawn 建成「噪声隔离」的委派通道：子 agent 的几十次工具调用不进主底片，主 agent 只收一条 tool 消息（结论）。这个隔离对**模型**是对的（主上下文不被过程垃圾撑爆），但当时的实现把同一刀也砍在**人**身上：

`spawn.py` 的 `run_turn(...)` 只传 `on_confirm`、**不传 `on_event`** ⇒ 子 agent 的工具过程零事件外发，只落审计日志（子 registry 与主 registry 审计同源，`spawn.py:133`）。后果分两侧：

- **评测侧**：`_full_arm` 的轨迹看不见委派过程，[049](049-p0-8-injection-hardening.md) 时代为此把 `_trace` 改成读副本审计日志绕过（`frozen_eval.py:343`）。
- **产品侧（未修，本案）**：任务视图同样看不见。AgentTeams 调研那句「隔离的是主 agent 上下文，不是人的眼睛」只兑现了一半——[architecture.md「多进程演进」条](../architecture.md)里它的原始语境是「子过程折叠可见」，而折叠的前提是先能收到。

人委派出去一段工作却看不到它在干什么，是**信任面的洞**：spawn 越多、主回答越像「凭空出现」。补透传的成本是一条缝（registry 已有同款 `receives_confirm` 可复用），不补的成本是每次排查子 agent 行为都要去翻审计日志。

## 地面真值（写代码前核对，全部读码取证）

1. **registry 已有一条注入通道可复用**：`Tool.receives_confirm`（[registry.py:107](../../src/agent/tools/registry.py)）+ `execute` 内 `if tool.receives_confirm: extra["confirm"] = confirm`（`registry.py:231`）。S5c 立的规矩是**显式声明而非 registry 隐藏状态**——func 签名要不要多个参数由工具自己 declare。
2. **`receives_confirm` 只有 spawn 两件在用**（`spawn.py:301` `spawn_subagent`、`spawn.py:358` `spawn_step`）⇒ 事件缝**不能 piggyback 在 confirm 通道上**：那会让「标了确认的工具」自动收到 `event` 参数，给未来任何一个 `needs_confirmation` 工具埋 `TypeError` 陷阱。
3. **`Agent.execute` 是 loop 与 registry 之间的唯一通道**（[agent.py:103-119](../../src/agent/orchestrator/agent.py)），confirm 也是这么透传的 ⇒ 加 `on_event` 形参是同一形状。
4. **loop 侧只有两处调用点**：`_run_parallel` 的 `ex.submit`（`loop.py:359`）与串行批（`loop.py:407`）。
5. **`run_turn` 只发五种事件**（Grep `on_event(` 穷举）：`tool_started`/`tool_result`（`loop.py:396`/`426`）、`stuck`（`611`）、`max_rounds`（`638`）、`error`（`650`）⇒ **子事件命名空间五项即穷尽**，不需要通配设计。
6. **server 侧未知事件类型原样透传**：`app.py:204` `run.emit(_EVENT_MAP.get(type_, type_), data)`，而 `_EVENT_MAP`（`app.py:51-57`）只列了五个下划线名 ⇒ 点分名的 `sub.*` **零改动**即可达 SSE 与 Run Store（`plan.*` 当初就是这么白送的，`loop.py:423` 注释已记此约定）。
7. **CheckpointWriter 只认精确类型**：[checkpoint.py:145-161](../../src/agent/orchestrator/checkpoint.py) 是 `type_ == "tool_started"` / `== "tool_result"` / `startswith("plan.")` ⇒ **`sub.` 前缀的事件既不写账本、也不触发底片落盘**。这是本案安全性的第一根柱子：子过程进不了恢复语义。
8. **冻结集 harness 同样只精确匹配**：`frozen_eval.py:224` `if type_ == "tool_started"` ⇒ **评测轨迹口径逐字不变**（[046](046-frozen-real-task-eval.md) 纪律：不为改动调评测口径）。`_trace` 读副本审计日志那条绕过路径继续有效、不必回退。
9. **前端 v2 的订阅是白名单**：[RunDetail.jsx:86-90](../../frontend/src/tasks/RunDetail.jsx) `TYPES` 九个类型逐个 `addEventListener`，且 `RunDetail.jsx:67` 注明「`tool_*`/`text.*` 等先收集不展示」⇒ 未订阅 `sub.*` **既不会崩也不会渲染**（零回归），同时意味着**本案在前端暂无可见收益**（见反方 1）。
10. **CLI 侧同理**：`cli.py:40-62` 的 `_cli_on_event` 是 if/elif 链，未知类型静默落空。
11. **跨线程 emit 今天已存在**：`run_store.py` 的 `request_confirm` 就在 worker 线程内 emit。但 `_next_seq` 是 `self._seq += 1`（`run_store.py:86`）**非原子**，而 059 起并行 spawn 的多个 worker 会**常态**并发 emit ⇒ seq 重号（seq 是排序/去重/重放的位置键，重号＝客户端可能漏读一条）。这是本案唯一一处「顺手修的既有根因」。
12. **子 agent 没有计划工具**：`_FORBIDDEN`（`spawn.py:67`）含 `make_plan`/`update_plan_step`/`finish_plan` ⇒ **不会出现 `sub.plan.*`**，命名空间无需覆盖 plan 族。
13. **既有测试钉住的是旧行为**：`test_smoke.py` 的 `test_smoke_spawn_through_sse` 原注释写「spawn 的 run_turn 不传 on_event…这是设计保证」⇒ 本案**必然要改一条既有测试**（与 057「一条不改」相反，因为 057 是纯加法，本案是**语义收窄**：噪声隔离的定义从「上下文 + 事件流」收窄为「上下文」）。

## 选项

- **甲（piggyback 在 `receives_confirm` 上）**：给标了确认的工具多注入一个 `event` 参数。
- **乙（独立 `receives_event` flag）**：与 confirm 同款形状，新增一个声明位，registry 注入 kwarg 名 `event`。
- **丙（子 agent 自己写 Run Store）**：spawn 拿到 run 对象直接 emit，不经事件缝。

## 拍板

1. **采用乙，否决甲与丙。** 甲的问题见地面真值 2（给未来工具埋 `TypeError`）。丙的问题是**层次颠倒**：工具层不该知道 Run Store 的存在（tools 不 import server，依赖方向纪律），且绕过事件缝就绕过了 checkpoint writer 的过滤——子事件会直接进账本，本案的安全论证（拍板 3）当场失效。乙的成本是一个 bool 字段 + 两行注入，与 S5c 的「显式声明」口径一致。
2. **命名空间用点分 `sub.` 前缀，不用下划线。** 与 `plan.*` 同风格 ⇒ 地面真值 6 的透传白送；同时**前缀即隔离**：三个消费方（checkpoint / frozen_eval / 前端 TYPES）都按精确名匹配，一个前缀就让它们全部自动豁免，不需要在任何一处加 `if not type_.startswith("sub.")`。
   五个映射固定为 `sub.tool.started`/`sub.tool.result`/`sub.stuck`/`sub.max_rounds`/`sub.error`（`_SUB_EVENT_MAP`），未知名兜底 `f"sub.{type_}"`——兜底不是为扩展性，是为了**将来 loop 新增事件类型时子侧不会静默丢**（丢事件比多发一种未知事件更难查）。
3. **子事件不进 checkpoint 账本，这是刻意的不是遗漏。** 底片是「主 agent 的上下文」，子过程本来就不该在里面（这正是 spawn 的立身之本）；账本要恢复的也是主 agent 的意图/结果配对（`tool_started` 的 `id` 与 `tool_result` 的 `id` 配对，checkpoint.py:135 注释）。子事件的 `id` 来自**子轮 LLM**，与父 tool_call id **不同域**，写进账本会造出配不上对的孤儿意图行。故 `sub.*` 只走「给人看」的三条路（SSE / Run Store / 审计），不走「给恢复用」的那条。
4. **`data` 加 `task` 摘要（前 60 字），且用副本不 mutate。** 并行 spawn 时多个 worker 的子事件会**交织**写进同一条父流（谁先跑完谁先到），没有 task 就无法区分是哪个兄弟。`{**data, "task": brief}` 而非原地改：data 由 loop 造出后会同时喂给多个消费者（RunStore、checkpoint writer、SSE 编码），在这条缝上加字段就用副本。
5. **`on_text` 不透传。** 子 agent 的流式正文不是给人看的成品（它会被主 agent 再加工一次），只回传结论这条边界不动；透传它会让父流里混进两路正文，SSE 的 `text.delta` 语义（「这是本轮回答」）当场歧义。
6. **`_sub_emitter(None, task)` 返回 `None`。** CLI/评测不关心事件时子 agent 也零开销，且 `run_turn` 的 `on_event=None` 分支本来就到处在做真值判断，不必为子侧造一个空函数。
7. **顺手修 `_next_seq` 的根因，不在调用侧加锁。** `RunStore.emit` 整体持新增的 `_emit_lock`（seq 分配 + `events.append` + 广播三步原子）。ponytail 的「修 bug 修根因不修症状：grep 所调用方，修共享函数一次」——若只在 spawn 侧加锁，`request_confirm` 那条既有的跨线程 emit 仍在锁外。锁序已核对：`request_confirm` 持 `_confirm_lock` 时调 emit ⇒ confirm→emit 单向，无反向路径，无死锁。
8. **前端与 CLI 一行不改**（地面真值 9/10）。本案的交付物是**事件流里有这个信息**，不是**界面上渲染它**——渲染属执行时间线，那条挂在 `RunDetail.jsx:67` 的触发信号上，届时是加 `TYPES` 两行 + 折叠 UI，与本案解耦。现在动前端等于给 v2 加一个还没设计过的视图（范围蔓延）。

## 判定标准（写代码前定）

1. 主 agent 调 `spawn_subagent`，父事件流里出现 `sub.tool.started`/`sub.tool.result`，`data["name"]` 是**子 agent 的工具名**、`data["task"]` 含子任务摘要。
2. 父命名空间不被污染：`tool_started`/`tool_result` 里只有 `spawn_subagent` 自己，没有子工具名。
3. 主底片不受影响：`session.messages` 里 tool 消息**仍只有 1 条**（噪声隔离的现行口径）。
4. `sub.*` 不进 checkpoint 账本、不触发底片落盘。
5. `receives_event` 通道与 `receives_confirm` 同款：标记的工具收到 `event`，**未标记的工具收不到**（不给未来工具埋 TypeError）。
6. `on_event=None` 时子侧零事件、不炸（CLI/评测路径）。
7. 冒烟：`sub.*` 经 SSE 端到端可达（走 `_full_arm` 那条真实链路，不是只测 spawn 函数）。
8. 冻结集评测轨迹口径不变：`_trace` 采到的序列里**依旧没有子过程**（`frozen_eval.py:224` 精确匹配）。
9. 三门全绿；既有测试允许改**一条**（`test_smoke_spawn_through_sse`，判定标准 3/7 的语义收窄所致），改动必须在本文档说明理由。

## 实现

`src/agent/tools/registry.py`：`Tool` 加 `receives_event: bool = False`；`execute` 加 `on_event` 形参，注入段与 confirm 并列（`extra["event"] = on_event`）。

`src/agent/orchestrator/agent.py`：`Agent.execute` 加 `on_event` 形参并透传给 `registry.execute`。

`src/agent/orchestrator/loop.py`：`_run_parallel` 加 `on_event` 形参、`ex.submit` 一并下发；`_execute_tool_calls` 的两个调用点（并行批 / 串行批）都传；`run_turn` docstring 的事件清单补 `sub.*` 一项（契约面文档）。

`src/agent/tools/spawn.py`：`_SUB_EVENT_MAP` + `_SUB_TASK_LEN = 60` + `_sub_emitter(on_event, task)`（改名 + 挂摘要的闭包工厂）；`spawn_subagent` 加 `on_event` 形参、`run_turn(...)` 传 `on_event=_sub_emitter(on_event, task)`；`_spawn`/`_spawn_step` 两个闭包加 `event=None` 并透传；两个 Tool 加 `receives_event=True`；模块 docstring 补「事件透传」段。

`src/agent/server/run_store.py`：加 `_emit_lock`，`emit` 整体持锁（拍板 7）。

`tests/`：`test_spawn.py` 新增 `test_sub_events_reach_parent_stream_namespaced`（判定 1/2/3）与 `test_registry_receives_event_channel`（判定 5）；`test_checkpoint.py` 新增 `test_writer_ignores_sub_events`（判定 4）；`test_smoke.py` 的 `test_smoke_spawn_through_sse` **语义翻转**（判定 7，见下）。

**改的那一条既有测试（判定标准 9 的诚实登记）**：`test_smoke_spawn_through_sse` 原先的断言是「子 agent 的 `search_notes` **不在**事件流里」，现在改成两条——`sub.tool.started` 里**有** `search_notes`（带 task 摘要），而父命名空间 `tool.started`/`tool.result` 里**没有**它。旧断言钉住的是「事件流隔离」，而 059 把噪声隔离的定义收窄为「上下文隔离」（主底片只多一条 tool 消息这条断言原样保留）。不翻转这条测试就无法落地本案，翻转的理由与新旧口径都写在测试 docstring 里。

**两处陈旧注释修正**（不属判定标准，属「改动波及的文档真值」）：`evals/frozen_eval.py:343` 的 `_trace` docstring 与 `tests/test_frozen_eval.py` 的同款文案，原写「子 agent 的事件不透传给父 `on_event`」已失效，改为「059 起以 `sub.*` 进父流，但 harness 采集口只认精确 `tool_started`（评测口径刻意不动）」。

三门：**ruff All checks passed｜mypy Success（59 files）｜pytest 682 passed, 2 skipped**（057 基线 679，净 **+3**）。

## 反方（预写）

- **「前端没订阅，人眼其实还是看不见，等于零收益。」** —— 成立，且是本案最主要的实际局限（拍板 8）。收益分两段：现在拿到的是**事件流里有这个信息**（SSE 抓包、`curl /api/runs/{id}/events`、Run Store 重放、将来任何新消费者都能读到），界面渲染要等执行时间线开工。选这个切法是因为**渲染是设计问题、透传是缺陷**——把缺陷修掉不需要先解决设计问题，反过来则会（先做 UI 再补透传＝UI 上线时是空的）。
- **「并行 spawn 时事件交织，读起来是一锅粥。」** —— 成立。`task` 摘要只能区分「是哪个兄弟」，不能重建「每个兄弟各自的时序」（没有 per-subagent 的子流 id）。要真解决得给每个子执行流一个 `sub_run_id` 并让前端分组折叠——那是执行时间线的需求，不是透传的需求。当前形状下人要看清单个兄弟，用非并行 spawn 或读审计日志。
- **「`_emit_lock` 是全局串行化点，会不会成为瓶颈？」** —— 不会。临界区只有三步内存操作（取号、append、往 N 个 queue put），没有 IO、没有 LLM 调用；相对一次工具执行的耗时是噪声级。真要有瓶颈，那说明事件量本身失控（该治的是发多少事件，不是锁）。
- **「子事件不进账本，那 spawn 到一半崩溃，恢复时子过程就丢了。」** —— 正确，且这是**期望行为**：恢复语义是「主 agent 从最后一个已配对的 tool_result 之后继续」，子 agent 的过程本来就不参与主 agent 的状态（它只回传一条结论字符串）。真丢的是「崩溃时那次委派做到哪了」——那属 spawn 自身的可恢复性（当前 spawn 不可恢复，崩了就整轮重来），与事件流无关，是另一张账单。
- **「这是为评测/演示改机制。」** —— 不成立。立项出处是活清单挂了 2 天的产品侧缺陷条，触发信号原文就写着「下一次 spawn 相关改动顺手补透传」；判定标准 8 反而**明确要求评测读数不变**，`_trace` 的采集口径一行未动。

## 不做（防范围蔓延）

- **不做前端执行时间线**：拍板 8，挂 `RunDetail.jsx:67` 既有触发信号。
- **不加 CLI 打印分支**：`_cli_on_event` 是给人看的排版，子过程在终端里逐行打会把主回答冲走；CLI 侧要看子过程用审计日志。
- **不让 `frozen_eval` 采集 `sub.*`**：会改评测轨迹口径 ⇒ 撞 046（须整轮重跑并与历史读数对照才有意义，那是独立一件事）。触发信号＝需要「委派过程」进评测归因时。
- **不给子事件加 `sub_run_id` / 分组语义**：见反方 2，属执行时间线的需求。
- **不透传 `on_text`**：拍板 5。
- **不动 `_FORBIDDEN`、不动 worktree 语义、不动 spawn 的返回契约**：本案只加一条缝。

## 遗留与触发信号

- **前端执行时间线开工时须吃掉本案**：`sub.*` 已在流里，届时是 `TYPES` 加两项 + 折叠渲染（`task` 摘要可直接当折叠标题）。触发信号＝`RunDetail.jsx:67` 那条（工具时间线）。
- **并行 spawn 的可读性**：实机出现「并行子事件交织到人无法排查」⇒ 修法是加 `sub_run_id` 分组（不是把并行改串行）。
- **`_emit_lock` 的正确性没有并发测试**：与 [039](039-s8a-session-model.md) 边界⑤同款裁定（不养 flaky），锁的必要性由「`_next_seq` 非原子 + 并行 spawn 常态跨线程 emit」的代码事实支撑，不由测试网支撑。触发信号＝实机出现 seq 重号 / 事件漏读。
- **子 agent 的 `stuck`/`max_rounds`/`error` 现在会冒到父流**：`sub.stuck` 意味着「子 agent 原地打转被熔断」，主 agent 自己**看不到**（它只收到结论字符串）。触发信号＝实机出现「子 agent 熔断但主 agent 照常汇报成功」，届时该议的是**熔断信息要不要回灌给主 agent**（那是 `spawn_subagent` 返回串的事，不是事件流的事）。
