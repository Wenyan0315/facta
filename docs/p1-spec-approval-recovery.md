# Facta 2.0 P1 Spec：审批恢复与任务推送

> 状态=待评审 · 日期 2026-11-28 · 依赖：[Spec 1 任务工作区绑定](p0-spec-workspace-binding.md)（操作归属）；推送游标部分可独立先行 · 关联：[研发落地计划](m5-task-delivery-plan.md) · [外部评审](m5-external-review.md) · [ADR 054](decisions/054-confirm-approval-trace.md) · [ADR 117](decisions/117-m3-multitask-and-authorization.md) · [ADR 118](decisions/118-m4-host-and-release.md) · Spec 4 并发账单与任务预算（另文）

一句话：把审批从「Run 身上的一个布尔槽」升级为可持久化、按操作绑定、可幂等裁决、可编辑出新版本的审批项——审批挂起不占执行槽、重启可恢复、已决动作再现自动套用；同时给任务事件游标加总线实例代号，断线重连即时获得全量现状。

## 一、现状

### 已落地能力

- **单槽位确认。** Run.request_confirm 挂起（running→waiting_approval→running）+ confirm.request/confirm.resolved 事件 + 等待中取消视为拒绝；resolve_confirm 收布尔裁决，无 pending 返回 False；S6b `_confirm_lock` 串行化并行确认（[request_confirm](../src/facta/server/run_store.py#L227)、[resolve_confirm](../src/facta/server/run_store.py#L263)）。裁决端点 POST `/api/runs/{run_id}/confirm` 以 run_id 定位当前等待项，收一个布尔值（[ConfirmRequest](../src/facta/server/app.py#L301)、[confirm 端点](../src/facta/server/app.py#L1652)）。
- **任务级首写授权。** write 档技能的写两件改标 WRITE_AUTH_GUARD（kind="write-auth"），首写 approve 后置位 Task.authorized_write，同任务后续写免停（[skills.py](../src/facta/skills.py#L61)、[on_confirm 接线](../src/facta/server/app.py#L552)、[authorized_write](../src/facta/orchestrator/task.py#L117)）。
- **Run 落盘与重启收口。** RunLog 元快照 + 事件逐条落盘；非终态元快照重建时按 interrupted 终态收口（孤儿轮，不占准入槽）（[\_from_disk](../src/facta/server/run_store.py#L378)）；任务侧 requeue_on_boot 把 running 干净任务回落 queued 重入队（[requeue_on_boot](../src/facta/orchestrator/task.py#L455)）。
- **工具 Checkpoint。** {sid}.jsonl 记 intent/result，heal 把悬挂 tool_calls 补齐后从现场往前走——已成功的非幂等调用不重复执行，回注其原始结果（[checkpoint.py](../src/facta/orchestrator/checkpoint.py#L19)）。
- **任务级推送。** TaskBus 全局 seq + 环形缓冲 500，断层回全量快照自愈（[TaskBus](../src/facta/server/run_store.py#L422)、[since](../src/facta/server/run_store.py#L468)）；task_event_stream 快照/续传二选一 + 实时增量 + seq 去重（[task_event_stream](../src/facta/server/app.py#L130)）；GET `/api/tasks/events` 解析 Last-Event-ID（[task_events](../src/facta/server/app.py#L1858)）；前端 EventSource + 60s 轮询兜底（[App.jsx](../frontend/src/tasks/App.jsx#L75)）。

### 已复现问题

外部评审已复现并定位（[外部评审](m5-external-review.md#p1-审批续传和预算)）：

1. **审批不绑操作。** 审批接口以 run_id 定位当前等待项、收一个布尔值；旧弹窗的重复请求可能落到该 Run 的后一项审批，等待中的 worker 继续占槽（`_IN_FLIGHT` 把 waiting_approval 算在跑，[run_store.py](../src/facta/server/run_store.py#L47)）。
2. **跨代际游标断层。** 已复现：客户端带旧游标 `300` 连到序号为 `1` 的新总线，首帧是心跳，快照和新增事件都未送达。机制：`since(300)` 在新总线上不触发断层判定（`301 < buf[0].seq=1` 不成立）返回空列表，task_event_stream 判「已续传」不发快照，实时段又按 `ev.seq <= last` 去重全部跳过——两条路都断。前端靠重连拉取与定时轮询兜底才没丢列表（[since](../src/facta/server/run_store.py#L468)、[去重](../src/facta/server/app.py#L166)、[前端兜底](../frontend/src/tasks/App.jsx#L86)）。

## 二、行为契约

### 2.1 审批项（ApprovalItem）与持久化

- 每个待裁决的写入操作必须建一个审批项，字段：`id`、`task_id`、`run_id`、`tool`、`arguments`、`target`、状态（pending / approved / rejected / superseded / unknown_result）、裁决、执行结果、时间戳、版本链（`supersedes` / `superseded_by`）。`arguments` 是工具参数快照，`target` 是操作作用目标（文件路径/命令等，由工具语义给出）——二者是幂等比对与编辑版本链的基准。
- 审批项必须落盘单一 JSON 快照（data/approvals.json 级别，线程锁 + tmp/os.replace 原子写，TaskStore 同款），重启存活。
- 现有单槽位 confirm_pending 机制（[request_confirm](../src/facta/server/run_store.py#L227) / [resolve_confirm](../src/facta/server/run_store.py#L263)）升级为按审批项裁决：挂起 = 创建 pending 项并阻塞等待**该项**裁决；裁决 = 按审批项 id 落子。裁决通道与审批项一一对应，消除「重复请求落到后一项」的串位面。

### 2.2 操作 ID 绑定与幂等裁决

- 审批项绑定（run_id, tool, arguments, target）；裁决请求必须携带审批项 id。
- 同一审批 ID 的重复裁决请求：返回原裁决，不重复执行。裁决不一致的重复请求（先批后拒）以首次落子为准，返回原裁决并注明已裁决。
- 对已决项带着不一致参数/目标来的裁决请求：必须明确拒绝（错误串指明与原项的差异），不得执行、不得改写原项。
- 旧的布尔裁决端点（POST `/api/runs/{run_id}/confirm`）保持兼容：映射到该 Run 当前 pending 项；无 pending 项时维持现状返回。

### 2.3 参数/目标编辑（版本链）

- 用户编辑参数或目标后不得覆盖历史：原项置 superseded（记 `superseded_by`），生成新的待审版本（新 id、`supersedes` 链回原项），重新走裁决。裁决与执行结果一律留在原项上。
- 与线五③ interrupt 参数编辑的合流点：本期做「编辑生成新待审版本」；「编辑后恢复不重跑已完成工作」按 2.5 的审批项幂等（同参自动套用原裁决）实现。

### 2.4 审批挂起不占执行槽

- waiting_approval 的 Run 不计入 in_flight 准入计数（[create_if_idle](../src/facta/server/run_store.py#L321) 全局上限条）——审批挂起不占槽，其他 Run/任务照常准入。[RunStore](../src/facta/server/run_store.py#L272) max_in_flight 语义补注。
- 会话互斥口径不变：同一会话仍只允许一个在飞 Run（session.messages 共享可变列表的正确性约束不因审批放松）。
- in_flight_count 的账单近似标注口径不动（归因正确性归 Spec 4）。

### 2.5 重启恢复与自动套用

- pending 项重启后存活、可查、可裁决（快照即真值源）。
- 在飞 Run 走既有孤儿轮 interrupted 终态语义（不假装在跑，[\_from_disk](../src/facta/server/run_store.py#L378)）。
- 任务 resume 时：未决项重新呈现等待裁决；已决未执行的同一（tool, arguments, target）再次出现时自动套用原裁决（幂等，不重跑已完成工作），执行结果回填审批项，可恢复查询。
- 已批准但执行结果未知的项：标 unknown_result「结果未知，需核验」，不自动执行，先进核验路径（保守方向与 checkpoint「发起过、结果未知」档一致）。

### 2.6 推送游标（gen:seq）

- 游标 = 总线实例代号 + 序号（`gen:seq`，gen 在 TaskBus 启动时生成）；SSE 帧 id 按此编码。
- Last-Event-ID 两种格式都必须解析：`gen:seq` 与纯数字旧格式。
- 旧格式纯数字、未知代号（跨代际）、超前游标（序号超前当前总线）：一律回全量快照后转增量。断线重连即时获得现状。
- since() 判定补两个条件：代号一致且缓冲覆盖；任一不满足 → 快照自愈（断层语义沿用）。本修复可先于审批实施（见实施顺序第 1 步）。

## 三、状态与数据变更

- **ApprovalItem 实体 + 快照文件**：data/approvals.json（paths.py 增常量，DATA_ROOT 族同款），字段见 2.1；线程锁 + 原子写。
- **Run 侧**：confirm_pending 单槽位升级为「当前挂起审批项 id」；request_confirm 建项挂起、resolve_confirm 按项裁决（旧布尔签名保留兼容层）。confirm.request 事件载荷增 approval_id / target（加法键，旧消费方忽略）。
- **API 面**：按审批项裁决端点、编辑端点（生成新待审版本）、查询端点（按 task/run 过滤）——路径与载荷形状在实施第 3 步收口，不预膨胀；旧布尔端点保持。
- **TaskBus**：增 gen 实例代号（启动生成）；since() 收游标（代号 + 序号）；[task_event_stream](../src/facta/server/app.py#L130) 的快照/续传判定改走新游标。
- **准入口径**：[\_IN_FLIGHT](../src/facta/server/run_store.py#L47) 注释补注——waiting_approval 不计入全局准入计数；同会话互斥条不变。

## 四、兼容规则

- **聊天端确认弹窗与技能任务级首写授权语义不变**：confirm.request / confirm.resolved 事件、kind=write-auth 文案、Task.authorized_write 一次性写授权含义均不动。作用域注记：持久化与恢复承诺本期覆盖后台任务的写入审批；聊天端确认走同一裁决通道，恢复语义不承诺（聊天 Run 重启即落 interrupted，无 resume 通道）。
- **旧布尔裁决端点**：映射到该 Run 当前 pending 项，旧前端零改动。
- **旧事件/旧 API 消费方不受影响**：事件 type 不变，新键一律加法。
- **旧游标连接**：纯数字 Last-Event-ID 回全量快照，不断流、不静默丢件。
- **维持现状**：per-Run SSE（`/api/runs/{run_id}/events`）重放语义、60s 轮询兜底、TaskBus 环形缓冲 500 与快照自愈均不变。

## 五、失败处理

| 失败场景 | 表现 | 处置 |
|---|---|---|
| 同一审批 ID 重复裁决 | 返回原裁决，不重复执行 | 幂等；裁决事件留痕 |
| 已决项收到不一致参数/目标的裁决 | 明确拒绝，指明与原项差异 | 不执行不改写；改参数走编辑生成新版本 |
| 用户编辑参数/目标 | 原项 superseded，新项待审 | 重新走裁决，版本链可溯 |
| 重启时存在 pending 项 | 项存活可查可裁决，在飞 Run 落 interrupted | resume 时重新呈现等待裁决 |
| 已批准但执行结果未知 | 标 unknown_result「结果未知，需核验」 | 不自动执行，先进核验路径 |
| 等待裁决期间取消任务 | 等待中取消视为拒绝（现状语义） | 挂起不挡取消通道 |
| approvals 快照损坏（半截 JSON） | 启动明确报错，保留现场 | 不静默清空；人工处置后重启 |
| 旧格式/未知代号/超前游标 | 回全量快照后转增量 | 断线重连即时获得现状 |
| 环形缓冲断层（缺件被挤出） | 快照自愈 | 现状语义沿用 |

## 六、测试矩阵

沿 [tests/test_task_feed.py](../tests/test_task_feed.py) 惯例（TaskBus 离线帧语义 + 脚本模型）；审批落点新增 tests/test_approval_recovery.py，游标落点并入 tests/test_task_feed.py。

| 编号 | 场景 | 通过条件 | 测试落点 |
|---|---|---|---|
| A1 | 审批项持久化与重启存活 | 项落 data/approvals.json；重建 store 后可查、可裁决 | tests/test_approval_recovery.py |
| A2 | 重复裁决幂等 | 同 ID 重复裁决返回原裁决，执行不二次发生 | 同上 |
| A3 | 参数/目标不一致拒绝 | 已决项带异参裁决被拒，错误指明差异，原项不改写 | 同上 |
| A4 | 编辑生成新版本 | 原项 superseded、新项 pending 链回原项，重新走裁决 | 同上 |
| A5 | 审批挂起不占槽 | waiting_approval 时并发 Run 可起（准入通过），排队任务照常跑 | 同上 |
| A6 | 重启后恢复原审批 | pending 项重启后重新呈现等待裁决，裁决生效 | 同上 |
| A7 | 已决动作再现自动套用 | 同（tool, arguments, target）重现时套用原裁决、不重跑已完成工作，结果回填审批项 | 同上 |
| A8 | unknown_result 核验路径 | 已批准但结果未知的项标 unknown_result，不自动执行，走核验 | 同上 |
| D1 | 旧格式/跨代际游标 | 纯数字游标（含旧 300 连新总线 seq 1）回全量快照后转增量 | tests/test_task_feed.py |
| D2 | 未知代号/超前游标 | 未知 gen 或序号超前 → 回快照自愈 | 同上 |
| D3 | 断线重连 | 重连即时获得现状（快照+增量），Last-Event-ID 两种格式都解析 | 同上 |

前端审批面板（2.3 编辑 + 裁决 + 恢复呈现）以视图字段断言并入 A 系列落点；真机展示归 [tests/test_browser.py](../tests/test_browser.py) 惯例，不在本矩阵扩项。

## 七、实施顺序

1. **SSE 游标代号**（独立可先行，无依赖）。TaskBus.gen + 游标解析 + 回快照判定。涉及 [run_store.py](../src/facta/server/run_store.py)、[app.py](../src/facta/server/app.py)。测试：D1-D3。
2. **审批项与持久化原语**（无依赖）。ApprovalItem + data/approvals.json 快照（线程锁/原子写）。涉及 [paths.py](../src/facta/paths.py)、审批 store（落点见分歧一）。测试：A1。
3. **裁决端点与幂等/编辑**（依赖 2）。按 ID 裁决 + 幂等返回原裁决 + 不一致拒绝 + 编辑版本链 + 旧布尔端点映射。涉及 [app.py](../src/facta/server/app.py)、[run_store.py](../src/facta/server/run_store.py)。测试：A2-A4。
4. **槽位释放**（依赖 2）。准入计数排除 waiting_approval + max_in_flight 语义补注。涉及 [run_store.py](../src/facta/server/run_store.py)。测试：A5。
5. **重启恢复与自动套用**（依赖 2、3）。启动加载、resume 重现、同参自动套用、结果回填、unknown_result。涉及 [app.py](../src/facta/server/app.py)、[run_store.py](../src/facta/server/run_store.py)。测试：A6-A8。
6. **前端审批面板**（依赖 3、5）。编辑参数 + 裁决 + 恢复呈现。涉及 [TaskCard.jsx](../frontend/src/tasks/TaskCard.jsx) 及任务面板。测试：视图字段断言并入 A 系列。

## 八、设计分歧与推荐

1. **审批项存 run_store 内嵌 vs 独立 store 文件** → 推荐独立快照文件（data/approvals.json）。重启恢复与任务数据解耦：审批是审计件，落盘时机、损坏处置、保留现场语义都与 Task 快照不同列；嵌进 run_store 的内存结构会让恢复逻辑与 Run 生命周期纠缠（Run 可被中断收口，审批项要活得比 Run 久）。
2. **编辑参数 = 新版本链 vs 原地改** → 推荐新版本链。审计可溯：谁在何时把参数改成了什么，链条上一目了然；原地改会把「用户批准过的参数」和「后来悄悄换掉的参数」混成一条记录，幂等比对基准也随之漂移。
3. **自动套用原裁决 vs 一律重新问** → 推荐同参自动套用 + 异参重新审。（tool, arguments, target) 相同 = 用户已裁决过的同一操作，套用原裁决既幂等又不重跑已完成工作；异参是新操作，重新审才不架空审批。一律重新问会把恢复路径变成弹窗轰炸，且与「编辑生成新版本」语义打架。
4. **游标方案：总线实例代号 + 序号 vs 跨重启持久递增游标** → 推荐代号 + 序号。持久递增要给每个事件落全局递增序号并落盘（存储与一致性成本高），而断层场景已由回快照自愈兜住；代号 + 序号改动面小，旧格式兼容只需解析层多一个分支。
