# 决策记录 · S6b 并行 spawn（2026-09-22）

> S6 第二站：一轮多 spawn 并行执行。返回 [architecture.md](../architecture.md)

- **四拍板（开工设计草案定）**：
  - **P1 只并行 spawn，不并行普通工具**：并行粒度=「连续 spawn_subagent 段」。唯一可证明安全的并行单元（独立 Session + worktree 隔离 + IO-bound）；普通工具（read/write/run_command）保持串行——模型常期待「先读 A 再决定读 B」，并行会打乱预期顺序。openai tool_calls 协议虽允许并行，但语义上多数模型假设串行——保守默认与 needs_confirmation 同哲学。通用「parallel-safe 标记」机制不做（YAGNI，等第二个真实用例）。
  - **P2 确认缝串行化**：`Run.request_confirm` 加 `_confirm_lock`。并行 spawn 的多个确认（子 agent 内高危工具 + worktree merge_worktree）会并发踩单槽位（`confirm_pending`/`_confirm_decision` 全局一份）——第二个 clear event 会冲掉第一个的裁决通道。锁串行化裁决：同一时刻只处理一个确认，其余排队——语义正确（人一次只能看一个确认弹窗），确认本就该串行。
  - **P3 事件顺序 = 点菜顺序**：tool_started 全发（表示都开始了）→ 并行跑 → tool_result 按点菜顺序回填。结果顺序由 `f.result()` 按 futures 提交序取（非完成序）保证——模型靠位置对应 tool_call_id，确定性优先于「最快完成」。
  - **P4 prompt 中性引导**：spawn description 加一句「有多件互不依赖的杂活可一次调用多个，会并行执行」——准确的能力说明，非过度暗示。

- **实现**：`_split_tool_batches`（切批纯函数：连续 spawn = 并行批 True，其余逐个 = 串行批 False；单 spawn 标记 True 但 len==1 走串行路径）/ `_run_parallel`（ThreadPoolExecutor，IO-bound 无需进程）/ `_execute_tool_calls`（抽函数：切批+执行+按序回填，顺带解决 run_turn PLR0912 分支超标）。LLM 并发安全判定：RobustLLM 无全局锁、HTTP 调用并发有效；精确缓存 OrderedDict 并发写是低概率风险（子会话输入不同几乎不命中），挂触发信号，不阻塞开工。

- **开发实踩（教训入档）**：**`merge_stream_chunks` 按 `index` 归并 tool_calls，测试 helper 的 `_call` 没带 index**——同轮多 spawn 被归到 idx=0，arguments 拼接成 `{"task":"甲"}{"task":"乙"}` 触发 JSON 解析失败。真模型（OpenAI 协议）的流式 tool_calls 带 index，是测试 helper 的缺陷；此前所有测试都是单 tool_call 或分轮，从未触发。S6b 首次构造「同轮多 tool_call」场景才暴露。修法：测试 `_call` 加 index + id 带 idx 保证唯一。

- **验收**：pytest 394 passed（+6：切批纯函数 2 + 并行耗时+按序回填 + 单 spawn 回归 + 混合工具串行 + 确认缝无串扰）；**冒烟回归 4 条全过（S6 安全网第二次兑现——并行是「怎么跑」变了，「模型看到什么」逐字节一致）**；ruff/mypy/CI 三道门全绿。

- **S6c 留档**：真编排（plan 步骤驱动 spawn）——并行 spawn 已就位，下一步是「计划步骤驱动多个 spawn 并行」的编排层入口。