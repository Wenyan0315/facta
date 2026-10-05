# ADR 076：reflect 全面异步化——固化不再挡执行主轴

- 状态：**已落地**
- 日期：2026-10-05
- 立项出处：[competitive-roadmap P2-1](../internal/competitive-roadmap.md)（记忆补全三件套之三：reflect 全面异步化，不阻塞执行主轴）
- 关联：032（裁定四：反思必须保持异步旁路）、074（learned 四态——固化管线的写入端）、P1-5 评审修复（settle 顺序约束）

## 背景

032 裁定四已认定「固化管线在退出复盘时触发、不挡主响应」的方向正确，但
现状只算半异步：触发点虽在回复交付之后，执行却仍挡在两条关键路径上——

- **Web**：`settle_session` 跑在 `run.finish` 之前——用户已收完回复文本，
  但 run 终态迟迟不推（前端不能解锁）、同会话下一轮 409，标题 + 固化
  两次串行 LLM 的耗时全由用户承担；
- **CLI**：`/new` 换新与退出都要先等 `settle_session(flush=True)` 跑完。

roadmap 点名「全面异步化」：把 LLM 重活从两条主轴上摘走，且不能踩回
P1-5 修过的坑（固化失败拖死对话保存），也不能挖新坑（异步落盘与
下一轮的 lost update——同会话下一轮在准入释放后立刻进场 append）。

## 决策

### 甲：拆「保底」与「重活」——保底留主轴，重活移终态之后

`settle_session` 原来的顺序约束（①保底 save → 标题/固化各自容错 → ②终态
落盘）升格为**分工**：保底 save 从函数里移出去、归调用方，函数只剩
LLM 重活 + 窄写终态——异步时序下函数内任何全量 save 都是覆盖炸弹
（下一轮此刻可能已进场 append，函数首行的「保底」会把它盖掉，测试
故障注入实测复现过）。两个壳的调用时序：

- **Web worker finally**：`emit(run.settling)` → **保底 save（毫秒级）→
  `run.finish`（终态事件 + 准入释放）→ settle 重活（同线程继续跑，
  从此没有人在等它）**。「读到 run.completed 时盘上必有这轮对话」的
  既有验收不变——保底仍在 finish 之前。
- **CLI**：`/new` 前同步保底 save（毫秒级），标题/固化丢后台线程
  （新旧会话是不同 sid 文件，结构性无竞态）；退出前 `finally` join
  全部后台线程——flush 语义（宁可退出慢几秒不丢记忆）与账单完整性
  （固化 token 计入 bill）保住。

### 乙：终态写改「窄写合并」——settle 只拥有 title 与游标两个字段

异步化后 Web 的 finish 即释放准入，同会话下一轮可能立刻进场 append——
settle 若仍全量 save 本轮快照，会把下一轮新消息覆盖掉（P1-5 的病以
lost update 的新形式复发）。因此 `settle_session` 的 ②终态落盘从
全量 save 改为窄写合并：

- `SessionStore` 新增 `update(sid, fn)` 原语：**per-sid 锁内** load →
  fn → save；`save` 也持同一把锁（把「update 的读改写窗口」与
  「任何并发 save」串行化，锁域毫秒级）。
- 窄写规则：
  - **title 只在 `fresh.title is None` 时填入**——用户 rename 优先，
    与「自动生成用于填空，不覆盖用户主动编辑」的既有纪律一致；
  - **游标单调推进，且仅当 `len(fresh.messages) ≥ 目标值` 才推**——
    下一轮进场后只 append（前缀共享）时精确正确；fresh 若已被压缩
    截短（位置语义撕裂）就不动，下轮重烧——重复优于跳过（P2-7 语义）。

所有权分界由此显式化：**`messages` 的写权限永远在轮次 worker 手里，
settle 只窄写 title 与固化游标**——丢消息在结构上不可能。

### 丙：测试同步点 = `Run._settle_done`

Web 侧 settle 移到 SSE 流 sentinel 收口之后，测试不能再靠「读完事件
流」推断收官完成。`Run` 增补一个 `threading.Event`，worker finally
末尾 set——生产逻辑零参与，纯测试同步点。

## 边界（诚实登记）

- **游标回退竞态（自愈型）**：下一轮 worker 若在 settle 窄写**之前**进场
  load，其后续全量 save（快照里的旧游标）会盖掉窄写推进的游标 → 下次
  settle 重烧同一批 → learned 可能重复几条。只发生在「finish 后秒发
  下一轮」+「固化 LLM 尚未跑完」的叠加窗口；错向是**重复不是丢失**
  （P2-7 兜底），且 messages 不受影响（窄写永不碰）。修剪靠人工，
  防复发靠触发信号。
- **learned/user.md 脏读（CLI）**：后台 settle 在 `LEARNED_LOCK` 内
  append learned/ 与 user.md 时，主线程新会话 `build_agent` 的快照读
  不持锁——最坏读到半行截断条目进 prompt，语义伤害趋零，不崩。
- **锁为进程内**：per-sid 线程锁不跨进程（CLI + Web 同跑仍属 ADR 071
  登记过的已知边界，同源继承，不另修）。

## 验收

三门全绿；新增窄写合并测试（下一轮消息不丢、游标单调不越界、title
不被 rename 覆盖）；`test_settle_sets_llm_title` 适配 `_settle_done`
等待；CLI 侧 `/new` 后台化 + 退出 join 保底。对应 roadmap P2-1 验收
「记忆操作零阻塞主循环」：Web 终态推送与准入释放不再等固化，CLI 换新
会话不再等固化。

## 触发信号

- learned 重复条目成批出现（重烧浪费可观测）→ 议 RunStore 级 settle
  准入，或下一轮 worker 进场时 join 上一轮的 settle
- CLI prompt 注入出现截断条目 → 议 `build_agent` 快照读套 `LEARNED_LOCK`
