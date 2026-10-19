# ADR 115：M1 可控长任务——steering 打断 + 自动续跑 + Task 实体 + 任务分账

- 状态：**已过开工前评审、按意见修订，待实施**｜日期：2026-10-19
- 上游：[114](114-facta-2-roadmap.md)（2.0 路线图，M1 期定义）、
  [038](038-run-checkpoint.md)/[040](040-run-checkpoint-impl.md)（崩溃续跑通道）、
  [056](056-dsml-leak-root-cause-closing-menu.md)（预算耗尽收尾段）、
  [082](082-orchestrator-supplementary-three-items.md)（①取消缝）、
  [076](076-reflect-async.md)（收官异步化与 `_settle_done` 同步点）、
  [100](100-plan-authorized-rounds-budget.md)（计划授权扩容）、
  [111](111-v01-review-prototype.md)/[112](112-review-tool-rounds-budget.md)（预算先例与 Web 账单摩擦）、
  [110](110-product-validation-and-engineering-showcase.md)（V02）
- 调整关系：本案只落 114 的 **M1 期**；M2–M4 各自另案（编号顺延）。
  验收钉子照 114：一个 ≥1h 真实任务无人值守跑完；中途 steering 改向一次成功；
  任务级账单可查。

## 背景

现状盘点（读码核对）：

| 能力 | 现状 | 缺口 |
|---|---|---|
| 单轮执行 | `run_turn` 一轮一停，`RunResult` 三态 | 无「轮之外」的推进者 |
| 打断 | `should_cancel`（`loop.py` 三检查点），命中即 `trim_incomplete_round` 杀轮 | 只有「杀」，没有「纠偏」 |
| 崩溃续跑 | `heal()` + `user_text=None` 通道（038） | 通道在，但没人自动用它 |
| 任务实体 | `Run`（`server/run_store.py`）内存态、绑会话 | 无跨 Run 的长期实体、无状态机、无产物/分账 |
| 计划 | `session.plan`（`PlanBoard`；`view() -> Plan`，`Plan.is_complete()` 认 done/skipped/**failed** 为终态） | 未与调度器联动 |
| 账单 | `UsageLedger`（进程累计）+ `CallLedger`（逐笔） | 无 per-Run 切片、无任务聚合、Web 无出口（111 摩擦#2） |
| 确认缝 | `Run.request_confirm` 阻塞等裁决（`while True` + 0.2s 轮询，**无超时**） | 任务时长保险丝在挂起期间失效 |

**架构红线（114 复用）**：不重写内核。`run_turn` 的「一轮」语义、审计/确认/
沙箱/围栏/checkpoint 全部沿用。M1 的演进只发生在两处：**编排层新增 Task 与调度
循环**、**内核加一个 pull 缝（steering）**。

## 概念裁定

### 1 Task 一等实体（完整）

- **关系**：`Task 1 : 1 Session : N Run`。Task 是一段长期委派；Session 是它的
  上下文（沿用 038 底片与 checkpoint 恢复）；每个 Run = 一次 `run_turn`。
  自动续跑 = 同一 Session 上**起新 Run**，而不是把一轮拉长。
- **`Run.task_id`**：新增字段，缺省 `""`（未纳入任务）。既有 `/api/runs`、
  `/api/runs/sync`、CLI 调用方**逐字节不受影响**——`/api/runs` 的响应是**显式
  挑字段**（`run_id/session_id/status/title/preview`，已核 `app.py:696-707`），
  不是 `asdict` 全量导出，故新增字段不会漏进响应；并在 `tests/test_app.py`
  加一条响应字段快照钉住。Task 走**新入口** `/api/tasks`。
- **Task 字段**：`task_id / goal / session_id / repo_root / status / run_ids /
  artifacts / billing / continuations / started_at / created_at / updated_at /
  cancel_requested / fuse_fired`。
- **`repo_root` 在 M1 只是登记字段**：chat 模式的 task 不因此产生文件围栏
  （围栏仍归 S6a/`WORKSPACE_ROOT`）；项目隔离是 M2 的事。**明写**以免误读为
  「项目隔离已生效」。
- **状态机**（114 的六态，落成触发条件；`waiting-approval` 为**派生映射**，
  不落 Task 字段——见下）：

  | 状态 | 进入条件 | 出口 |
  |---|---|---|
  | `queued` | 创建，尚未起 Run | → `running`（worker 启动） |
  | `running` | 有 Run 在飞 / 续跑间隙 | → 其余各态 |
  | `waiting-approval` | **派生**：在飞 Run 的 `status == waiting_approval` | 裁决后回 `running` |
  | `waiting-steer` | 原地踏步熔断（`stuck` 事件）——需要人改向 | `resume` 给方向 → `running` |
  | `blocked` | 触硬保险丝（续跑/时长/成本上限，或续跑准入反复被拒） | 仅人工 `resume` → `running` |
  | `done` | 计划**全部** `done` 或 `skipped`（含无计划且自然收尾） | 终态 |
  | `failed` | Run 终态 failed，**或计划含 `failed` 步骤** | 终态 |
  | `cancelled` | 用户取消（`task.cancel_requested`） | 终态 |

  `done` 判据用 `Plan.is_complete()` **是不够的**——它把 `failed` 也认作终态
  （`plan.py:44-46`，已核）。M1 的 `done` 自己数一遍：全 `done`/`skipped` 才
  `done`，出现 `failed` 即 `failed`。
  区分 `waiting-steer` 与 `blocked`：前者「模型没进展，等人给方向即可续」，
  后者「安全保险丝熔断 / 续跑准入枯竭，必须人显式放行」。
- **产物登记**：产物 = Run 引用（`run_ids`）+ 会话引用（`session_id`），
  **不新建存储**（114 裁定）。
- **TaskStore**：内存 dict 为读取快路径，`data/tasks/<task_id>.json` 快照为
  持久真值源。**落盘时机**＝Task 状态迁移 **+ Run 边界**（起新 Run、Run 收尾）
  ——不是「只在状态迁移」（那样 `run_ids`/`billing` 在连续续跑期间永远不落盘，
  与「可追溯」自相矛盾）。频率仍远低于事件级。原子写（tmp + `os.replace`）。
  目录创建归 TaskStore（`mkdir(parents=True, exist_ok=True)`）。
  升级信号：多实例部署换外置存储（与 `RunStore` 同一取舍）。
- **`paths.py`**：新增 `TASKS_DIR = DATA_ROOT / "tasks"`（运行时数据，不进 git）。

### 2 steering 打断（纠偏，不杀 Run）

- **缝**：`loop.py` 的 `run_turn` 新增参数
  `steer_source: Callable[[], str | None] | None = None`，只在**检查点①（每次
  模型调用前）** drain 一次——即**同一 Run 内的下一次模型调用**（不是「下一
  轮 Run」，措辞必须统一）。有内容则：append 一条 `user` 消息进
  `session.messages` **与** `payload`、`on_event("steer.applied", {...})`、
  **继续本轮（不 return）**。`None` 时零行为差异。
- **为什么是 pull 缝**：与 `should_cancel` 同构（worker 轮询 Run 状态），
  内核不认识 HTTP；steering 与取消共享检查点，不新增一条 loop 缝。
  `checkpoint.py` 的 `on_event` 只认 `tool_started`/`tool_result`/`plan.*`，
  未知类型安全忽略——`steer.applied` 不进账本（已核，无需改）。
- **信箱**：`Run.steer_pending` + `request_steer(text) -> bool`（仅非终态生效）
  + `take_steer() -> str | None`（清空并取出）。**两条都要持 Run 上的新增
  `_steer_lock`**——`Run` 目前没有对应锁（`ide_ctx` 用的是闭包里的锁），
  「取出」与「置空」必须原子。
- **端点**：`POST /api/runs/{run_id}/steer {text}` ——任意在飞 Run 可用。
- **边界**：
  - 只对 `running` 生效；`waiting_approval` 挂起时 steering 无意义（确认槽独立）。
  - **不重置本轮预算**：纠偏在同 Run 的下一次模型调用生效；预算耗尽仍有
    `_close_out`（056）兜底，且任务态下续跑会用新预算接上（见 §3）。
  - **收尾段（`_close_out`）到达的 steer**：检查点① 不在收尾段，`_close_out`
    的模型调用不经过它 ⇒ steer 不会被消化。**必须显式兜**：`_task_worker` 在
    `should_continue` 为 False 时**先 `take_steer()`**，有内容则用它起新 Run
    （而非直接 `done`）；交互态 Run 则发 `steer.dropped` 事件告知客户端。
    补一条「steer 在收尾段到达」的测试。

### 3 自动续跑（编排层循环，不进内核）

- **判定函数**（住 `orchestrator/task.py`）：`should_continue(task, run, session)
  -> bool`，条件全满足才续：
  1. `run.status == STATUS_COMPLETED`（**读 Run 置态**，不依赖 `RunResult`
     被挂到 Run 上——`_run_worker` 现在不挂它）；
  2. 本轮未触发 `stuck` 事件（触发 → 置 `waiting-steer`）；
  3. `p = session.plan.view()` 非 None 且 `not p.is_complete()`；
  4. 未触硬保险丝且 `task.cancel_requested` 为假。
- **载体**：同一 Session 起**新 Run**（`run_ids` 追加），`user_text` = 显式标注的
  续跑注入消息（形如 `[自动续跑] 计划尚有未终态步骤 #a/#b…，请继续推进；
  若已全部完成请明确声明。`）。**不用 `user_text=None`**：那条是 038 的「崩溃
  续跑」语义（不追加提问、heal 悬挂调用），混用会污染两套语义的边界。
- **准入被拒的处理**（`RunStore.create_if_idle` 在全局 in-flight 满时返回字符串）
  ：续跑起新 Run 时做**退避重试**（上限约 30s/若干次，常量与主常量同区），
  耗尽 → `blocked` 并把原因写进 Task（否则 1h 验收可能在「槽位被别的会话占满」
  时无声断在 `running`，且状态机分不清「在跑」与「被拒」）。**不改**
  `RunStore` 的并发上限语义，也不为任务预留槽位。
- **保险丝与看守线程**：`TASK_MAX_CONTINUATIONS`（默认 20）、
  `TASK_MAX_WALL_CLOCK_S`（默认 7200）、`TASK_MAX_COST`（默认 None＝不限）。
  时长/成本保险丝**不能只靠轮间检查**——`Run.request_confirm` 是**无超时阻塞**
  （`run_store.py:169-175`），L2 确认挂起期间 `_task_worker` 拿不到控制权，
  保险丝会失效。故：**每任务一个看守线程（daemon，30s 一跳）**，到期即置
  `task.fuse_fired` 并调在飞 Run 的 `request_cancel()`（`request_confirm` 的
  等待循环本就轮询 `cancel_requested`，因此能顺带解开挂起）。熔断后 Task 落
  `blocked`（**不是** `cancelled`）——靠 `fuse_fired` 与用户取消区分开。
  看守线程随任务终态退出，不常驻。
- **计划契约**：续跑以「活跃计划」为契约——没有计划的委派是**一发**，跑完即
  `done`（不猜「它还想不想继续」）。首条消息带一句极简 kickoff 指令，提示
  「复杂任务先立计划」（人设化归 M2 skill，M1 只一句，不新建人设）。
- **宿主循环**：`app.py` 新增 `_task_worker`，**循环调用既有 `_run_worker`**
  （加可选 `task` 参数：登记 Run 进 task、把最终 session 与 `fuse_fired` 挂给
  Run 供判定读）。每轮照旧 `save + finish`（076 保底不变式「读到 run.completed
  时盘上必有这轮对话」逐轮成立）；`settle_session` 收尾仍在每轮 `_run_worker`
  内（不改聊天路径，任务路径逐轮复用同一行为）。
- **`cancel` / `resume` 语义**：`POST /api/tasks/{id}/cancel` 同时置
  `task.cancel_requested` 并调在飞 Run 的 `request_cancel()`（否则取消后看守/
  续跑可能再点火）。`POST /api/tasks/{id}/resume` 在恢复前**重置保险丝基线**
  （续跑计数、`started_at`、成本基线），否则一恢复就再次熔断。
- **反方登记**：逐轮 settle 意味着 20 次续跑 = 20 次固化 LLM 调用（成本）。
  接受：复用同一份 `_run_worker` 的收益大于这点成本；升级路径是「settle 延迟到
  任务收官」，但会与聊天路径分叉——M1 不做分叉。

### 4 任务分账

- **per-Run 切片**：`_run_worker` 在 `run_turn` **之前**取一次 `UsageLedger`
  快照、在 `run._settle_done.set()` **之前**再取一次，差值为 `run.usage`。
  `UsageLedger` 是 dataclass，快照 = 字段值拷贝（`dataclasses.replace`）。
  注意 `internal_llm` 与 `ctx.llm` **共享同一本账**（`assemble.py:404-413`，已核）
  ⇒ 压缩链与 settle 的成本也在差值内，属本轮成本，口径如此。
- **并发归因的诚实处理**（评审问题 1 的处置）：全局账本是进程级、默认并发
  `FACTA_MAX_CONCURRENT_RUNS=3`，别的会话在两个快照之间记账会**串进本 Run**。
  M1 的处置不是假装精确、也不是禁止并发，而是**显式标注**：Run 窗口内若有
  其他 Run 在飞（`RunStore` 加一个 `in_flight_count()`），置
  `run.usage_approx = True`；`Task.billing` 汇总时同样带
  `approx_runs` 清单。**口径写死**：无并发时精确；有并发会话期间为近似**且已
  标注**，绝不静默串账。
  升级路径（登记触发信号＝并发下的账单被实际用于决策）：①**任务域账本**
  （任务用自己的 `UsageLedger` 建 LLM 链，落点在 `assemble` 加一个 scoped
  factory）；②**`CallLedger` 按 `run_id` 标注**（`CallEntry` 加可选 `run_id`，
  网关按线程归属打标）。两条都在案，M1 不做。
- **任务聚合**：`task.billing = Σ run.usage`，随 Task 快照落盘。
- **出口**：`GET /api/tasks/{task_id}/billing` ——兑现 111 摩擦#2（Web 壳无账单
  出口），同时是 M1 验收「任务级账单可查」的凭据。响应含 per-Run 明细、
  合计、`approx` 标记。

## 落点清单（供开工）

| 文件 | 改动 |
|---|---|
| `src/facta/orchestrator/task.py` | **新**：`Task` + `TaskStore` + `should_continue` + `_is_task_done` + 保险丝常量 |
| `src/facta/orchestrator/loop.py` | `run_turn` 加 `steer_source` 参数（检查点① drain） |
| `src/facta/server/run_store.py` | `Run` 加 `task_id` / `steer_pending` / `_steer_lock` / `request_steer` / `take_steer` / `fuse_fired` / `usage` / `usage_approx` / `_final_session`；`RunStore` 加 `in_flight_count()` |
| `src/facta/server/app.py` | `CreateTaskRequest` + `_task_worker` + 看守线程 + `_run_worker` 加 `task` 参数 + 端点 |
| `src/facta/paths.py` | `TASKS_DIR` |
| `tests/test_task.py` / `tests/test_steer.py` | **新**：状态机与 done 判据、续跑判定与保险丝（含确认挂起期间熔断）、steering 缝（含收尾段到达）、分账切片与 approx 标注 |
| `tests/test_app.py` | 端点 + `/api/runs` 响应字段快照 + 既有路径不回归 |

**端点（共 7 条：6 条 task + 1 条 run）**：`POST /api/tasks`（建任务 + 首 Run）｜
`GET /api/tasks`｜`GET /api/tasks/{task_id}`｜`GET /api/tasks/{task_id}/billing`｜
`POST /api/tasks/{task_id}/cancel`｜`POST /api/tasks/{task_id}/resume`｜
`POST /api/runs/{run_id}/steer`。

**前端**：M1 **不新增页面**，验收靠 API。理由：①111 摩擦#2 的本质是「缺端点」
而非「缺页面」；②前端有构建闸（`static/fw/` 进 git + CI `git diff --exit-code`），
M1 保持后端聚焦，任务面板归 M3（多任务视图）与 M4（VS Code 面板）。
反方登记：可观测性降一档（要 curl）；用户若要求，M1 内补一个任务卡片页。

## 边界与负决策

- **不做**：多任务并行调度（M3）；skill 数据化与项目记忆归属（M2）；
  VS Code 面板（M4）；任务持久化换外置存储（触发信号＝多实例部署）；
  并发下的精确归因（升级路径见 §4）。
- **不改**：`run_turn`「一轮」语义；056 收尾段；082① 取消路径；038 healing；
  `RunStore` 并发上限语义。
- **Task 不并入 TodoStore**：`todos`（个人待办）与 Task（委派工作单元）是两类
  实体，不合并、不互相引用（避免「待办」概念被撑大）。

## 开工前评审记录（2026-10-19，本仓 `/review` dogfood）

评审输入＝本案草案全文 + 114/M1 验收钉子；评审 Agent 用 `deepseek-flash`，
12 轮预算（112），只读证据菜单（111）。输出：总体判断「需要修改后再开工」，
8 条影响交付的问题 + 9 条可选建议。

| # | 评审问题 | 处置 |
|---|---|---|
| 1 | per-Run 账单用全局 `UsageLedger` 快照差＝**归属错误**而非近似（默认并发 3，别的会话会串账） | **采纳**：改为显式标注 `usage_approx`（不假装精确、不禁并发），升级路径①任务域账本②`CallLedger` 按 run_id 标注写入 §4 并挂触发信号 |
| 2 | 时长保险丝在 L2 确认挂起期间失效（`request_confirm` 无超时阻塞） | **采纳**：加每任务看守线程（30s 一跳）置 `fuse_fired` + `request_cancel()`，熔断落 `blocked` |
| 3 | 续跑起新 Run 被 `create_if_idle` 全局上限拒绝时无处理 | **采纳**：退避重试 + 耗尽置 `blocked` 并记原因；不改 RunStore 语义 |
| 4 | `done` 判据把 `failed` 步骤也算「全终态」（`Plan.is_complete()` 认 failed 为终态，已核） | **采纳**：`done` 自数「全 done/skipped」，含 `failed` → `failed` |
| 5 | 「只在状态迁移落盘」会丢 `run_ids`/`billing` 的逐轮更新，与「可追溯」自相矛盾 | **采纳**：落盘时机扩为「状态迁移 + Run 边界」 |
| 6 | `waiting-approval` 无触发点（`_task_worker` 在 `_run_worker` 返回前读不到 Run 状态） | **采纳**：定为**派生映射**（序列化时读在飞 Run 的 `status`），不落 Task 字段（即自审 #6 的「只做映射」路线） |
| 7 | steering 在收尾段（`_close_out`）到达会被丢弃 | **采纳**：`_task_worker` 在 `should_continue` 为 False 时先 `take_steer()`，有内容则用它起新 Run；补测试 |
| 8 | `Run.task_id` 是否破坏 `/api/runs`「逐字节不变」（待验证：序列化是否 `asdict`） | **核实后驳回，不改设计**：`app.py:696-707` 是显式挑字段，新增字段不漏进响应；另加一条响应字段快照测试钉住 |

**可选建议 9 条全采纳**：cancel 同时置两处；`resume` 重置保险丝基线；端点计数
措辞（6 task + 1 run = 7）；「下一轮」统一为「同一 Run 内的下一次模型调用」；
`take_steer`/`request_steer` 明确加锁；判定条件 1 改读 `run.status`；
`Task.repo_root` 在 M1 只登记不产生围栏（明写）；`TASKS_DIR` 创建归 TaskStore；
验收步骤写明 steering 在**轮边界**发送（避免把「没即时打断」误判为失败）。

**评审遗留待验证（本案已自行核实的）**：`Plan.is_complete()` 认 failed 为终态
（`plan.py:44-46`，事实）；`internal_llm` 与 `ctx.llm` 共享同一本 `UsageLedger`
（`assemble.py:404-413`，事实）；`/api/runs` 序列化为显式挑字段（`app.py:696-707`，
事实）；`checkpoint.py` 的 `on_event` 对未知事件类型安全忽略（事实，无需改）。

## 自我挑刺（草案自审）

| # | 质疑 | 处置 |
|---|---|---|
| 1 | TaskStore 落盘是否过度（YAGNI）？ | 保留：114 要「可观测与追溯」，跨重启追溯需落盘；且落盘点已收窄为状态迁移 + Run 边界。 |
| 2 | 自动续跑为何不用 `user_text=None`？ | 已裁定不用（§3）：那是 038 崩溃续跑语义，混用污染边界。 |
| 3 | 逐轮 settle 的 LLM 成本 | 接受并登记；升级路径「延迟到收官」写明，M1 不分叉。 |
| 4 | 没有计划的委派会不会永远 `done` 太早？ | 是设计选择：以计划为契约；kickoff 指令促其立计划。 |
| 5 | `waiting-steer` 与 `blocked` 是否冗余？ | 不冗余：一个「给方向即续」，一个「须显式放行」；与 M3 权限档对齐。 |
| 6 | 状态机是否要「等用户在飞 Run 完成」的过渡态？ | 不加：Task 层只做映射（`waiting-approval` 即派生），避免双份状态机。 |

## 待验收

- [ ] 一个 ≥1h 真实任务无人值守跑完（计划驱动、逐轮落盘、账本可回溯）。
- [ ] 中途 steering 改向一次成功（**在轮边界发送**；`steer.applied` 事件可见、
      方向进入同 Run 的后续模型调用）。
- [ ] `GET /api/tasks/{id}/billing` 返回 per-Run 切片、合计与 `approx` 标记。
- [ ] 硬保险丝可触发（含 **L2 确认挂起期间**的时长熔断）并落 `blocked`；
      `resume` 重置基线后可恢复。
- [ ] 既有路径零回归（`/api/runs` 响应字段快照、CLI、评测、同步端点）。
- [ ] M1 验收本身即 110/V02 自用任务（与 113 的 V02 第一例衔接）。

## V02 自用摩擦记录（第二例：评审 M1 草案）

- **材料准备成本**：≈0（只给「评审对象＝115 草案」+ 目标/验收/上游 ADR 清单 +
  请特别核实的 4 点，其余让它自己去仓库读）。
- **评审产出**：8 条影响交付的问题 + 9 条可选建议 + 4 条待验证，其中 7 条被
  采纳改写设计、1 条（问题 8）经核实为**伪问题**（序列化非 `asdict`）。
- **质量判断**：8 条问题**全部落在真实代码事实或真实裁定冲突上**（无一条凭空
  推测），且两条（问题 2 确认缝无超时、问题 4 `is_complete` 认 failed）是我自己
  读码时漏掉的——评审这一轮**净增了设计质量**，不是走过场。
- **待改进**：评审提了 4 条「待验证」，其中 3 条在仓库里可读原文就够（`plan.py`
  / `assemble.py` / `app.py`）——说明我的评审 brief 没把「可直接核实的点」说清，
  下一轮应把可核实项写成「请直接读 X 文件」，减少它的「待验证」占位。
