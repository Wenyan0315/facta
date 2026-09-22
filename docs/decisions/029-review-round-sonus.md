# 决策记录 · 评审修复轮（sonus.md，2026-09-22）

> 外部专家评审（docs/reviewq/sonus.md，442 行，73 项测试+探针验证），按 020 修复轮模式消化。返回 [architecture.md](../architecture.md)

- **评审方法论**：外部评审跑了 73 项测试+探针验证，报告 442 行含三角色视角（研发/产品/UED）+项目定位与四视角建设建议。本轮按「先坐实再修」纪律：8 项硬伤逐条核实代码路径，7 项当场坐实即修，1 项（R8 语义缓存）属 M10 交互放大而非独立硬伤——同修。每项修复配回归测试防退化。

- **R1 工具读边界不对称**：`read_notes` 裸拼接 `ctx.notes_dir / filename`，`../../.env` 可越界读密钥——写有三重防线（resolve+is_relative_to+.md 后缀）、读裸奔的「防御不对称」被坐实。同修 `search_code`：rglob 直接绕过 `_resolve_in_workspace` 的 .env 文件级黑名单，命中行会把密钥内容吐给模型——rglob 循环里加 `_BLACKLIST_PARTS` 检查（与 read_file 同一清单，不 import 那个函数：解析语义不同）。

- **R2 XSS（marked.parse → innerHTML）**：模型可能复述外部网页/用户输入的恶意 HTML，`marked.parse(text)` 直接进 `innerHTML`。修复：escape-before-parse（先 escapeHtml 再 parse，marked 负责结构、不负责清洗）+ 渲染后 `<a>` 协议白名单（`sanitizeLinks`：只放行 http/https/mailto/#，其余 removeAttribute）。

- **R3 事件名契约断裂**：服务端 `_EVENT_MAP` 映射为点分（`tool.started`/`tool.result`），前端 RunDetail 订阅下划线版（`tool_started`/`tool_result`）——tool 事件全收不到。修复：前端 TYPES 改点分版；契约文档化在 `_EVENT_MAP` 注释。

- **R4 plan 生命周期断裂**：`_archive_current` 清空段漏清 `session.plan`——旧任务的活跃计划泄进新会话投影（CLI `/new` 同修）；`_switch_session` 换血段漏恢复 `session.plan`——归档文件里存着（S5b 序列化），恢复时被丢弃。修复：清空段加 `session.plan = PlanBoard()`；换血段加 `session.plan = restored.plan`。

- **R5 spawn 默认子集泄漏父历史**：`_FORBIDDEN` 漏了 `search_history`/`read_history`——闭包绑父会话 messages，噪声隔离反向泄漏。修复：`_FORBIDDEN` 加历史两件（与 spawn_subagent/make_plan/update_plan_step/finish_plan 同列）。显式指定子集时也强制剔除（程序侧，不信任模型自觉——已在 S5c 硬约束）。

- **R6 SSE 单队列竞争消费 → 广播模型**：单 Queue 时代，聊天页+任务页同时订阅同一 Run 时事件被随机分食（竞争消费——谁先 get 谁拿走）。修复：`_queue` → `_subscribers: list`；`emit` 遍历广播；`subscribe` 新建独立队列+终态补发哨兵（晚到的订阅者不错过收口）；新增 `unsubscribe`（SSE 连接断开时退订，防队列引用滞留）。**配套修复**：旧 test_run_store.py 假定单队列语义（emit 后才 subscribe，队列已有历史事件）→ 广播模型下 `subscribe()` 只收订阅后事件 → `get()` 永久阻塞（全套 pytest 挂起的根因）。测试修正为先 subscribe 再 emit。

- **R7 归档覆盖手工重命名**：`_archive_current` 无条件调 `summarize_title` 生成标题——用户 rename 过的名称被 LLM 重新提炼顶掉（「自动生成用于填空，不覆盖用户主动编辑」——计划名/记忆标签同此原则）。修复：`if ctx.session.title is None:` 才生成。

- **R8 语义缓存跨上下文串味**：语义缓存只比最后一条 user 消息，忽略历史/system/计划——「继续」在不同任务里含义完全不同，外部评审探针实证了跨上下文串味；M10 direct 路由把纯聊天送进 tools=None 命中区后风险被进一步放大。修复：主链默认关闭，`CORTEX_SEMANTIC_CACHE` 环境变量显式开启（无状态 FAQ 场景）。精确缓存（完整输入哈希）不受影响仍在 RobustLLM 内生效。

- **四视角吸收**：外部评审提出四视角建设建议——①机制可观察（已落地：事件流 SSE + Run Store + 审计日志，吸收为既有方向的注记）②任务评测并入 evals 方向（已落地：evalkit + model-bench，不新开工）③运维视角进「企业级考量」（个人单机定位暂不需要，触发信号=多用户/团队版）④预算视角（已落地：UsageLedger 全进程记账，028 的 deepseek-flash 选型含成本考量）。四视角不新开工——已有对应设施覆盖，评审的价值在确认方向而非发现缺口。

- 验收：ruff ✓ mypy ✓ pytest 376 passed +2 skipped（test_review_round +9 用例：R1 越界读/search_code 黑名单、R3 事件名契约、R4 plan 重置/换血恢复、R5 禁止单验证、R6 广播模型+退订+终态哨兵、R7 手工名保留；test_run_store 广播模型修正 2 用例）。
