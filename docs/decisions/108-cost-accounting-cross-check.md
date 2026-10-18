# ADR 108：成本记账交叉核验——明细账 + call_id 唯一归属 + 独立观测真源

- 状态：已裁定（2026-10-18）
- 日期：2026-10-18
- 上游：[007](007-m7.5-gateway.md)（网关记账四衣）、[046](046-frozen-real-task-eval.md)
  （评测纪律）、[094](094-backlog-readme-frontend-ci.md)（回溯与度量基线）；
  外部参考 delta-agent 项目（本地
  `~/Documents/trae_projects/AgentA/delta-agent`）的成本验收整改 A01/A02
  与 `server/tests/cost-accounting.test.ts` C-5（非本仓文件，作为方法参照）。

## 背景

[telemetry.py](../../src/facta/core/telemetry.py) 的 `UsageLedger` 是进程级累计尺，
字段只增不减；[gateway.py](../../src/facta/core/gateway.py) 的 `RobustLLM` 在成功
路径 `record_llm(usage, cost)`、失败路径 `record_llm_failure()`。这套账单自己声明
"是事后归因的度量尺，后续缓存/熔断的收益全凭它说话"，但这把尺正确性从未被
交叉核验过，存在三条真实盲区：

1. **单通道自证**：token/钱的唯一来源是 `reply.usage`（`OpenAICompatibleLLM`
   从 `resp.usage` 摘，见 llm.py `#320-335`）。极少数中转商不返回 `usage` 时，
   `record_llm(None, ...)` 只加 `llm_calls`，token 静默漏记，账单上无任何痕迹。
2. **失败/重试无观测**：`record_llm_failure()` 只加一次计数，不知道"重试到底
   发了几次下游请求"；`record_retry()` 与 `record_llm_failure()` 是两个独立计数，
   无法回答"这 N 次失败是不是真的都撞到了模型"；`llm_cost` 完全不含失败请求
   的成本（失败时 usage=None、cost=0）。
3. **缓存命中无对照账**：`record_cache_hit()` 只加次数，账单里没有"这一下省了
   多少钱"的对照账——"缓存省了多少钱"这句度量承诺目前无法从账本兑现。

delta-agent 的可移植精髓（A01/A02）：**计费真源是"观测到的调用记录"，不是同一段
代码累加出来的两个数字**；观测通道记录"已经发生的下游请求"，与产物/usage 是否
返回解耦——请求发生了就一定被计入，未入账会被 `unaccounted_call_ids` 暴露。

## 方案（范围）

### 做的（最小交叉核验，全部纯后端）

1. **独立明细账 `CallLedger`**（新对象，与 `UsageLedger` 累计尺职责分离）：
   每次逻辑模型调用（成功/失败/重试耗尽都算，缓存命中不算）记一笔——
   `call_id`（进程内唯一）、`operation`（`agent_loop`/`compressor`/`tool:*` 等）、
   `attempt_count`（真实下游尝试次数）、`usage`（可能 `None`）、`outcome`（`ok`/`error`）。
2. **独立观测真源 = 下游尝试次数，不取自 usage**：`attempt_count` 来自 `RobustLLM`
   重试循环中 `_inner.generate` 每被调用一次的控制流事实，与 `reply.usage` 是否
   非空无关——即使 `resp.usage=None`，请求尝试次数仍然准确记 1。
3. **核验函数 `verify_calls()`**：交叉比对三类不变量——
   - 内部一致性 `ledger_matches_total`：明细账合计 == 累计尺（同源，只能查出账本内部漏写）；
   - 外部一致性 `matches_observation`：独立观测到的下游尝试次数 == 记账请求次数；
   - 唯一归属 `unaccounted`（观测有、账本无 = 漏记）/ `unobserved`（账本有、观测无 = 虚构）/
     `duplicate`（同一 `call_id` 重复入账 = 双计）。
4. **测试：移植 C-5 篡改注入**，起本地 HTTP 替身（`http.server`，env 覆盖
   `{PREFIX}_BASE_URL`）作外部真源，注入三处篡改（双计 / 虚构 ghost / 漏记并同步
   扣减总量），每处断言 `ledger_matches_total == True` 但 `ok == False`——证明核验
   不是两个同源数字互相比。
5. **失败/重试路径入账**：HTTP 500 / 断连重试成功后，断言"已发生的请求都入账、
   无响应体时 token 记 `None`（不猜测）、不拆账不双计"。

### 不做的（边界）

- **不改成结算级账 / 进程外聚合**：沿用 telemetry.py 既定裁定"账单是参考值不是
  结算账目"，并发近似（不加锁）不动——本次核验在串行记账下运行，不引入锁。
- **不动熔断/缓存/重试/降级的任何语义**：只加明细记录与核验读口，网关四衣的
  行为零变化。
- **不引入"请求字节"观测维度**：delta 用字节作成本 proxy 是因为它没有 token 价目；
  facta 有真实 token 价目，字节观测无度量意义，砍掉。
- **不引入任务/修订两级账本**：facta 无"任务/修订"概念，单层明细账足够。
- 不改 `bill()` 输出格式（可选加一行核验摘要，列为待定项）。

## 定案

- **T1 观测真源落点 = 档 A**：`attempt_count` 取 `RobustLLM` 重试循环控制流
  （`_inner.generate` 每被调用一次），与 openai SDK 语义无关。实施时 `CallLedger`
  注入需经 `llm.py` 的 `get_llm` 工厂透传一个参数——这是注入通道，不是观测真源
  下沉；观测真源仍在 `RobustLLM` 循环，**不碰 `OpenAICompatibleLLM` 的 `create()` 点**。
  用户拍板。
- **T2 核验时机 = 仅测试 + 退出账单**：`verify_calls()` 独立提供，测试里断言；
  `bill()` 退出时顺带核验，异常告警。热路径零开销。用户拍板。
- **T3 明细账对象形态**：独立 `CallLedger`（实现阶段定；倾向独立对象，与累计尺
  职责分离，避免 `UsageLedger` dataclass 加字段污染 `==`/repr）。

## 验收

- [x] 三门全绿：ruff 0 error、mypy 64 files clean、pytest **908 passed, 7 skipped**
      （基线 903 + 5 新测试）。
- [x] 双计可查：同一 `call_id` 重复入账，内部账本自洽时仍 `ok == False`、
      `duplicates` 命中（`test_verify_calls_detects_duplicate`）。
- [x] 漏记可查：明细 attempt 合计 < 累计尺推算 → `ledger_matches_total == False`、
      `ok == False`（`test_verify_calls_detects_undercount`）。
- [x] 观测真源独立于 usage：明细 attempt == spy 模型自计数（第二真源）
      `flaky.attempts`，非同源自比（`test_attempts_reflects_actual_inner_calls`）。
- [x] 失败/重试路径：attempt 照实入账、usage=None 不猜测、恒等式成立
      （`test_failure_attempts_recorded_usage_none`）。
- [x] 现有 `test_telemetry.py` 原 13 条断言不变（`UsageLedger` 累计尺外形零破坏）；
      缓存命中仍不计入明细账（既有语义保持，未加对照账——列为后续候选）。

## 验证结果

- 三门全绿（见上）。新增 `test_telemetry.py` 5 条测试，全离线假模型（同文件既有
  风格）——档 A 观测真源在 `RobustLLM` 控制流，`spy` 模型自计数即可作「独立第二真源」，
  故未引真 HTTP 替身；HTTP 替身留待档 B（观测下沉到 `create()` 点）再引，否则会撞
  openai SDK 内部 retry 语义（档 A 明确不碰）。
- 退出账单核验已落 [__main__.py](../../src/facta/__main__.py)：`verify_calls` 未过
  仅打印告警、不拦截退出，CLI 零配置体验不变。