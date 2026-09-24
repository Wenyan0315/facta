# 决策记录 · Run checkpoint 与崩溃恢复（2026-09-24）

> 竞品对标 P0-3 立项：借 LangGraph 的 checkpoint/resume 语义给执行主轴加持久化，不引入图编排框架。返回 [architecture.md](../architecture.md)

- **来源与依据**：长任务（>5 分钟或 >20 次工具调用）进程崩溃只能从头重跑，是执行主轴当前最大的可靠性缺口。LangGraph 证明的正确语义：checkpoint 按执行标识持久化、interrupt/resume 支撑人工闸门；其公开教训同样重要——resume 时节点从头重跑，**副作用不幂等 = 重复执行**（重复发通知类事故）。Temporal 对 LangGraph 的批评（checkpoint 只保数据不保执行、恢复要靠自己编排）提示：语义必须自建且最小化。cortex 现状：loop 纯内存态，worktree（030）只解决文件隔离不解决状态恢复。本条为红队版「烂尾也值」最小集之二（另一条见 037）。

- **四拍板**：
  - **P1 checkpoint 写入点**：tool / plan 事件落库后，向 Run Store（外置 JSONL，与 032「JSONL canonical、对人可 diff」同哲学）追加一条 checkpoint；内容 = 重建上下文的最小状态（当前 plan、消息史指针、已执行工具调用摘要、副作用清单），不做全量内存快照。
  - **P2 resume 语义**：从最近 checkpoint 重建会话继续执行；**副作用型工具（写文件 / 终端 / 网络）注册时必须标注幂等性**，重放按标注去重——已成功的非幂等调用不重复执行，改为回注其原始结果。
  - **P3 不引入图编排框架**：自有 loop 架构不变，只借 checkpoint/resume 语义（对标文档「明确不学清单」第 4 条）；不追 LangGraph 的任意中点恢复，粒度只到工具事件边界。
  - **P4 覆盖范围限主 loop**：子 agent（spawn）的 checkpoint 归属、与 worktree 生命周期的交互本轮不定——挂触发信号：「子 agent 长任务崩溃实踩」或 S8 常驻进程开工，届时单独立项。

- **反方意见**：
  1. **自建语义无框架兜底**：checkpoint 正确性全靠自己——应对：语义最小化（只到工具事件粒度），evalkit 新增 kill-resume 场景集守正确性，语义变更必须过该场景集。
  2. **I/O 开销**：每事件追加写有成本——应对：异步写 + 事件粒度（非每 token），实测开销超阈值再优化，不预先工程化。
  3. **checkpoint 与记忆层边界模糊**：Run Store（执行状态）与 learned 三桶（固化知识）必须保持分离——应对：Run Store 只存执行态、不进固化管线；回顾价值由退出复盘（032 管线）萃取，不由 checkpoint 直接入记忆。

- **触发信号（未达即不扩编）**：主 loop kill-resume 稳定后，若实际使用中「恢复后上下文错乱/重复副作用」出现率不为零，冻结扩编（子 agent 覆盖等），先修语义。

- **验收**：evalkit「kill 进程 → 从 checkpoint 恢复 → 任务完成且副作用不重复」场景通过；checkpoint 文件人工可读（JSONL）；pytest / ruff / mypy 三道门全绿；实机轮真模型长任务 kill 一次验证。
