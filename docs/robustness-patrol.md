# src/facta 全仓健壮性巡检报告（2026-11-01）

巡检范围：src/facta 八个子包（core / memory / tools / knowledge / orchestrator / server / github_bot / evalkit）+ 顶层四个文件（review.py / paths.py / cli.py / __main__.py），并交叉核对 tests/ 下对应测试。

判据：只收录能落到具体代码事实（文件、行号/函数、触发条件、预期 vs 实际偏差）的问题。未经代码事实支撑的疑点不收。**本报告全部结论来自静态代码阅读，未运行测试复现**；标注明细见各条「证据强度」。

---

## 一、高危问题

### H1. `RobustLLM.generate_stream` 调用方提前停止消费时，记账与熔断复位被跳过
- 位置：`src/facta/core/gateway.py`，`RobustLLM.generate_stream`
- 触发：流式调用已 yield 出第一块后，调用方 close() / 抛 GeneratorExit / 未消费完就发起下一次请求，生成器停在 yield 处。
- 预期 vs 实际：预期每次成功调用都留账；实际尾部 `record_llm` / `_on_success` 不执行，成功调用既不计成功也不复位熔断计数，`call_ledger` 也丢记录。
- 证据强度：生成器语义级可证实，未跑测试。

### H2. 首块 `StopIteration` 破坏 PEP 479 语义，空流按可重试失败处理
- 位置：`src/facta/core/gateway.py`，`RobustLLM.generate_stream`（读首块的 `next(stream)` 处）
- 触发：空流，或候选 `generate_stream` 在 yield 之前抛 StopIteration。
- 预期 vs 实际：预期失败统一以显式异常上抛；实际 StopIteration 被 `except Exception` 捕获、因无 status_code 被判可重试，最终 `raise error` 越过生成器边界会被转成 RuntimeError 或被外层 for 当作正常结束静默吞掉。

### H3. `setup`：MCP 子进程只在成功路径关闭（github_bot）
- 位置：`src/facta/github_bot/worker.py`，`run_fix_issue` / `run_review_pr`
- 触发：`run_turn` 抛异常、`result != COMPLETED` 提前 return、`_verify_ci_trio` / `_stage_whitelist` / `gh pr create` 任一失败、以及 `DRY_RUN` 提前 return。
- 预期 vs 实际：预期与 `__main__.py`「无论正常退出还是异常崩掉都关掉所有工具服务器—不留孤儿进程」一致；实际这些分支都不遍历 `ctx.mcp_clients` 调 `close()`，留下孤儿子进程。

### H4. 同步端点 `?timeout=` 无上限，可打满线程池致服务假死
- 位置：`src/facta/server/app.py:706`（Query 只校验 `gt=0`）、`app.py:743`（`run._settle_done.wait(timeout=timeout)`，未传时 `timeout=None`）
- 触发：外部自动化/脚本漏传 timeout 并发多次调用普通同步端点。
- 预期 vs 实际：预期有上限或默认超时；实际无限阻塞占用 FastAPI 默认线程池 worker，池满后其它 GET 全部排队。
- 测试：`test_app.py:173` 覆盖到期返回 timed_out、`:195` 覆盖 timeout≤0 → 422；「未传 timeout 无限阻塞」与「缺上限」无测试。

### H5. `resume_task` 缺幂等/准入门，可并起两个 worker
- 位置：`src/facta/server/app.py:1057–1077`（状态检查与置 QUEUED 之间无锁，`:1058–1062`）
- 触发：同一任务短时间内两次 resume（双击 / 客户端重试），或任务处于 queued 且 worker 尚未起 Run 的窗口。
- 预期 vs 实际：预期每会话单 in-flight；实际两个 `_task_worker` 同时推进同一 `Task` 与同一 session，`run_ids/billing/continuations` 互相覆盖，后一个被 `create_if_idle` 拒绝后秒级空转重试，撞全局上限后落 blocked。
- 测试：无并发 resume 用例；`test_app.py:1151` 只覆盖终态任务拒 resume。

### H6. 黑名单真值源在 files 与 sandbox 两处消费语义不一致（围栏声明与执行脱节）
- 位置：`src/facta/paths.py:116–125`（`BLACKLIST_DIRS` 含 `WORKTREES_DIR`）；`src/facta/tools/files.py:68`（`rel == banned or rel.startswith(banned + "/")` 相对比较）；`src/facta/tools/sandbox.py:158–160`（`root / d`，d 为绝对路径时 pathlib 直接返回 d）
- 触发：`FACTA_DATA_DIR` 迁出 workspace 后，围栏条目变为绝对路径，`rel.startswith("/abs/...")` 永不命中 —— files 侧黑名单空转；sandbox 侧则按绝对路径正常生效。
- 预期 vs 实际：同一份黑名单在两个消费方语义不同，声明的封锁在执行层落空。

### H7. `KnowledgeBase.search` 对空/空白 query 不短路
- 位置：`src/facta/knowledge/knowledge_base.py:225-226`（直接 `self.embedder.embed([query])[0]`）
- 触发：`query=""` 或纯空白，经 `tools/notes.py:277` 与 `:315` 的 `ctx.kb.search(query, top_k=3)` 生产链路可达。
- 预期 vs 实际：预期零成本返回空列表；实际对真实语义 embedder 发起一次网络 embedding 调用（费用 + 延迟，可能以 API 报错收场）。
- 测试：test_sync / test_graph / test_vector_store 均无空 query 用例。

### H8. `plan.py` 的 `update_step` 不校验状态迁移
- 位置：`src/facta/memory/plan.py`（约 168–195 行）
- 触发：步骤当前为 pending 时传入非终态值，或由 in_progress 回退到 pending。
- 预期 vs 实际：预期只允许 pending→in_progress→终态、终态不可回退；实际只校验「旧是终态则拒」「新是终态需带 note」，非终态之间的迁移不受约束，可反复改。
- 证据强度：test_plan.py（45KB）本轮未读，是否已有测试锁定未确认。

### H9. `__main__.py` 会话 create/load 顺序疑点【待证实，不计入已证实清单】
- 位置：`src/facta/__main__.py:62`、`:94`
- 说明：子 agent 提出 `ctx.store.latest() or ctx.store.create(Session())` 可能使 heal/settle 作用在未落盘的空会话上，但未能读完 `memory/store.py` 的 latest/create/load 实现，按纪律保留为待证实疑点，供后续复核。

---

## 二、中危问题

### core
- **M1 `merge_stream_chunks` 的 index 归并**：`src/facta/core/llm.py` 约 70 行 `idx = frag.get("index") or 0` 把 `index=None` 与缺省都折算成 0；两个工具调用中第一个 index 缺失时参数被拼进同一 slot，拼出错误 arguments。
- **M2 `ScenarioRouter.route` 未知选项被记为成功**：`src/facta/core/jev.py` 约 190 行，picked 不属任何已知选项时先 `record_jev`（计成功并复位熔断计数）再 warning 返回 None，持续异常决策会掩盖真实故障率。
- **M3 `SemanticCacheLLM._entries` 无锁**：`src/facta/core/gateway.py`，进程级单例的 list 在 append/pop(0)/`_best_match` 线性扫描间无锁；同类竞态在 `RobustLLM` 已用 `_cache_lock` 防护，此处未跟进（影响限于淘汰边缘的计数偏差）。

### memory
- **M4 `PlanState.create` 可重复创建**：`src/facta/memory/plan.py` 约 135 行，事件史会出现多个 `plan.created`，`view()` 每次遇到 created 整体覆盖旧步骤；board 层挡住了实际路径，但 PlanState 作为公开单元缺校验。
- **M5 `PlanState` 无终态闸**：`finish` 后仍可 `revise`/`update_step`（约 202 行）；终态不可改只落在 board 层（active 置 None），状态层未落实；`finish` 也不校验 summary 非空。
- **M6 `TodoStore.update_text` 不校验空文本**：`src/facta/memory/todos.py` 约 100 行，空串会写入并落盘；tests/test_todos.py 无空文本用例。

### tools
- **M7 `_run_command` 超时只杀直接子进程**：`src/facta/tools/terminal.py:257–270` 用 `subprocess.run(..., timeout=...)`，无 `Popen` / `start_new_session` / `killpg`；`sh -c "cmd &"` 一类孙进程在超时后残留（`:273` 分支只返回超时字符串）。test_terminal.py:287 只断言输出含「超时」，不检查孙进程存活性。
- **M8 `_spawn_step` 缺 try/finally 兜底**：`src/facta/tools/spawn.py:330–342`，先 `board.update_step(step_id,"in_progress")` 再调 `spawn_subagent`；后者抛非预期异常（非返回 RunResult.FAILED 的路径）时步骤永久停在 in_progress，只能靠 finish_plan 的悬空检查兜住。test_spawn_step.py 无该用例。
- **M9 sandbox 的 .env 遮蔽是 wrap 时枚举**：`src/facta/tools/sandbox.py:178–192` 用 `root.glob(".env*") + root.rglob(".env")` 生成遮蔽清单，命令执行期间新建的 .env 不在围栏内（docstring 亦自述）。
- **M10 `write_note` 不建父目录**：`src/facta/tools/notes.py` 约 265 行，`path.write_text` 前无 `mkdir(parents=True)`，notes_dir 不存在时只能返回「写入失败」，首次在空工作区使用时无法建库。
- **M11 `read_notes` 异常暴露面过窄**：`src/facta/tools/notes.py` 只捕获 FileNotFoundError；目标为目录或非 UTF-8 时异常继续上抛（registry 兜底成「工具执行失败」，但语义上应回「没有这个笔记」）。
- **M12 `search_code` 与 `read_file` 围栏不对称**：`src/facta/tools/files.py:115–134` 的 `_iter_searchable_text` 用 `rglob` 直遍历，不走 `_resolve_in_workspace`（read_file 在 `:97` 走）；仅靠 `.git`/黑名单前缀/`.env` 文件级跳过。实际泄露面收敛到「未被黑名单覆盖的敏感路径」，但仍属机制不一致。已有部分测试（test_files.py:109、test_review_round.py:41），无对称性断言。
- **M13 `_finish_plan` 的 confirm 异常未兜底**：`src/facta/tools/plan.py` 约 380–420 行，`_verify_delivery` 里确认通道抛异常未包 try/except，会把收官炸掉而非降级。
- **M14 `_write_file` 父目录为 symlink 时先建外部目录**：`src/facta/tools/files.py` 约 250–290 行，`mkdir(parents=True)` 先执行，写盘阶段才失败，留下外部空目录。

### knowledge
- **M15 删除安全阀分子分母口径不一致**：`src/facta/knowledge/sync.py:99–104`，`vanished` 按文件名集合计数，分母 `len(stored_hashes)` 按指纹计数，与 `SyncReport` docstring（`sync.py:41`「按内容指纹计数」）自相矛盾；同内容多文件时统计失真。test_sync.py:110–122 只覆盖内容互异的情况。
- **M16 `ChromaVectorStore` 无异常降级**：`src/facta/knowledge/vector_store.py:138–199`，`upsert/delete/query/get_all/clear` 直调 `self._col` 无 try/except，底层失败直接外抛中断同步流程；`clear` 先 get_all 再 delete，大集合内存与往返放大。无并发保护。

### orchestrator
- **M17 `_stuck_check` 阈值与注释口径不一致**：`src/facta/orchestrator/loop.py`，判定用 `streak >= _STUCK_LIMIT - 1`，注释写「达上限即熔断」，实际提前一批熔断。需读 test_run_turn.py 的 stuck 用例定性。
- **M18 压缩窗口边界无显式保护**：`src/facta/memory/compressor.py` 约 180–226 行，窗口内无 user 消息时 `window_start` 向左扩展，`summarized_upto > start` 时 fresh 变空切片并静默复用缓存。

### server
- **M19 `cancel_task` 返回值与真实终态不一致**：`src/facta/server/app.py:1046–1049`，running 任务只置 `cancel_requested`，`_task_verdict` 之后才写 TASK_CANCELLED，响应里的 `task.status` 仍是旧值 running；Run 终态与任务终态不同步。测试无该分支断言。
- **M20 `_fuse_guard` 看守线程滞留**：`src/facta/server/app.py:496` 以 `time.sleep(30)` 轮询，任务终态后最多滞留 30 秒；resume 会再起一只，短窗口并存。
- **M21 `RunStore._runs` 只增不删**：`src/facta/server/run_store.py`，Run 结束后无注销或上限，长跑进程 `list_runs` 线性增长。
- **M22 `_settle_done` 未在 finally 中 set**：`src/facta/server/run_store.py`，worker 在 settle 前因未捕获异常退出时，`create_run_sync` 会永久等待（与 H4 叠加放大）。
- **M23 SSE 断连依赖 GC close**：`src/facta/server/app.py` 的 `events`，`finally: run.unsubscribe(q)` 依赖生成器被 close，队列引用可短暂滞留（未见到硬泄漏证据）。

### github_bot
- **M24 CI 判定用子串匹配**：`src/facta/github_bot/worker.py` 的 `_verify_ci_trio`，ruff 判绿靠 `"All checks passed" in lint_out`；pytest 判红靠 `"failed" in out or "error" in out`，任何含 error 的用例名/日志都会误判。
- **M25 Progress 回调无异常隔离**：`worker.py` 的 `Progress.event`/`flush`/`post` 无 try/except，`gh api` 失败（限流、token 失效）异常会从 `on_event` 冒泡打断 `run_turn`，而进度评论本应只是旁路。
- **M26 review 正文临时文件不删除**：`worker.py` 的 `run_review_pr` 用 `NamedTemporaryFile(delete=False)`，从不删除；`gh pr review` 失败时同样残留。

### evalkit / 顶层
- **M27 `FACTA_DATA_DIR` 相对路径按 cwd 解析**：`src/facta/paths.py:34–38` 只做 `.resolve()`，无相对路径/尾斜杠/不存在目录处理；相对路径随进程启动 cwd 漂移，与模块自述「不依赖从仓库根启动」冲突。test_paths.py 未覆盖这四种边界。
- **M28 `DATA_ROOT` 加载期结算 vs 延迟求值不一致**：`paths.py:34–38`（import 期固定）与 `:70–82`（`user_memory_path()` 每次重读 env）时机不同，已 import 的模块拿到旧值。
- **M29 `cli.py` 的 `plan.step_updated` 直接取 key**：`src/facta/cli.py:36` 用 `data['id']`/`data['status']`（`plan.revised` 已用 `.get` 防御），事件缺字段时 KeyError 冒泡出 on_event 缝。
- **M30 `precision_at_k` 的 k<=0 除零**：`src/facta/evalkit/ranking.py:27-28`，k=0 抛 ZeroDivisionError，而同文件 `recall_at_k`/`staleness_at_k` 对 k<=0 返回 0.0，契约不一致。test_evalkit.py 未覆盖。

---

## 三、低危问题

- **L1 缓存返回浅拷贝**：`core/gateway.py` 的 `RobustLLM.generate` 缓存命中分支 `replace(cached)` 是浅拷贝，`tool_calls` 列表与缓存共享引用，调用方原地修改会污染缓存。
- **L2 `_breaker_blocks` 表达含糊**：`core/jev.py` 末行 `return self._half_open and False` 恒为 False，靠副作用放行，逻辑等价但易误读。
- **L3 `audit.read()` 无坏行容错**：`core/audit.py` 逐行 `json.loads` 未捕获异常，截断行会让整个读取中断。
- **L4 `todos._save` 固定 tmp 文件名**：`memory/todos.py:66–75` 用 `<name>.tmp` 而非 uuid，跨进程（agent 与 Web 并用）互踩；store.py 已修过同类问题（P1-6），todos 未跟进。
- **L5 `add/complete` 返回对象易误用**：`memory/todos.py`，返回的是落盘前的那份 Todo，继续修改不影响磁盘且无 docstring 说明。
- **L6 `load_session` 对顶层非 dict/list 无防御**：`memory/store.py:129–160`，`raw.get` 抛 AttributeError，而 `list_metas` 只捕获 JSONDecodeError/TypeError/OSError，会穿透炸掉清单。test_store.py 未覆盖。
- **L7 `spawn_subagent` 的 max_rounds 类型错会抛异常**：`tools/spawn.py` 约 200–230 行，传非数字抛 ValueError 冒到 registry，与「错误也返回字符串」的惯例不一致。
- **L8 worktree 收尾措辞混淆**：`tools/spawn.py` 约 170–195 行，confirm 缺失时返回「用户未批准（或无确认通道）」，无法区分「无人审」与「被拒」。
- **L9 diff 措辞误导**：`tools/files.py` 约 200–240 行，新内容为空串时 `splitlines()` 为空，unified_diff 只输出头，shrink 提示把「清空」描述成「少 N 行」。
- **L10 `todo.py` 的 int() 二次转换**：`tools/todo.py` 的 complete/delete/update 对 `todo_id` 再 `int()`，绕过 schema 传字符串会抛异常而不是走「不存在」提示（registry 能兜住）。
- **L11 `registry._validate_args` 不校验 integer 边界**：`tools/registry.py`，与 history 在函数内部判越界的风格不统一。
- **L12 `registry.register` 静默覆盖**：`tools/registry.py`，同名工具后者覆盖前者；内置名单目前无重名，MCP 与内置工具的重名策略未在本轮核实。
- **L13 Chroma 与 sync 无并发保护**：`knowledge/vector_store.py` 全类无锁；`knowledge/sync.py:127–128` 自述「先增后删」非原子窗口，并发同步会扩大新旧块共存期。
- **L14 `related_notes` 含锚点自身笔记**：`knowledge/graph.py:283` 起，`seen = set(anchors)` 使锚点自己的笔记进入结果；与 docstring 一致属设计行为，但测试只断言 `in`，未显式锁定该语义。
- **L15 `executor._run_parallel` 单元素批走串行**：`orchestrator/executor.py`，`len(batch) > 1` 才并行，与 docstring「连续 spawn 段并行」在单元素批上有偏差。
- **L16 notes 乐观锁非严格 CAS**：`server/app.py` 的 `notes_save`，「读—比—写」三段无锁，两个并发 PUT 带同一 base_hash 都会通过；`os.replace` 保证不写半截。
- **L17 `_require_session` 无 sid 白名单**：`server/app.py` 依赖 `store.path(sid).is_file()` 与 ValueError 兜底，无显式字符/长度校验（未构成已证实缺陷）。
- **L18 `comments[-10:]` 直接取键**：`github_bot/worker.py` 的 `run_fix_issue`，`c['user']['login']`/`c['body']` 在 GitHub ghost 用户（user 为 null）时触发 TypeError。
- **L19 缺凭据错误定位差**：`github_bot/worker.py`，`TOKEN`/`GITHUB_TOKEN` 均空时无前置校验，直到首次 `gh_json` 才以 RuntimeError 暴露。
- **L20 `_stage_whitelist` 路径解析不全**：`github_bot/worker.py`，`line[3:].split(" -> ")` 只处理 rename，未处理引号转义路径与非 ASCII 路径。
- **L21 `judge.py` 正则无法抠嵌套 JSON**：`evalkit/judge.py:18`，`\{[^{}]*\}` 遇到嵌套对象返回 None 或被判裁判失灵（已知取舍）。
- **L22 `is_relevant` 空串恒真**：`evalkit/ranking.py:13`，`expected` 含空字符串时 `exp in chunk` 恒为 True；test 只覆盖空列表。
- **L23 `__main__.py` provider 无白名单**：`__main__.py:52` 直接 `sys.argv[1]` 传给 `assemble`，未知 provider 以未捕获 traceback 退出（退出码 1）。

---

## 四、各模块覆盖情况（含「未见可证实问题」）

| 模块 | 已读 | 结论 |
|---|---|---|
| core | __init__/types/vector_math/audit/gateway/llm/telemetry/jev 全部 8 个源文件 | H1 H2 M1 M2 M3 L1 L2 L3；types.py、vector_math.py、__init__.py 未见问题；tests 未核对 |
| memory | plan/todos/store/compressor/title/learned/consolidate（后两者仅部分） | H8 M4 M5 M6 L4 L5 L6；learned.py 与 title.py 的完整通读未完成，不作「未发现问题」断言；test_plan.py / test_compressor.py 未读 |
| tools 第1部分 | files/sandbox/terminal/plan/spawn 共 5 文件 + memory/plan.py | H6 M7 M8 M9 M12 M13 M14 L7 L8 L9；`_read_file`/`_list_dir`/`_search_code` 主逻辑、白名单五道闸、`format_view`/`plan_scope_check`/`_FORBIDDEN` 等未见可证实问题 |
| tools 第2部分 | builtin/context/time/registry/todo/notes/history + mcp_client 前半 | M10 M11 L10 L11 L12；history.py（空检索与越界 start 显式返回）未见问题；unknown 工具分发与 todo「不存在」分支均正确；mcp_client 后半、mcp_config、mcp_http、web.py、graph.py、worktree.py 未读 |
| knowledge | 7 个实现文件全部读完 | H7 M15 M16 L13 L14；loader.py（空目录抛错已有测试）未见问题；extract.py、__init__.py 未核出可落代码事实的问题 |
| orchestrator | loop/executor/projection/agent/task/checkpoint 前 120 行 | M17 M18 L15；projection.py 与 agent.py 未见问题；run_turn 未触发列表身份陷阱（原地 append，无 rebind），轮次预算有终止路径，工具异常经 registry 兜底回灌 |
| server | app.py 全文、__main__、sse、run_store 全文 | H4 H5 M19 M20 M21 M22 M23 L16 L17；静态资源路径安全（硬编码 FileResponse + StaticFiles 挂载，无用户路径拼接）、__main__.py、sse.py 未见问题 |
| github_bot | worker.py 366 行、prompts.py、__init__.py 全部读完 | H3 M24 M25 M26 L18 L19 L20；子包内无 webhook 接收/验签/事件分发/HTTP 重试代码（入口是 GitHub Actions），相应检查项不适用；prompts.py、__init__.py 未见问题 |
| evalkit + 顶层 | paths/cli/__main__/review 全文 + evalkit 3 文件 | M27 M28 M29 M30 L21 L22 L23；review.py（纯文本无分支）、evalkit/__init__.py 未见问题 |

---

## 五、本轮巡检的局限（如实记录）

1. **未运行任何测试**：全部结论为静态代码阅读所得，未做动态复现。
2. **未完整覆盖的代码面**：memory 的 learned.py/title.py/consolidate.py 完整通读、tools 的 mcp_client 后半/mcp_config/mcp_http/web.py/graph.py/worktree.py、orchestrator/checkpoint.py 后半、`.github/workflows/agent.yml`（敏感路径策略拒绝读取）。
3. **未核对的测试**：test_plan.py（45KB）、test_compressor.py、test_learned.py、test_app.py 的 cancel/resume 分支用例、test_notes/test_history/test_mcp。
4. **待证实、未计入清单的疑点**：H9（`__main__.py` 的 latest/create/load 顺序）。
5. 首次知识库写入因「库中已有同名笔记」被拒，改为读取原文后并入本文件（原名 `robustness_patrol.md`）。
