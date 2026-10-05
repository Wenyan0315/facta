# ADR 081：Headless JSON 输出——同步提交任务 → 拿结构化结果

- 状态：**已批准**
- 日期：2026-10-05
- 立项出处：[competitive-roadmap P2-5](../internal/competitive-roadmap.md)（Headless JSON/RPC 模式）
- 关联：076（`_settle_done` 收官同步点——本项是它第一个「wait 的消费者」）、S2b（Run Store 状态机 + 事件序列）、S4b（L2 确认缝——headless 无批准人的边界）

## 背景

roadmap P2-5 原文：

> 改动：`server/app.py` 补机器可读输出模式，供外部自动化（含 Kimi Work
> 的定时任务）驱动。
> 验收：外部脚本可非交互提交任务并拿到结构化结果。

现状病灶（读 `server/app.py` + `server/run_store.py` 核对）：

- **SSE 是唯一取结果的途径**：`POST /api/runs` 返回 202 后，客户端必须挂
  上 `GET /api/runs/{id}/events` 的 EventSource 流，一路消费事件到
  `run.completed` 才拿得到回复。脚本自动化（定时任务、CI、外部编排）不想
  也不该维护一条长连接——它要的是「提交 → 等 → 拿文本」一条往返。
- **完整回复没被保留**：`_run_worker` 只把最终回复截断进
  `run.preview = reply.content[:300]`（任务视图的交付摘要）。headless 要
  的是完整结构化结果，300 字截断不够。

## 选项

- **A. 新增同步 JSON 端点** `POST /api/runs/sync`：复用现有 `_run_worker`
  + `create_if_idle`，请求线程 `run._settle_done.wait()` 阻塞到终态，返回
  `{run_id, session_id, status, text}`（`text`＝完整回复，失败/取消为空）。
- **B. 完整 RPC 协议**（长连接、工具调用往返、双向事件流）：OpenHands 式
  的 headless REST 全量形态。
- **C. 复用 `POST /api/runs` 加 `?wait=true`**：一个端点两种语义。

## 拍板

**A。B 不做（无消费者）；C 不做（语义污染）。**

### 1. 同步 JSON 是「外部自动化」的最小可用形态

Kimi Work 定时任务、CI 这类消费者要的交互就是「发一条请求，拿一个 JSON」。
它们不关心中间事件、不关心流式增量，要的是**终态的文本与状态**。一条
HTTP 往返能完成的事，不需要让对方维护 EventSource 长连接 + 事件解析。
这是 Pi 四模式里「JSON 模式」的等价物，是「机器可读输出」的最实一步。

### 2. 复用 `_settle_done`：零新机制

`_settle_done`（ADR 076）已经是「worker 收官完成」的同步点——注释原话
「生产只 set 不 wait」。headless 端点就是第一个需要 wait 的生产消费者：
`wait()` 返回时，Run 必已推进终态且 settle 已收尾，`run.status` /
`run.reply_text` 都已就绪。没有引入任何新的线程原语或状态。

### 3. C 不做：一个端点一种语义

`POST /api/runs` 的 202 契约是「异步创建、立即返回」——前端靠它秒回
run_id 再挂 SSE。塞一个 `?wait=true` 会让同一路由既可能 202 秒回、又可能
阻塞几十秒才 200，前端与脚本都难预测行为，且污染既有前端调用方。独立
`/sync` 端点把「同步等结果」与「异步订阅」切成两条正交契约，各自清晰。

### 4. B 不做：完整 RPC 没有真实消费者

工具调用往返、双向事件流是「远程控制台」的形态——需要一个同时想**边跑边看
+ 边干预**的消费者。现在没有。按 ponytail「等真消费者」：先交付同步 JSON
这条 95% 自动化需求都在的路径，RPC 形态等消费者出现再定协议。

## 负决策（明确不做）

- **不做完整 RPC 协议**（长连接、工具往返、双向事件流）：无消费者，协议
  形态未知，抢先设计只会猜错。
- **不做 `?wait=true` 复用**：保持 `POST /api/runs` 的 202 异步语义单一。
- **不自动裁决 L2 确认**：headless 没有「批准人」，自动放行危险终端命令是
  安全红线；确认挂起时同步端点如实阻塞，这是诚实边界而非缺陷。
- **不设超时参数**：超时语义依赖消费者对任务时长的假设，现在定不下来，
  等真消费者带真实时长诉求再设。

## 正面（本项落地的两处）

- **`server/run_store.py`**：`Run` 加 `reply_text: str = ""` 字段（完整回复
  文本），`preview` 仍留 300 字截断（任务视图摘要，契约不变）。
- **`server/app.py`**：`_run_worker` 在 `RunResult.COMPLETED` 分支回填
  `run.reply_text = reply.content or ""`；新增 `POST /api/runs/sync` 端点，
  创建 run + 启动 daemon worker + `run._settle_done.wait()` + 返回
  `{run_id, session_id, status, text}`。

## 边界（诚实登记）

- **同步端点阻塞请求线程**：FastAPI 同步端点跑在默认线程池，阻塞不堵
  事件循环；但会占用一个线程池槽位——headless 任务长跑时，线程池耗尽由
  uvicorn 的并发配置兜（不在本项范围）。
- **L2 确认挂起会阻塞同步端点**：headless 无批准人，`request_confirm`
  会等到取消才回。headless 任务应避免触发 L2（终端危险命令走白名单免确认
  的只读/安全路径）。
- **结果只含最终文本，不含中间事件**：需要流式增量走现有 SSE，两套协议
  并存、不互相替代。

## 验收

- 外部脚本可单请求非交互提交任务并拿到 `{run_id, session_id, status, text}`。
- `text` 为完整回复（不受 `preview` 300 字截断影响）。
- 三门通过（ruff / mypy / pytest）。

## 触发信号

- **真实 RPC 消费者出现**（需要工具往返 / 边跑边干预 / 流式事件推送）：
  届时再升级为 B 形态，协议以消费者为准。
- **headless 任务频繁卡在 L2 确认**：评估「headless 会话禁用 L2 触发工具」
  或显式「headless 遇 L2 直接失败并告知」的策略。
- **线程池槽位成为瓶颈**：同步端点长跑占满 FastAPI 线程池 → 评估独立
  端点走 `run_in_executor` 或独立 worker 池。
