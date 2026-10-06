# ADR 084：只读工具并行批——`is_readonly` 驱动可并行判定

- 状态：**已批准**
- 日期：2026-10-06
- 立项出处：[competitive-roadmap P1-7](../internal/competitive-roadmap.md)（v0.5 编排补遗）
- 关联：082（③ 把切批逻辑从 loop.py 拆进 executor.py 的同批补遗）、S6b（并行 spawn 的原始出处，`_SPAWN_TOOL` 常量与 `_split_tool_batches` 均在此引入）、S3（`Tool.is_readonly` 字段的原始出处）

## 背景

roadmap P1-7 原文：

> 编排层代码评审。现保守默认「普通工具串行——模型常期待先读 A 再决定读 B」
> 对**同一轮并列点菜**不成立：模型一轮里同时点了 read A 和 read B，说明决定
> 已做完，无顺序依赖。改动：`_split_tool_batches` 增加只读名单（`read_file` /
> `search_code` / `list_dir` 等）：连续只读段与连续 spawn 段一样走线程池并行；
> 写类工具保持串行；结果仍按点菜顺序回填。

病灶：`_split_tool_batches` 只对 `_SPAWN_TOOL`（`spawn_subagent`）开并行，
其余工具一律单元素串行批。模型「同一轮点 read A + read B + read C」这类
「决定已做完、无顺序依赖」的并列只读点菜，被保守默认串行化——多读取场景
的延迟是累加的。

## 拍板

可并行判定从「名字 == `_SPAWN_TOOL`」扩展为「名字 == `_SPAWN_TOOL` 或
`registry.get(name).is_readonly`」。切批逻辑不变：连续可并行段并成一批
（True），其余逐个串行批（False）。`_split_tool_batches` 新增 `registry`
参数以获知工具只读状态。

- **用既有 `Tool.is_readonly` 字段驱动，不硬编码只读名单**——单一真值源：
  S3 权限分级已在每个工具上声明 `is_readonly`（只读 L0 / 写 L1），硬编码
  `{"read_file", "search_code", ...}` 名单是与 registry 第二份真值，将来加
  只读工具会漏改两处。查 `registry.get(name)` 后读字段，天然跟随注册表。
- **`registry.get(name)` 返回 None（未注册/未知工具）→ 不并行**——保守方向，
  与「没声明 `is_readonly` 一律按写类」同哲学。`spawn_subagent` 是唯一
  名字特判（它 `is_readonly=False` 但已证明可并行，独立 Session + worktree
  隔离），其余全靠字段。
- **结果仍按点菜顺序回填**：`_run_parallel` 用 `f.result()` 按 futures 提交序
  取，非完成序——并行只消掉墙钟时间，不改模型看到的顺序（逐字节等价）。

**负决策**：

- 不硬编码只读工具名单——见上，与 registry 第二份真值，省码且不腐烂。
- 不引入新的 `is_parallel` 独立字段——`is_readonly` 就是可并行判定的充分
  条件（只读工具无副作用、无顺序依赖），再造字段是重复表达。
- 不改 `_run_parallel` 的线程池策略——只读工具多为 IO-bound（读文件、查
  chroma、HTTP 检索），GIL 不碍事，线程池够用，不必上进程。

## 正面（本项落地的位置）

- **`src/facta/orchestrator/executor.py`**：`_split_tool_batches` 签名加
  `registry: ToolRegistry`，可并行判定收敛到一个 `_is_parallel(name, registry)`
  内联判断；`_execute_tool_calls` 调用点透传 `agent.registry`。
- **`tests/test_parallel_spawn.py`**：`test_split_tool_batches` 更新断言
  （read_file 不再插断并行段），补只读并列 + 只读/写类混合的切批用例。

## 边界（诚实登记）

- **chroma 并行只读查询的线程安全未证明**：`search_notes` / `search_and_summarize`
  / `query_graph` 底层用 `ChromaVectorStore`（chromadb PersistentClient）或
  `InMemoryVectorStore`（纯 Python 字典）。只读 `query` 是否线程安全是
  chromadb 的实现细节，本项不做进程隔离。**触发信号**：多读取并行场景出现
  chroma 查询抛异常 / 结果错乱时，收敛为「chroma 类工具回到串行批」（名单
  特判，负决策保留的一处例外）。
- **CPU-bound 只读工具并行不加速**：`query_graph` 的 InMemory 余弦是 CPU 密集，
  线程池并行会因 GIL 退化为串行（不加速但不更慢、也不破坏正确性）。本项
  目标是 IO-bound 读取（文件/网络/chroma 磁盘），不承诺 CPU 密集场景的加速。
- **写类工具保持串行不变**：`write_file` / `run_command` 等仍单元素串行批，
  本项对写类的行为零改动。

## 验收

- `test_split_tool_batches`：`[spawn, spawn, read_file, spawn]` 现切为
  `[(True, 4)]`（read_file 只读，并入同一可并行段）；新增只读并列
  `[read_file, search_code]` → `[(True, 2)]`、只读/写类混合
  `[read_file, write_file, list_dir]` → `[(True,1), (False,1), (True,1)]`。
- 现有 spawn / 切批测试全绿；三门通过（ruff / mypy / pytest）。
