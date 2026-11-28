# Facta 2.0 P1 Spec：并发账单与任务预算

> 状态=待评审 · 日期 2026-11-28 · 依赖：无（可独立开发；实时成本熔断以逐笔归因为前提）· 关联：[研发落地计划](m5-task-delivery-plan.md) · [外部评审](m5-external-review.md) · [ADR 039](decisions/039-s8a-session-model.md) · [ADR 108](decisions/108-cost-accounting-cross-check.md) · [ADR 115](decisions/115-m1-controllable-long-tasks.md) · Spec 3 审批恢复与任务推送（另文）

一句话：把「每笔模型调用花在哪个任务哪个 Run」逐笔记清，任务执行中就能读到含在飞估算的已记支出，并在下一次付费调用发起前用任务预算熔断——重叠 Run 不双记，超支在调用前拦住。

## 一、现状

### 已落地能力

- **累计尺与明细账分离。** UsageLedger 累计（record_llm / record_retry / record_cache_hit，「账单是参考值不是结算账目」，039/108 参考值口径）+ CallLedger 明细（CallEntry：call_id / operation / attempts / usage / outcome，attempts 是观测真源），verify_calls 交叉核验恒等式（总 attempt = calls + retries + failures，call_id 查重）（[UsageLedger](../src/facta/core/telemetry.py#L21)、[CallEntry](../src/facta/core/telemetry.py#L110)、[CallLedger](../src/facta/core/telemetry.py#L126)、[verify_calls](../src/facta/core/telemetry.py#L168)）。
- **网关记账横切出口。** RobustLLM 四件衣：成功 / 失败 / 重试 / 缓存命中各有记账，缓存命中不触达网络（[RobustLLM](../src/facta/core/gateway.py#L61)、[缓存命中](../src/facta/core/gateway.py#L110)）；FallbackLLM 降级链逐候选试、链耗尽抛 LLMUnavailableError（[FallbackLLM](../src/facta/core/gateway.py#L426)）。但明细账无 task/run 归因字段，重试/回退/缓存命中无逐笔标记。
- **任务分账（快照差）。** Run.usage = 两次 UsageLedger 快照差（usage_delta），收尾回填（[\_billing_closeout](../src/facta/server/app.py#L451)、[usage_delta](../src/facta/orchestrator/task.py#L130)）；并发窗口标 usage_approx（[run.usage_approx](../src/facta/server/run_store.py#L90)、[app.py](../src/facta/server/app.py#L469)）；Task.billing 聚合与合计推导（[billing_add](../src/facta/orchestrator/task.py#L139)、[billing_totals](../src/facta/orchestrator/task.py#L148)），GET `/api/tasks/{task_id}/billing` 供任务卡（[get_task_billing](../src/facta/server/app.py#L1897)）。
- **成本保险丝。** TASK_MAX_COST env（缺省不限）+ fuse_expired 成本判定 + 保险丝呈现（blocked + 原因）（[TASK_MAX_COST](../src/facta/orchestrator/task.py#L45)、[fuse_expired](../src/facta/orchestrator/task.py#L373)）。

### 已复现问题

外部评审已复现并定位（[外部评审](m5-external-review.md#p1-审批续传和预算)）：

1. **双记 0.04。** 模拟复现中两笔合计 `0.04` 的调用被两个重叠 Run 各记为 `0.04`——快照差按全局账本切时间窗口，窗口内别人的花费被整段吞进来；usage_approx 只标注不纠账。
2. **在飞花费读不到。** 进行中的 Run 要到收尾才入账（[\_billing_closeout](../src/facta/server/app.py#L451)）；成本熔断读任务账单时看不到在飞支出，任务中途的预算判定失真。

## 二、行为契约

### 2.1 逐笔归因

- 模型网关每笔计费调用必须落一条明细：task_id、run_id、供应商（实际应答的模型/候选名）、唯一调用 ID、缓存命中/重试/回退标记、用量与成本、成本分类。归因以调用发生时的执行上下文为准（task_id / run_id 随执行链传递，含子 Agent）。
- 记账口径：
  - **主模型与子 Agent**：各自的调用分别成笔，归因到发起它的 Run；子 Agent 调用记在所属任务的当前 Run 下（spawn 在同一执行链内）。
  - **重试**：每次下游尝试单独成笔（attempts 观测真源沿用），重试笔标 retry，归属同一逻辑调用。
  - **回退**：FallbackLLM 链上每个候选的尝试分别成笔，标 fallback；失败候选笔 outcome=error，成功候选笔记实际供应商与成本。
  - **缓存命中**：记 0 成本笔留痕（不触达网络但必须可查——「缓存省了多少钱」靠它说话）。
- 唯一性：唯一调用 ID 是核验锚（verify_calls 恒等式沿用）；同一笔调用不得落两条（重叠 Run 双记归零）。

### 2.2 成本口径（三类）

- **已确认**：供应商返回用量且有价目 → 确认成本。
- **估算**：用量/成本按参考值口径推算，沿用 usage_approx 标注（039/108 参考值口径——参考值不是结算账目）。
- **未返回**：usage=None 不猜测（「失败或无响应体时不猜测」纪律沿用），成本显式标未返回。
- 旧近似账单保持可读：字段加法不回改——历史 Task.billing / Run.usage 形状不变，分类标注是加法键。

### 2.3 实时预算

- Task 新增 `budget` 字段（缺省 None = 不限预算，与 TASK_MAX_COST 缺省一致；两者独立生效，先到者熔断）。
- 任务执行中必须可读取已记支出：Σ 已落笔 + 在飞未收尾调用的估算暂扣（见分歧三）。
- 判定点 = 下一次可控的付费调用发起前：已达上限 → 不发起该调用，任务熔断——保留已完成产物与剩余工作状态，落 blocked，原因标「预算耗尽（budget_exhausted）」，呈现沿用保险丝。缓存命中（0 成本）不触发预算拦截。
- 「可控的付费调用」= 经网关发起的计费调用（主模型、子 Agent、embedding）；预算判定与明细入账同一收口点，不得旁路。

### 2.4 兼容

- billing_add(task, run_id, usage, approx) 聚合账单保持可读（签名与落盘形状不变）；单 Run/聊天路径照旧（无归属调用 task_id=""，照常记账、不参与任务预算）。
- GET `/api/tasks/{task_id}/billing` 响应加法扩展（分类/明细键），旧消费方不受影响。

## 三、状态与数据变更

- **CallEntry 扩字段**：task_id、run_id、provider、flags（cache_hit / retry / fallback）、cost_class（confirmed / estimated / missing）、cost。加法；verify_calls 恒等式不变。
- **归因通道**：执行上下文携带（task_id, run_id）（contextvar 或显式传参，实施第 1 步定，不预膨胀）。
- **Task 字段**：`budget: float | None`（缺省 None）。落盘 asdict 自动跟上；to_dict 显式补键（[to_dict](../src/facta/orchestrator/task.py#L157)）。
- **预算判定缝**：挂在网关调用入口（付费调用发起前的必经收口），预算状态由任务侧注入读取（注入形状在实施第 4 步定，不预膨胀）。
- **保险丝呈现**：blocked_reason 增预算耗尽原因串，与续跑/时长/成本保险丝原因并列可辨。

## 四、兼容规则

- **存量任务**：无 budget 字段的 Task JSON 读取兼容（不限预算），不补造；billing 结构不回改。
- **旧账单**：billing_add / billing_totals 口径不变；逐笔归因是新账本维度，不重写历史记录。
- **单 Run/聊天路径照旧**：无 task 归属的调用不参与任务预算判定，其余记账行为不变。
- **保险丝并存**：env TASK_MAX_COST 与 Task.budget 独立生效，先到者熔断；沿用现有保险丝呈现通道。

## 五、失败处理

| 失败场景 | 表现 | 处置 |
|---|---|---|
| 供应商未返回用量（usage=None） | 明细标「未返回」，成本不虚构 | 不进已确认合计；标注供人工核对 |
| 归因窗口内并发（快照差失真） | 逐笔归因取代快照差作真值 | usage_approx 降为参考标注，口径不回改 |
| 在飞调用未收尾时读支出 | 按估算暂扣计入 | 判定保守偏严，收尾后回正 |
| 预算达上限 | 下一次付费调用前拦住；落 blocked + 预算耗尽原因，产物与剩余工作状态保留 | 沿用保险丝呈现；调高/移除 budget 后 resume |
| 并发 Run 同时逼近预算 | 各自调用前判定、暂扣互见 | 竞态一拍不保证严格不超支，量级 = 在飞暂扣（分歧三） |
| 调用已发出后才越线 | 该笔照常入账 | 不撕已发请求；后续付费调用拦住 |
| 明细账交叉核验不平（verify_calls 失败） | 如实报不平（漏记/重复可见） | 不静默吞；测试与排查按 call_id 锚定 |

## 六、测试矩阵

沿模拟账单与脚本模型惯例（与 [tests/test_task.py](../tests/test_task.py) 保险丝断言同源）；落点新增 tests/test_billing_budget.py。

| 编号 | 场景 | 通过条件 | 测试落点 |
|---|---|---|---|
| C1 | 逐笔归因唯一（重叠 Run 不双记） | 两重叠 Run 合计 0.04 的两笔调用各记一次，任务账单合计恰 0.04（外部评审双记 0.04 场景回归） | tests/test_billing_budget.py |
| C2 | 重试记账口径 | 一次逻辑调用 n 次尝试 = n 笔（retry 标记），归属同一任务，verify_calls 恒等式成立 | 同上 |
| C3 | 回退记账口径 | 切候选：失败候选笔 + 成功候选笔，供应商字段各记实际候选 | 同上 |
| C4 | 缓存命中留痕 | 0 成本笔落账、不触达网络，明细可查 | 同上 |
| C5 | 成本三分类 | 已确认/估算/未返回标注正确；未返回不虚构成本 | 同上 |
| C6 | 任务中途读取支出 | 执行中读到已记支出 + 在飞未收尾估算暂扣（不等收尾） | 同上 |
| C7 | 预算熔断 | 达上限后下一次付费调用被拦，落 blocked/budget_exhausted，已完成产物与剩余工作状态保留 | 同上 |
| C8 | 旧账单兼容 | billing_add / 合计 / 存量 Task JSON 读取正常，/billing 对旧消费方可读 | 同上 |

## 七、实施顺序

1. **归因通道与明细扩展**（独立可先行）。CallEntry 增字段 + 执行上下文携带（task_id, run_id）。涉及 [telemetry.py](../src/facta/core/telemetry.py)、[gateway.py](../src/facta/core/gateway.py)、[app.py](../src/facta/server/app.py)（worker 设置上下文）。测试：C1 骨架。
2. **记账口径**（依赖 1）。重试单独成笔、回退逐候选成笔、缓存命中 0 成本留痕、三类成本标注。涉及 [gateway.py](../src/facta/core/gateway.py)、[telemetry.py](../src/facta/core/telemetry.py)。测试：C2-C5。
3. **任务支出读取**（依赖 1、2）。逐笔聚合为真值（快照差降参考标注），含在飞未收尾估算暂扣。涉及 [task.py](../src/facta/orchestrator/task.py)、[app.py](../src/facta/server/app.py)。测试：C6。
4. **预算判定与熔断**（依赖 3）。Task.budget + 调用前判定 + blocked/budget_exhausted + 保险丝呈现。涉及 [task.py](../src/facta/orchestrator/task.py)、[gateway.py](../src/facta/core/gateway.py)、[app.py](../src/facta/server/app.py)。测试：C7。
5. **兼容收口**（依赖 1-4）。billing_add/旧账单可读、/billing 加法、全矩阵过绿。测试：C8、C1 收口。

## 八、设计分歧与推荐

1. **明细账本 append-only jsonl vs 复用现有 telemetry 结构** → 推荐在现有 CallLedger 上加字段，不另造存储。CallLedger 本就是逐笔明细 + call_id 唯一锚，verify_calls 核验通道现成；另造 jsonl 会出第二真值源，两本账的对账本身就是新工作量。持久化诉求若出现（重启后回查账单），再按触发信号升级。
2. **预算判定点 = 每次付费调用前 vs 轮次边界** → 推荐调用前。实时熔断才成立：轮次边界判定下，一轮内的 N 次调用会先花后拦，「达上限熔断」退化成事后记账；调用前判定恰好落在网关必经收口点，成本一次判定。
3. **在飞未收尾调用的预算占用 = 按估算暂扣 vs 不计** → 推荐估算暂扣。不计会在「两笔在飞、预算只剩一笔的钱」时把两笔都放行，超支漏判正是本 spec 要关的口子；暂扣偏严可接受（收尾回正），符合 039/108「参考值宁可保守」的口径。
4. **双记修复 = 逐笔归因取代快照差 vs 修补快照差窗口** → 推荐逐笔归因。快照差在并发窗口下原理上分不清「自己的」与「别人的」调用（usage_approx 就是这个妥协的标注），修窗口 = 继续近似；逐笔归因让账目可核验，快照差降级为参考标注（字段加法不回改）。
