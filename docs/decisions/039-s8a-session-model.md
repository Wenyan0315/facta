# 决策记录 · S8a 会话模型重构：身份=文件名，多会话并发跑（2026-09-24）

> S8 第一站（C1）：把「当前会话」从服务端主语里删掉——多段对话可真并发，长任务在跑时另开一段问点别的。返回 [architecture.md](../architecture.md)

- **立项依据**：待讨论区「多进程演进」议题（2026-09-21）的**关键修正**——「长任务期间继续聊」是**并发**需求不是进程需求。三分法里本轮只解①并发：agent 负载 IO-bound，线程即可，GIL 不碍事；②隔离已由 S6a worktree 承担；③独立生命周期留 S8b（IM 渠道/常驻）。同议题的 **C 径「Run Store 外置」被收窄**：不动存储介质，只把**单锁口径从「全进程一把」收窄到「一段对话一把」**——内存 Run Store 足以支撑个人规模的并发，外置触发信号（多实例部署）未到。本条即该议题的结案依据之一。

- **八项拍板**（排法 C：分两拍，C1 后端模型 + 前端最小适配，C2 前端整体迁 Preact）：
  - **#1 会话形状 C**：会话仓库是「id → Session」的字典语义，**身份 = 文件名**（`%Y%m%d-%H%M%S`，同秒冲突追加 `-1`/`-2`）；**废掉 active 固定位**（`data/memory/session.json` 退役为一次性迁移源）；生命周期 live/running/archived 是**内存概念**，不是磁盘位置。
  - **#2 并发上限可配，默认 3**：`CORTEX_MAX_CONCURRENT_RUNS`。上限是**资源约束**（每个 Run 一个线程 + 一条模型长连接，无上限会让「开 20 个标签页」变成 20 路并发），不是正确性约束——正确性由 #1 的会话内准入保证。
  - **#3 补锁范围 = 全并发面清点**：不接受「想到哪补哪」。S8a 让多会话 worker 真并发，**进程级单例第一次面临多线程**——先清点再动手（清单见下）。
  - **#4 前端借机迁 Preact**：但**不与 C1 捆绑**——C1 只做最小适配（vanilla app.js 跟上破坏性 API 变更），整体迁移是 C2。理由：模型重构的正确性风险已经够大，再叠一次前端重写会让「哪一层坏了」无法归因。
  - **#5 排法 C 分两拍**：同上，C1 可独立验收（真机冒烟 + 462 测试），C2 可独立回滚。
  - **#6 固化 = 增量 + 阈值**：老口径「归档/退出时全量固化一次」随归档动作一起消失，触发点必须换成「攒够就固化」——`CONSOLIDATE_THRESHOLD`（默认 20 条消息，env 可调）+ `consolidated_upto` 游标。否则一段长对话的记忆永远不落 `learned/`。
  - **#7 per-session Agent 工厂**（用户在三个解法里拍板推荐项）：`AppContext.build_agent(session) -> Agent`，每轮现造。**否决「驻留表」方案**（per-session agent 常驻 + LRU 驱逐）：驻留表会带来三件本可不存在的东西——驱逐策略、跨请求的 per-session 锁、以及 learned/用户记忆快照陈旧（本轮刚固化的记忆要等重启才进 prompt）。现造的代价只是每轮多读几个 md 文件拼 prompt，换来「agent 与会话同生共死」这条强不变量。
  - **#8 磁盘布局 A**：所有会话同住 `data/memory/sessions/`，`collapsed`（前端「收起」标记）作为 Session 可选字段落盘，折叠 UI 留 C2。**否决按生命周期分目录**（`live/` + `archived/`）：位置一旦承载状态，「收起/归档」就又变成 move 操作——正是本轮要删掉的那类代码。

- **删掉的代码**（本轮的净收益主要在这里）：
  - `archive_session` / `restore_session` / `list_archived_sessions` 三个「搬文件」的函数整体下线，`SessionStore` 只剩 create/load/save/delete/list_metas 五个**单文件级**操作——没有跨文件原子性要求，因为「一段对话只有一个物理副本」从「靠 move 顺序保证」变成「靠位置唯一保证」（根本没有第二处可放）。
  - `POST /api/sessions/{name}/switch` 端点删除：服务端不再有「当前会话」这个状态可切。
  - `ensure_persona` 的调用点从**三个**（服务启动 / 归档清空后 / 切回换血后）收成**一个**（`build_agent` 工厂内部）——「有 agent 但没人设」这个状态在结构上不存在了。三个调用点时期真实复踩过英文回复再现。
  - CLI `/new` 从「原地 clear + 补种人设 + 重置计划板」变成「另建 Session + 重造 agent」：「绝不 rebind `session.messages`」那条列表身份陷阱纪律的适用范围随之缩回 `run_chat` 内部（闭包与会话同生共死，不再需要迁就常驻 agent）。

- **API 破坏性变更清单**（C1 一次性改完，不留兼容垫片）：
  | 变更 | 旧 | 新 |
  |---|---|---|
  | 新建会话 | `POST /api/sessions/new` | `POST /api/sessions` → 201 `{"id": ...}` |
  | 历史消息 | `GET /api/messages`（隐含「当前会话」） | `GET /api/sessions/{sid}/messages` |
  | 切换会话 | `POST /api/sessions/{name}/switch` | 删除（前端概念） |
  | 清单项字段 | `name` / `current` | `id` / `running`，新增 `collapsed` / `time` |
  | Run 与会话 | Run 无会话归属 | `POST /api/runs` 请求体可选 `session_id`（省略=后端新开一段）、响应必带；`GET /api/runs?session_id=` 可过滤 |
  | SSE 事件 | —— | 新增 `run.settling`（收尾阶段：补标题 + 固化都要调 LLM，终态前有几秒静默，显式说出来别让人以为卡死） |

- **并发正确性：用策略代替锁**（本轮的核心裁定）。同一段对话的独占**不是靠锁**：
  - **准入**：`RunStore.create_if_idle(sid)` 在单锁内原子完成「检查 + 创建」，同一会话已有 in-flight（`pending`/`running`/`waiting_approval`——**等确认也算在跑**，人在裁决时会话仍被占着）即拒，返回**原因串**而非 None（前端要按「该会话在忙」vs「全局满了」显示不同文案）。
  - **写操作对 in-flight 会话返 409**（`_require_writable`）：worker 在整轮里独占这段对话（进场 load、出场 save），此时任何外部写都会在 worker 落盘时被覆盖（lost update）。与其用锁把写排队到几十秒后，不如直接告诉用户「这段对话正在被写」。
  - **理由**：临界区是**一整轮对话（几十秒）**，排队没有意义。`SessionStore` 因此保持无状态门面（只持一个目录路径），锁不在这一层。
  - **收尾顺序是正确性约束**：`settle_session`（补标题 → 增量固化 → 落盘）放在 worker 的 `finally`，且**必须先于 `run.finish`**——Web 壳是常驻进程没有 CLI 的退出钩子，不收尾就在服务被杀时丢掉整轮对话；而 finish 一推终态就释放准入，同会话下一轮可能立刻 load，save 落在 finish 之后就是 lost update。游标推进只在固化没抛异常时做（宁可重复萃取也不丢记忆）。

- **c1-6 全并发面清点表**（#3 的产出）。**必修 5 处**（新增 5 把锁）：
  | 位置 | 症状（不锁会怎样） | 锁范围 |
  |---|---|---|
  | `core/gateway.py` 精确缓存 LRU | `move_to_end` 撞 `popitem` 会 KeyError，而调用点不在重试 try 里——**一次已成功的调用会整体炸掉** | `_cache_lock` 只圈 `_store` 的读改写；读侧不锁 |
  | `tools/mcp_client.py` | `_request` 的「发号 → 等自己的号」必须原子：撞号会互取响应，且「跳过他人响应」是**取走不归还**，被跳过方只能干等到超时 | `_call_lock` 全程持有 |
  | `memory/learned.py` + `consolidate.py` | 面板编辑的「读→改→写」（整文件重写）会**抹掉窗口期内固化刚 append 的行** | 共用 `LEARNED_LOCK` 圈住整重写与批量 append。**v1 记的「单用户并发概率极低」前提已被 S8a 推翻**：固化从「退出时一次」变成「每 20 条一次」，与面板编辑撞车的窗口大幅变宽 |
  | `core/audit.py` | 多 worker 交错的 write 会把一行 jsonl **撕成两半**，审计流不再可解析 | 补上原作者留位的 `_lock` |
  | `tools/worktree.py` | 两个会话的 spawn 同时合回会互踩主仓库 `.git`（index/HEAD） | `WORKTREE_LOCK` **只圈碰主仓库的命令**（worktree add / merge / remove / branch）；worktree 内的 add/commit 走私有 index，保持并发 |

  **刻意不锁 4 处**（理由写进代码，不只在决策记录里）：
  1. `core/telemetry.py` 账本计数——`x += 1` 在 CPython 里是读-改-写三步，并发下计数是**近似值**。不锁的代价权衡：10 个 `record_*` 各包一层 `with` 噪音大，dataclass 塞 Lock 字段还会污染 `==`/`repr` 语义（测试要比对账本）。账单是参考值不是结算账目，误差量级 = 并发度。真要精确账目得上进程外聚合，那是多进程演进的事。
  2. `core/gateway.py` 熔断三态——竞态后果只有「多放行一次试探 / 少记一次失败计数」，都是软指标不炸流程；而要保证三态原子就得**把网络调用圈进临界区**，一个慢请求堵死所有会话，代价远大于收益。熔断本就是启发式护栏，不要求精确。
  3. 向量库——worker 侧只读，`sync_notes` 只在启动跑（单线程），无并发面。
  4. notes/files 工具的语义冲突——两个会话同时写同一个文件是**产品语义问题**（谁后写谁赢），加锁只能把冲突变成排队，不解决「谁的版本对」。S6a worktree 才是这个问题的答案（隔离而非串行）。

  **没有为补锁写并发测试**——有意裁定：竞态难以稳定复现，养一批 flaky 测试的代价高于收益；正确性由「锁范围最小化 + 理由写在代码里」保证，回归由既有 462 个单线程测试守住「加锁没改语义」。

- **前端 C1 方案：脱钩 + 重放重挂（零后端改动）**。「当前会话」降级为纯前端概念：
  - 切走 = 关 EventSource（`detachStream`），**Run 在后端照跑**——这正是多会话并发的意义，不再需要「有任务在跑就不许新开」的守卫。
  - 切回 = `GET /api/runs?session_id=` 找 in-flight Run（前端复用后端的 `_IN_FLIGHT` 口径）→ 新建 EventSource 挂上去。**借的是 S2b 已有的能力**：新建连接不带 `Last-Event-ID` ⇒ 后端从 seq 0 全量重放，本轮内容一件不落。
  - 清单用 `running` 字段标 ▶（切走的那段可能还在后台跑）；在跑的会话后端会 409 ⇒ 重命名/删除按钮**直接不给**（而非点了报错）。
  - 已构建的 FW 站 `tasks.js` 无需改动：它只读 `GET /api/runs`，响应字段是**新增**不是改名。

- **实机冒烟结果**（mock provider，/tmp 沙箱，真 HTTP）：事件序 `run.started → text.delta… → run.settling → run.completed`；同会话重发 409；`POST /api/runs` 省略 session_id 后端新开一段（**id 撞号自动加 `-1` 后缀**，S2 验收修复轮#3 的血泪逻辑在「快速连点新建」场景下原样管用）；空会话标题「（空会话）」且**进清单**（老布局靠「归档前空会话守卫」把空壳挡在清单外，那是 move 语义的补丁不是产品需求）；`PUT` 重命名 200、`DELETE` 200 → 再删 404；非法 id 边界——`abc` / 带 `.json` / `%00` 均 **400「非法会话 id」**，编码穿越 `..%2F..%2F..%2Fetc%2Fpasswd` **404**（框架层拦在路由匹配）。**id 白名单 `_ID_RE` 是信任边界**：id 就是文件名，而文件名来自 HTTP 路径参数，不校验等于把 `../../etc/passwd` 交给 Path 拼接；格式非法直接崩（`ValueError`），由 `_require_session` 翻成 400 而不是以 500 漏给客户端。

- **一次性迁移**（`migrate_legacy_active`）：只搬老 active 固定位那**一个**文件——老布局的另一半（归档仓库 `SESSIONS_DIR`）就是新布局的会话目录本身，且归档文件名本就是合法 id，**老归档在 S8a 眼里已经是正常会话，原地不动即完成迁移，历史清单一条不丢**。搬迁 = `shutil.move`（不读不解析不重写，坏文件也照搬，清单读取时才跳过）；按 mtime 分配 id，迁移后它在清单里的位置跟迁移前一致；幂等（源文件不存在返回 None）。

- **实踩与已知边界**：
  1. **`paths.py` 全是相对路径**（`data/notes`、`data/learned`、`data/memory/sessions`、`data/graph.json`），依赖启动 cwd；只有 `WORKSPACE_ROOT`/`WORKTREES_DIR` 用 `__file__` 锚定。冒烟时换 cwd 启动直接崩（`scan_notes` 对缺目录抛 FileNotFoundError）。这是既有「必须从仓库根启动」边界的另一面——**要不要让它成为启动即报的友好错误，挂待讨论区**。
  2. `Run.emit` 返回 `None` 而非 `RunEvent`：emit 的一个主要用途是直接当 `run_turn` 的 `on_text`/`on_event` 回调，而那两处要 `Callable[..., None]`——返回事件会让 lambda 形式过不了类型检查。要读事件走 `run.events`。
  3. `ScriptedLLM` 剧本耗尽**不抛异常**而是返回兜底文本：多会话测试里剧本条数容易算错，抛异常会让失败信息指向「剧本不够」而非真正的行为断言。
  4. `load_session` 里 `consolidated_upto` 的缺省取 `len(messages)` 而非 0——老文件是在「退出时全量固化」的旧语义下写的，历史已萃取过；给 0 会让第一次增量固化把整段历史**重烧一遍 LLM**（learned/ 长出重复条目）。
  5. 损坏文件跳过不炸清单（写盘中途被杀会留 partial write）：清单读取是展示路径，不该被一个坏文件整垮。
  6. `SessionMeta` 为拿标题仍要 load 整个文件（教学规模足够）；升级触发信号 = 文件数 >20 或单文件 >10MB，届时换只读文件头的索引。

- **反方意见**：
  1. **无驻留表 = 每轮重造 agent 的开销**：每轮多读几个 md 拼 prompt + 重建 registry（搬运母 registry 的 Tool 对象引用，不重启 MCP 子进程）。应对：个人规模下这点开销远小于「长任务期间能另开一段聊」的价值；真成为瓶颈时再加缓存，而缓存的正确性前提（learned 快照失效策略）比现在复杂得多——**先证明它慢，再优化**。
  2. **准入用 409 而非排队，体验上可能「点了没反应」**：应对：前端在跑的会话**不给**重命名/删除按钮，同会话发送在客户端就被 `currentRunId` 挡住；409 只兜多标签页并发这条路，且带原因文案。
  3. **补锁没有测试覆盖**：见上「有意裁定」——但这条是真实的债，登记在此。若将来出现「并发下数据错乱」的实踩，第一嫌疑就是这 5 把锁的范围划错，届时补 kill-race 场景而非 flaky 单测。
  4. **API 破坏性变更一次性改完、不留兼容垫片**：应对：唯一消费者是自家前端（同一 commit 系列内改完）+ FW 站已确认无需改动；项目未对外发布，兼容层是纯负债。

- **触发信号（未达即不扩编）**：
  - Run Store 外置（SQLite/Redis）：多实例部署，或重启后需要看到历史 Run 清单。
  - 会话驻留/agent 缓存：实测「每轮重造」成为可感知延迟。
  - `SessionMeta` 索引化：会话文件数 >20 或单文件 >10MB。
  - 前端折叠 UI（`collapsed` 的写入端点）：C2 迁移时一并做——字段已落盘，端点未开。

- **验收**：pytest **462 passed, 2 skipped**（+5 vs S7b 的 457）；ruff / mypy（57 files）全绿；CI 与本地同三门无需改动。两个 commit：`3ff31fb`（后端模型 + 补锁，21 文件 +1073/−819）、`a325e41`（前端最小适配，+117/−49）。**真机验收（真模型 + 人眼）待做**：长任务期间另开一段对话聊 / 多标签页同时开不互抢。
