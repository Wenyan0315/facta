# ADR 117：M3 多任务——后台队列 + 任务级并发上限 + 授权档位

- 状态：**已裁定**（2026-11-17，用户定稿照修订稿开工）｜日期：2026-11-17
- 上游：[114](114-facta-2-roadmap.md)（2.0 路线图，M3 期定义与两颗验收钉子）、
  [115](115-m1-controllable-long-tasks.md)（M1 底座：TaskStore/状态机/看守线程/退避先例）、
  [116](116-m2-bounding-and-skills.md)（M2 底座：技能表/build_skill_agent/锚点接线；
  本案承接其四条 M3 挂账）、
  [111](111-v01-review-prototype.md)（repo_root 锚点 replace 先例）、
  [100](100-plan-authorized-rounds-budget.md)（计划过 L2 人审＝授权语义先例）、
  [019](019-s4b-terminal-confirm.md)（L2 确认缝原型）、
  [052](052-memory-write-fence.md)（写围栏）、
  [038](038-run-checkpoint.md)/[040](040-run-checkpoint-impl.md)（崩溃续跑通道）
- 调整关系：只落 114 的 **M3 期**；M4 另案（编号顺延）。116 负决策里挂 M3 的
  四件事（授权档、Task 绑技能、文件围栏接线、spawn_step 证据机械强制）由
  本案一并裁决，不另开案。

## 背景

现状盘点（读码核对，行号以 2026-11-17 main=212eb1d 为准）：

| 能力 | 现状 | 缺口 |
|---|---|---|
| 任务队列 | `POST /api/tasks` 202 即起 daemon 线程跑 `_task_worker`（app.py:1046-1067），**无排队实体**；Run 槽满时 `_create_run_with_backoff` 退避 2s×15（app.py:541-556），耗尽落 `blocked` | 「队列」是重试轮询假队列：无 FIFO 保证、并发大时任务批量 blocked；`queued` 状态机里有名无实（创建即 running，app.py:608） |
| 并发上限 | 全局 Run 槽 `FACTA_MAX_CONCURRENT_RUNS` 默认 3（app.py:660-662），任务与聊天争同一池，115 明裁「不为任务预留槽位」 | 无任务级上限概念；3 任务 + 1 聊天 = 4 in-flight 需求 > 3 槽，第 3 个任务必然退避——114 钉子「3 任务并行互不阻塞」在现状下**结构性达不成** |
| 授权档 | 无档位概念。挂 L2 确认的只有 `run_command`（动态 `_confirm_rule`：白名单免确认/危险停确认，terminal.py:156-210）与 `make_plan`（tools/plan.py:451）；`write_file`（files.py:356-368）与 `write_note`（notes.py:348-358）**免确认直写**，拦截仅围栏+内容闸；`approval_policy` 是 env 级全局（registry.py:284-299），非任务级 | 116 挂账「技能需要写工具 = M3 授权档一起做」未兑现；114 钉子「涉写任务停 L2 等确认」无承载 |
| 任务绑技能 | Task 无 skill 字段；任务恒用主 agent（全工具池） | 114 定位「每任务绑一个项目仓 + 一个技能」只兑现了 repo_root 半边 |
| 文件围栏 | 技能 agent 只读 8 件已按 repo_root 锚定（116）；但写工具锚点恒 WORKSPACE_ROOT（S6a 现状） | 116 明裁「任务的读写限在项目内的围栏接线随 M3 任务绑项目一起做」 |
| spawn_step 证据 | 116 顺手件 4 只加了描述纪律一行，机械强制明裁「归 M3 授权档一起做」 | 无动态证据仍自动标 done（spawn.py:342-347） |

**架构红线（114 复用）**：不重写内核。run_turn 一轮语义、L2 确认缝（registry.py
execute 收口）、审计/围栏/checkpoint 全部沿用；演进集中在**调度层（队列 +
并发准入）+ 技能装配层（档位扩池）**，写围栏只换锚点参数不改围栏本身。

## 概念裁定

### 1 后台队列：`queued` 落实 + 触发式调度（无常驻单点）

- `POST /api/tasks` 改为**入队**：创建 Task 落 `queued` 态即返 202，不再直接
  起线程。`queued → running` 的迁移由调度函数 `_maybe_start_queued` 执行。
- **触发式调度**（懒惰阶梯：不引入常驻 dispatcher 线程）：`_maybe_start_queued`
  在三个事件点被调——①入队时；②任一任务落终态时（`_task_verdict` 尾部）；
  ③服务启动装配完成时（处理落盘遗留）。逻辑：按 `created_at` 序扫 `queued`
  任务，`running` 任务数 < 上限则逐个起 `_task_worker`（起线程前置
  `queued→running` 落盘，防双起）。
- **兜底巡检**：触发式有错过事件的竞态缝（如异常路径未触达触发点），配
  低频巡检线程（daemon，60s 一跳，`_FUSE_TICK_S` 同款风格）只做一件事：调
  `_maybe_start_queued`。巡检异常捕获后继续下一跳，不退出——队列不会因单次
  异常卡死。
- **重启恢复**：服务启动时，落盘任务里 `queued` 态与「`running` 且未熔断
  未取消」的回落 `queued` 重新入队；`fuse_fired`/`cancel_requested` 为真的
  保持 `blocked`/`cancelled` 语义**不回落**（熔断不因重启洗白——评审问题 4）。
  Run 级崩溃续跑 038 通道不动；任务级不做无缝续跑，保守回落，触发信号=
  首例真实任务因回落重跑造成实际损失。
- **resume 入队语义**（评审问题 3）：`POST /api/tasks/{id}/resume` 的
  blocked/waiting-steer 恢复**落 `queued` 走调度**（尊重并发上限），不再
  直起新 worker——统一「起 worker 只有调度器一个入口」，消除 M1 遗留的
  双路径；保险丝基线重置逻辑（app.py:1123-1127）原样保留。

### 2 任务级并发上限：新常量，池不隔离

- 新增任务级并发上限（默认 **3**，与钉子同数；env 可调）。**双层命名**
  （评审问题 6，M1 保险丝常量同款先例）：env `FACTA_MAX_CONCURRENT_TASKS`
  （app 层读入）→ `task.py` 常量 `TASK_MAX_CONCURRENT_TASKS`（缺省值，
  与 `TASK_MAX_CONTINUATIONS` 同区）。
- 计数口径：任务**占槽从 worker 起线程起**——退避等待、确认挂起、推进中
  均算（评审问题 5），终态释放。waiting-approval 挂起中的任务占的是真
  并发（worker 线程仍活着）。
- 全局 Run 槽默认 **3 → 5**（`FACTA_MAX_CONCURRENT_RUNS` env 语义不变，仅
  默认值上调）：3 任务各占 1 槽 + 聊天余 2 槽。任务与聊天**继续共池**
  （115「不为任务预留槽位」语义保留——调度只管任务数，Run 准入仍走
  `create_if_idle` 原路）；池隔离不做（负决策）。
- 任务 worker 内既有退避逻辑不动（Run 槽被聊天挤满时仍退避重试，耗尽落
  blocked 的语义保留——那是 Run 层保险丝，与队列层正交）。

### 3 授权档位：两档，`readonly`（缺省）/ `write`

**档位模型**（最小两档起步，第三档挂触发信号）：

| 档 | 工具池 | 写行为 |
|---|---|---|
| `readonly` | 技能 registry 8 件只读（116 现状） | 写/终端**结构上缺席**（不在菜单，调不到） |
| `write` | 8 件 + `write_file` + `write_note` + `run_command` | `write_file`/`write_note` **本任务首次调用停 L2**（任务级一次性授权，见下）；`run_command` 维持既有动态规则（白名单免确认/危险停确认，019 原样） |

- **技能文件新节 `## authorization`**：值 `readonly`（缺省）或 `write`；
  未知值**装配报错**（116「校验不静默」同款）。`SkillSpec` 增
  `authorization: str` 字段。
- **任务级一次性写授权**（钉子「涉写任务停 L2 等确认」的承载）：
  - `write` 档任务首次调用 `write_file`/`write_note` → 该工具挂
    `needs_confirmation`（动态规则，`_confirm_rule` 同款模式），Run 进
    waiting-approval，确认消息明示「本任务写授权（批准后本任务后续写操作
    免停）」。
  - **approve** → 本次写执行 + Task 落 `authorized_write=True`（TaskStore
    落盘，重启不丢）+ 后续写免停；**reject** → 本次调用以拒绝原因返回
    模型（不置位），下次写再停。
  - 实现（评审问题 1/8 的口径收口）：`request_confirm` 挂起时在 Run 上
    登记 `confirm_kind`（"write-auth" / 常规确认缺省 None——工具名在
    `request_confirm(tool, …)` 参数里现成可得，run_store.py:185）；confirm
    端点 approve 时**先置位后放行**（按 `run.task_id` + kind 写
    Task.authorized_write，同线程顺序保证原子），再 `resolve_confirm`。
    普通确认 kind=None 零改动；`resolve_confirm` 本体签名不动。
- **覆盖规则**：`POST /api/tasks` 可带 `authorization` 覆盖技能档位，但
  **只能收严**（write→readonly 允许；readonly→write 拒 422）——放宽必须改
  技能文件（授权真值源唯一：文件是声明，请求是约束）。
- **主 agent 任务（不绑技能）零回归**：档位只作用于技能任务；主 agent 任务
  维持现状全池 + 现状确认规则（write_file 免确认直写）。不把主 agent 任务
  改造成默认受档——那是行为回归，M3 不做（触发信号=用户主动要求主任务也
  分档）。
- **与 100 计划授权的关系**：不重合不冲突——100 管「计划批多大轮次预算」，
  本案管「任务能碰哪类工具」；`make_plan` 的 L2 人审原样不动。

### 4 Task 绑技能 + 文件围栏接线（116 挂账兑现）

- **`Task.skill: str | None` 字段**：`POST /api/tasks` 可带 `skill`（校验
  已加载技能名，未知 422——mode 端点同款）。缺省 None = 主 agent（现状）。
  `_task_worker` 里 `skill` 非空 → **复用 `_agent_for_mode(ctx, session,
  skill, repo_root)`**（app.py:326-340，评审问题 2——技能分派入口已存在，
  不另造构造路径）；None → `build_agent` 现状。任务内**串行续跑**（一轮
  一 Run，115 现状）——「任务内并行 spawn」不在 M3 范围（生成轨 Brief
  双轨对照暴露的口径二义，在此显式收口：并发指任务间，不指任务内）。
- **settle 行为接线**：技能任务收尾两处（标题就地补 app.py:453、跳固化
  app.py:471）复用 M2 的 `skip_settle` 预计算布尔（技能 settle=skip → 真；
  主 agent → 假），worker 条件已按布尔判断（116 改造过），task 路径只需
  把布尔算进去——不新增按名字判断。
- **文件围栏换锚**（116 明裁）：技能任务带 repo_root 时，其工具池内
  `write_file`/`run_command` 的工作区锚点 = repo_root（111 锚点 replace
  先例从只读泛化到写与终端；S6a 围栏机制不变，只换根）。主 agent 任务
  维持 WORKSPACE_ROOT 锚点（零回归）。读工具 M2 已锚，此处补齐写侧。

### 5 spawn_step 证据机械强制（116 顺手件 4 兑现）

- spawn_step 自动标 done 的条件增加一条机械判据：**结论串须含至少一个可
  验证锚点**——checkpoint id / 文件路径 / run_id / 会话 id 任一可正则匹配
  （落点 `spawn.py:342-347`）。无锚点 → 该步不标 done，落 `waiting-steer`
  并在任务事件里注明「缺可验证证据，待人工核」。
- 纯机械可测、零语义误伤（只挡「空口断言」，不试图判断证据真伪）；「证据
  真伪核验」仍是 M3 外（负决策）。

## 落点清单（供开工）

| 文件 | 改动 |
|---|---|
| `src/facta/orchestrator/task.py` | `Task` 增 `skill`/`authorization`/`authorized_write` 字段；`TASK_MAX_CONCURRENT_TASKS` 常量；重启回落（熔断/取消不回落）与 resume 入队语义 |
| `src/facta/server/app.py` | `/api/tasks` 改入队（202 即返）；`_maybe_start_queued` 触发式调度 + 60s 巡检线程；`_task_worker` 按 skill 走 `_agent_for_mode`；confirm 端点先置位后放行（按 confirm_kind）；启动恢复 |
| `src/facta/server/run_store.py` | `Run` 增 `confirm_kind`（request_confirm 登记，缺省 None；resolve_confirm 签名不动） |
| `src/facta/skills.py` | `SkillSpec.authorization` + 节解析校验；`build_skill_agent` 按档位扩池（write 档注册写三件）+ 锚点泛化到写工具 |
| `src/facta/tools/files.py` / `notes.py` | 写工具挂动态确认规则（write 首停，_confirm_rule 同款）；锚点参数化 |
| `src/facta/tools/spawn.py` | 证据锚点机械闸（:342-347） |
| `docs/architecture.md` | 决策记录回写（项目纪律：所有架构决定进决策记录——评审问题 9） |
| `tests/test_queue.py` / `test_authorization.py` | **新**：FIFO/上限/重启回落（含熔断不回落）/resume 入队；写首停-approve-免停链、readonly 结构性缺席、收严覆盖、主 agent 零回归、巡检兜底、run_command 确认不置位、spawn 证据闸 |
| 既有 `test_tasks.py` / `test_skills.py` | 重锚：queued 落实后的状态断言、build_skill_agent 新参数缺省值 |

**既有路径不回归**：主 agent 任务全行为、聊天 Run、`/api/runs/sync`（mode
分派）、技能表装配、frozen-eval、`/api/runs` 响应字段快照（115）。
`approval_policy` env 语义不动（与档位正交：env 管全局策略，档位管任务池）。

## 边界与负决策

- **不做**：外部队列/进程池（celery/redis——单进程触发式足够）；任务优先级
  与抢占（FIFO）；池隔离（任务/聊天共 Run 池）；跨重启 running 任务无缝
  续跑（回落 queued，触发信号制）；第三授权档（如网络禁用档——触发信号=
  真实使用中发现两档不够）；任务域账本（M1 挂账不变，触发信号=并发下账单
  被实际用于决策）；前端面板与队列可视化（M4 宿主层；M3 验收全 API，
  116 同口径）。
- **不做**：「approve 置位」以外的确认链路改动（run_command 动态规则、
  make_plan 人审、headless 不自动裁决 L2——081 红线原样）。
- **不做**：证据真伪核验（spawn 闸只挡空口断言）。
- **不改**：run_turn 一轮语义、S6a 围栏机制本身（只换锚根）、052 记忆写
  围栏、098 固化确认红线、M1 状态机既有迁移与保险丝。

## 自我挑刺

| # | 质疑 | 处置 |
|---|---|---|
| 1 | 触发式调度错过事件（异常路径未触达触发点）→ 队列卡死？ | 60s 巡检兜底线程兜住；巡检自身异常捕获续跳；测试钉「无触发点时巡检拉起队列」 |
| 2 | 任务级一次性写授权太粗——批一次就能写满仓？ | 三重缓解：围栏锚 repo_root 限范围、run_command 仍逐条动态规则、审计全记；真值源在技能文件（改档须改文件）；触发信号=首例越权写 |
| 3 | Run 槽默认 3→5 的破坏面？ | env 语义不变、已显式配置者不受影响；默认值变更在 changelog 注明理由（钉子结构性要求） |
| 4 | approve 链路改动的回归面？ | 置位只对「写首停」类型确认生效，普通确认零变化；测试钉「run_command 确认不触发置位」 |
| 5 | 技能任务 settle=skip 与任务固化混线？ | 复用 skip_settle 布尔（116 已按属性判断），不新增按名字分支；测试钉 |
| 6 | 并行任务写同一仓冲突？ | 不做文件锁（M1 并发归因近似同款立场）；触发信号=首例并行写真实冲突 |
| 7 | 队列 FIFO 按 created_at——同秒创建顺序？ | 同秒按 task_id（创建序号后缀）稳定排序，测试钉 |

## 待验收

- [ ] 本 ADR 过开工前评审（评审对象含 **Brief 双轨例**：116 待验收唯一
      未验项——M3 草案评审时，手工 Brief 与生成 Brief 同一时点独立写、
      对照引用真实性/相关性/过时判定/长度与耗时）。
      → **草案评审已做**（见文末评审记录：9 条候选问题全部采纳修订）；
      **Brief 双轨对照仍未覆盖**（本期验收未排入），随 M4 一并做。
- [x] 三门全绿：ruff / mypy / pytest（新 test_queue/test_authorization；
      既有 suite 零回归）。→ PR #67 CI 三检查全绿；本地 1002 passed / 7 skipped
      （MCP 两文件 deselect＝本地代理 502 环境性预存失败）。
- [x] 钉子①：3 任务并行互不阻塞——真机 3 任务同时 running、各自推进到
      终态，互不 blocked（API 全程可观测）。→ 1s 采样 trace：`t+0s` 三 running
      + 第四 queued，`t+8s` 四个全 done，无一 blocked。
- [x] 钉子②：涉写任务停 L2 等确认——write 档技能任务首写 waiting-approval
      → approve → 后续写免停 → 任务跑完终态；reject 路径对照。→ approve 链
      实测（同 run 内第二写零 confirm、`authorized_write=true`、文件落 repo_root
      内）；reject 对照不置位、文件不落盘。
- [x] readonly 结构性缺席：readonly 档技能任务菜单无写/终端工具。→ 模型自陈
      菜单恰只读 8 件，回 `NO_TOOL write_file`，`confirm.request=0`。
- [x] 队列行为：第 4 任务排队、终态触发下一任务启动、重启回落重入队。
      → 终态同拍拉起（未等巡检）；`kill -9` 后以 `FACTA_MAX_CONCURRENT_TASKS=1`
      重启，脏 running 回落 queued 且受新上限约束，FIFO 排空全 done；
      熔断/取消不因重启洗白；resume 亦走队列（槽满时稳定停 queued）。
- [x] 零回归抽查：主 agent 任务/聊天/既有 sync 端点行为不变。→ chat sync 200
      completed；主 agent 任务 `write_file` 免确认直写（`confirm.request=0`）。
- [x] spawn_step 证据闸：无锚点结论不落 done。→ 真机复现：闸拦下后步骤留
      `in_progress`、`finish_plan` 拒收官，模型空转至续跑熔断落 blocked。
      证据与三条观察见 [m3-acceptance-report.md](../../m3-acceptance-report.md)。

**验收结论（2026-11-19，main=`ec0aca4`）**：除「Brief 双轨对照」（116 遗留，
随 M4）外全部通过，未发现 P0，无阻断项。真机证据、账单（20 单 ¥0.9862）与
四条观察处置见 [m3-acceptance-report.md](../../m3-acceptance-report.md)。

## 开工前评审记录

- **评审对象**：本 ADR 草案 v1（2026-11-17 起草）；评审 Agent = deepseek、
  12 轮预算、只读证据菜单（真机 dogfood，review 模式，约 58s）。
- **评审结论**：草案评审轮预算耗尽前产出 **9 条候选问题**（事实错误类：
  常量命名不一致；落点/上游冲突类：`_agent_for_mode` 未入落点、queued
  与 resume 迁移未交代、置位层选错；口径二义类：重启回落忽略保险丝
  标记、退避期计数口径、触发点时序、approve 原子性；纪律类：
  architecture.md 回写缺落点），评审自身按「无完整引证不发布正式结论」
  纪律仅出候选清单。起草者直读三处被点名代码（`_agent_for_mode`
  app.py:326-340 / `request_confirm`/`resolve_confirm` run_store.py:185-221
  / Task 恢复面 app.py:1110-1127）逐条定案，**9 条全部采纳修订**，已并入
  上文对应小节（标注「评审问题 N」）。
- **处置表**：

| # | 问题（评审候选 → 起草者定案） | 处置 |
|---|---|---|
| 1 | `resolve_confirm` 只收 bool，置位方案拿不到工具名 | 属实。工具名在 `request_confirm(tool,…)` 现成可得——挂起时登记 `confirm_kind`，端点先置位后放行（§3） |
| 2 | agent 构造入口未入落点 | 属实。复用 `_agent_for_mode`（app.py:326），不另造路径（§4） |
| 3 | queued 与 resume 迁移未交代 | 属实。resume 改落 queued 走调度，起 worker 单一入口（§1） |
| 4 | 重启回落会把熔断任务洗白 | 属实。fuse_fired/cancel_requested 为真不回落（§1） |
| 5 | 退避等待期计数口径含混 | 明确：占槽从 worker 起线程起（§2） |
| 6 | 常量命名正文与落点不一致 | 统一双层命名：env + task.py 常量（§2，M1 先例） |
| 7 | 触发点时序疑虑 | 不改：触发点③「装配完成时」时序已明确；测试钉启动拉起遗留 |
| 8 | approve 原子性 | 端点内先置位后放行（同线程顺序），测试钉（§3） |
| 9 | architecture.md 回写缺落点 | 属实。落点清单补一行（项目纪律） |

- **Brief 双轨例**（116 待验收唯一未验项，本案评审时兑现）：同一输入句
  「Facta M3：后台队列 + 任务级并发上限 + 授权档位……」两轨独立成稿——
  手工轨（起草者凭调研写，约 1900 字）vs 生成轨（brief 模式真机，5581
  字符 / 76s / 12 轮预算）。对照结论：**通过**——①引用真实性两轨全真，
  生成轨「事实/推测/记忆」三层区分更严格（build/lib 副本甄别、M3 误命中
  甄别均正确）；②相关性生成轨盲跑即抓到三条核心张力（M3 须独立 ADR /
  授权点与 L2 人审重合 / 115 槽位语义须正面回答），与带草案知识的手工轨
  同水平；③过时判定手工轨占优（生成轨对 115 状态行只能标「未核实」）；
  ④设计取舍类分歧真实存在（生成轨建议涉写「每次停 L2」vs 草案「首写停
  一次」）——印证 113 定位：Brief 供事实底座，决定仍归人裁。附带正向
  证据：草案文件在场的第一跑，生成 Brief 拒绝把 ADR 转述冒充读码事实、
  并主动提示双轨例存在（诚实纪律实证）。目录列表截断第三次复现
  （M2 报告观察项 #3，继续挂档）。双轨全文存
  `/tmp/facta-accept-20261117/`（验收材料，不入库）。
- **状态**：修订后待用户定稿，定稿后开工。

## 实现注记（2026-11-17，实施期收口）

落点清单三处按更薄方案落（结论不变，载体减重），照实留档：

1. **`Run.confirm_kind` 不加**——写首停改在 `_run_worker` 的 `on_confirm`
   包装实现（技能任务 + write 档时：写两件直接放行、其余走 `run.request_confirm`，
   approve 后置位 `Task.authorized_write` 落盘再放行）。包装在 worker 线程内
   「先置位后放行」原子性比 confirm 端点拆两步更不易漏，run_store 零改动。
2. **files.py/notes.py 不挂动态确认规则**——写两件的 `needs_confirmation` 改标
   在 `build_skill_registry`（write 档 `replace(tool, needs_confirmation=
   WRITE_AUTH_GUARD)`）完成：档位是技能池的属性而非工具的全局属性，主 agent 的
   `write_file` 免确认直写逐字节不变（零回归半边）。
3. **spawn 证据闸的「落 waiting-steer」在步骤层折算为留 `in_progress`** +
   note「缺可验证证据，待人工核」——StepStatus 无 waiting-steer 态（那是 Task 态）；
   机械后果同构：`finish_plan` 因悬空步骤拒收官，人工核过再 `update_plan_step`。

另：起跑注入消息落 `Task.next_text`（刻意不进 `to_dict`——它是指令载荷不是
状态）；创建=goal+kickoff 提示，resume=用户指令/续跑代打，重启回落清空防重发。

### 验收期观察（2026-11-19 真机验收，四条均**不改码**、挂触发信号）

4. **确认文案未明示授权语义**：§3 要求「确认消息明示『本任务写授权（批准后
   后续写免停）』」，实测 SSE `confirm.request` 只携 `tool` + `arguments`（与
   `run_command` 常规确认同形）。**机制正确**（首停-approve-免停、reject 不置位
   均真机成立），差的是 UI 文案；补文案要动 `Run.request_confirm` 形参 + 前端
   确认卡 → 归 M4 宿主层。触发信号＝M4 面板落地时一并补。
5. **证据闸有绕行路径**：闸只在 `spawn_step` 自动标 done 这条路径生效；当
   `make_plan` 的 tools 未声明 `spawn_step` 时，模型可走 `spawn_subagent` +
   `update_plan_step` **手工**标 done 绕过（§5 落点本就只裁 spawn_step，属既定
   范围而非实现偏差）。触发信号＝首例「手工标 done 掩盖无证据步骤」造成实际
   损失，再把闸下移到 `update_plan_step`/`finish_plan` 侧。
6. **闸拦下后模型会空转到熔断**：子任务结论天然不含锚点时模型无合规出路，
   实测重试至 21 次续跑触发 M1 保险丝落 `blocked`（单均账单最高一单 ¥0.30）。
   「宁可 blocked 不假 done」＝设计意图；触发信号＝真实任务反复出现「闸→熔断」
   空转，再在返回串里给出可走的下一步（如「先推进其他步骤或如实汇报受阻」）。
7. **主 agent 任务的 `authorization` 字段易误读**：不绑技能的任务视图显示
   `readonly`，而实测其 `write_file` 免确认直写（§3「档位只作用于技能任务」）。
   字段语义＝技能档位，无技能即无档位实效。触发信号＝前端面板上线时决定是否
   对 `skill` 为空的任务隐藏该字段。

证据、账单与逐项结论见 [m3-acceptance-report.md](../../m3-acceptance-report.md)。
