# 决策记录 · Run checkpoint 落地方案与范围裁定（2026-09-25）

> 竞品对标 P0-3 实现篇：[038](038-run-checkpoint.md) 定了语义（借 LangGraph checkpoint/resume、不引框架、副作用按幂等标注处理），本篇记**落地时新做的裁定**——038 没定的那些。返回 [architecture.md](../architecture.md)

- **来源与依据**：038 的四拍板是语义层的（写入点、resume 去重、粒度、覆盖范围），落到代码上仍有一批必须现场决定的事：checkpoint 到底存什么、恢复是重放还是补洞、幂等标注怎么表达、写盘同步还是异步、要不要开 HTTP resume 端点。这些决定一旦写下就变成后续维护者的既成事实，按设计原则第 5 条（实现后必回写决策记录）单独成篇。代码落点：`src/agent/orchestrator/checkpoint.py`（新模块）、`loop.py`（事件带 call_id + append 早于 emit + `user_text=None` 续跑）、`memory/store.py`（原子落盘）、`tools/registry.py`（`Tool.idempotent`）、`server/app.py` 与 `__main__.py`（两个入口接线）。

- **七裁定**：
  - **P1 底片是唯一真值源，账本只存底片表达不了的两件事**：`session.json` 照旧是完整上下文（038 P1 说的「重建上下文的最小状态」就是它），在工具边界原子落盘（tmp + `os.replace`，半截文件不可能被读到）；新增 `data/checkpoints/{sid}.jsonl` append-only 账本，只记 `intent`（发起事实）与 `result`（工具结果全文）。**否决「另存一份状态快照」**：那会造出第二个真值源，两者不一致时没有仲裁规则。账本存在的唯一理由是——底片尾部悬挂时，「这条调用发起过没有、结果是什么」在底片里查不到（发起事实是 assistant 消息，结果压根没写进去）。
  - **P2 resume = heal 补洞 + 向前走，不重放任何调用**：038 P2 原文是「重放按标注去重」，落地改成不重放——`heal()` 把底片尾部悬挂的每条 `tool_call` 补成一条合法 tool 消息（有 result 的原样回注；只有 intent 的按幂等性告诉模型「可安全重做」或「先核验现场」；账本无记录的告诉它「通常意味着没跑过」），然后 `run_turn(user_text=None)` 从现场继续走。副作用不重复由「压根不重放」保证，不靠去重表（去重表要维护 call_id→结果映射、还要处理部分成功与嵌套 spawn，复杂度不划算，而 LangGraph 的公开教训正是重放这条路踩出来的）。幂等标注因此从「去重开关」降级为「给模型的措辞依据」，但仍然必要：它决定 heal 写哪一档文案。
  - **P3 `Tool.idempotent` 是新字段，只读工具免声明**：不复用 `is_readonly`（语义不同：只读说的是「不改世界」，幂等说的是「重复改同一处世界结果一致」，`write_file` 是写类但幂等）。判定处按 `is_readonly or idempotent` 合并，所以只读工具无需逐个标 True。已标 6 处写类可重做工具：`write_file` / `complete_todo` / `update_todo` / `delete_todo` / `update_plan_step` / `sync_graph`。默认 False = 保守档（未标注的一律按「先核验现场」处理，宁可多一次只读核验，不冒重复副作用的险）。
  - **P4 落盘顺序是不变量：append 早于 emit**：`tool_started` 先写 intent 再 save 底片（反了就会出现「底片说发起过、账本查不到」——恰好是 heal 最需要的证据丢了）；`loop.py` 里事件一旦外发，盘上已经有这件事（`CheckpointWriter.on_event` 在 `run.emit` 之前）。这条不变量由 `tests/test_checkpoint.py` 钉住，改序即红。
  - **P5 账本每轮截断重写**：`begin(run_id)` 直接截断该会话的账本文件。**ponytail 裁定**——历史 run 的账本没有消费者（heal 只看最后一轮，人工排查另有 `data/audit/`），留着只会长出「清理策略 + 磁盘增长」两件不必要的事。代价是「上一轮为什么中断」查不到，需要时再改成保留 N 轮。
  - **P6 同步写，否决 038 反方 2 的「异步写」**：事件粒度（非每 token）下一轮十几次 append + 若干次原子 save，实机无感；异步队列恰好与本模块的目标矛盾——崩溃时队列里没 flush 的正是最需要的那几条。符合 038「实测开销超阈值再优化，不预先工程化」。
  - **P7 不加 HTTP resume 端点**：范围裁定。Web 侧下一次 `POST /api/runs`（`user_text=None`）自动走 heal，CLI 侧启动时 heal 一次（覆盖「上次是 Web 被杀留下的残局」这种共享 session 的情况），evalkit 直接调 `heal + run_turn`——**没有消费者需要单独的 `/resume`**。前端也没有 resume UI。等 S8 常驻进程或前端要做「一键续跑」时再开。
    > **口径修正注记（2026-10-09，[102](102-n07-hygiene-dispositions.md)）**：本条的「Web 侧 `POST /api/runs`（`user_text=None`）」在 **HTTP 边界不成立**——`CreateRunRequest.text` 是必填纯字符串，API 层表达不了 None，空串 `""` 只会原样传给 `run_turn` 追加一条空用户消息（实测模型回「这条消息是空的」）。**续跑机制本身工作如验收记录**（`run.healed` + 副作用零重复）；真实的续跑 UX＝用户再发一条消息（如「继续」），heal 在 worker 进场时补齐悬挂轮次。`user_text=None` 只在内核/evalkit 直调路径可达。原文留档不改写。

- **CLI 为什么不挂 writer**：CLI 只在轮末 `settle_session` 落盘，中途被杀时 `session.json` 仍停在上一个干净边界，压根没有残局；Ctrl+C 路径已有 `trim_incomplete_round` 收尾。所以 CLI 只在启动 heal（读账本），不在执行期写账本——写了对它自己没用（它不会产生悬挂），留着只是白付 I/O。

- **反方意见**：
  1. **result 全文进账本 = 大输出写两份**（底片一份、账本一份）——应对：账本每轮截断（P5），长期占用只与最后一轮成正比；真出现磁盘压力再改「result 只存指纹」，但那会让 heal 失去原样回注的能力，属于用正确性换空间，先不做。
  2. **heal 只检尾部悬挂，中途轮次不管**——应对：中途不可能悬挂（每个 `tool_started` 都 save 底片，P4），尾部是唯一可能的残局位置。这不是简化，是事实。
  3. **不重放 = 崩溃瞬间正在执行的那条调用结果永久未知**（intent 有、result 无）——非幂等工具只能让模型自己核验现场（读文件、查状态），可能多花一次只读调用；幂等工具直接告诉模型可以重做。接受：比重复执行一次副作用便宜得多。

- **验收（038 验收四条对账）**：
  - ✅ evalkit kill-resume 场景通过：`evals/resume_eval.py` + `evals/scenarios/kill_resume.jsonl`，三段式父子进程（kill 段真模型 + 真 `run_command`，数到第 N 次 `tool_result` 落账后 `SIGKILL` 自杀；resume 段 load→heal→续跑；父进程 verify）。实机 deepseek-flash **2/2 通过**，且父进程断言 `heal > 0`（否则场景没走到恢复路径，悄悄 pass 等于验收作废）。「副作用不重复」的可观测载体是累积量（追加三行 / 计数器 = 3），write_file 覆写语义看不出来所以场景一律用 shell 累积。
  - ✅ checkpoint 文件人工可读（JSONL，一行一条 `{type, ts, ...}`）。
  - ✅ pytest / ruff / mypy 三道门全绿（484 passed, 2 skipped；新增 `tests/test_checkpoint.py` 13 个离线测试钉语义——账本读、写序不变量、result 不落底片、begin 截断、heal 四档文案、只补未答那条、端到端「续跑不重复执行副作用」）。
  - ✅ 实机轮真模型长任务 kill 一次验证（2026-09-25，Web 入口）：`.venv/bin/python -m agent.server`（deepseek-flash，127.0.0.1:8000）→ HTTP 建 Run 跑「分三次 `echo >> log.txt` 追加 one/two/three」→ 第 2 次 `tool.result` 落账后对服务进程 `SIGKILL`（退出码 -9）→ 现场取证：底片尾部 `assistant tool_calls=1` 悬挂、只回填了前一条 tool 消息，账本 5 行 `run/intent/result/intent/result`（无 `done`）→ **换一个进程**重启服务、同会话续跑 → `run.healed` 补齐 1 条 → 模型接着追加 two、three → `run.completed`，文件恰好 `['one','two','three']`。落在 heal 最强档（intent+result 都在 → 原样回注，压根不重放），L2 确认走 `/api/runs/{id}/confirm` 端点批准，副作用零重复。
