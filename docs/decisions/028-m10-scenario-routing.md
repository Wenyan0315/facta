# 决策记录 · M10 场景路由与快慢分工（2026-09-21）

> model-bench 双轨模型评测（2026-09-20，git 外独立目录）结论落地：单一主力模型 → 场景分工（决策模型管路由 + 快模型管生成）。返回 [architecture.md](../architecture.md)

## 评测证据摘要（适用域=原子决策场景，非对模型整体判语）

- 参测：Jev（TypeSafe 决策模型）/ deepseek-v4-pro / deepseek-flash / glm-5.3 / glm-5.3-flash
- **题库轨道**（30题×3判断）：DS-Flash 综合 86% 第一，校准 ECE 0.042 最佳，p50 741ms，¥0.02/90判断 → 选为生成主力
- **工具调用轨道**（20题）：Jev choice 原生路由 19/20 全场第一，成本约为主力 LLM 1/20 → 意图识别外包；无参数通道、score 评级仅 53% → 参数填充归 LLM，评分场景不用 Jev
- **安全发现**：DS-V4-Pro 全场唯一裸奔（rm -rf data 直接执行）；五模型安全确认无一全对 → **确认闸门必须 harness 程序侧硬编码**——现有 S4b 白名单机制不动，bench 为其正名
- **三层分解**（核心架构结论）：工具调用 = 意图识别（可外包决策模型）+ 槽位填充（LLM 主场）+ 循环决策（harness 程序侧）
- GLM-5.3 决策场景方差大（两轮 60%→80%、思考模式吃光 token 空输出可复现）→ 决策场景不推荐（未测其推理主场，不下整体判语）

## 核心决策

1. **场景分类 v1 三类**：`direct`（纯生成，tools=None）/ `single_tool(name)`（Jev 选工具、LLM 填参数）/ `complex`（全量菜单，模型自决——S5b make_plan 承接）。Jev 一段式 choice（选项=工具名∪{direct, complex}），payload 与 bench run_jev 协议同构（已验证）。noul 确认信号 v1 不上（触发信号：实测出现白名单漏放案例）。

2. **挂载层：编排语义、core 落位、轮首一针**。router 挂 Agent 对象（S5a 行为定义收口处）；模块住 `core/jev.py`（core 不 import 上层——工具名作为参数传入）；run_turn 只加 `_route_first_menu` 一针（S5b `_plan_stamp` 同款手法）。**不进 gateway**：场景路由是编排语义（该不该调模型/调谁），gateway 是可靠性语义（怎么调），分层不许糊。

3. **半路由**：路由只影响本轮第一次模型调用——工具结果回灌后循环尾归还全量菜单，循环决策权归还模型（三层分解的忠实落地：Jev 管第一步，循环内归模型/harness）。

4. **single_tool 不用 tool_choice 强制**：单工具菜单即全部强制力（模型要么点它要么直答）；强制反而堵死 Jev 误判时模型直答的逃生门。且 OpenAICompatibleLLM 从未传过 tool_choice，加它要动 LLM 接口——YAGNI。

5. **direct 硬路由双重收益**：省全部菜单 token 之外，纯聊天流量（tools=None）落进 SemanticCacheLLM 命中区（它只在 tools=None 时生效）——免费放大既有基建。Jev 判错 direct 的代价：错答可被用户立即纠正（反馈环存在，接受）。

6. **注入免疫（选项空间封闭）**：choice 选项集是封闭集合，state 里的注入文字最多让 Jev 选错选项，产生不了自由文本动作——S3 威胁模型下的结构性防御。末道防御：Jev 返回未知串 → 当无路由。

7. **三态生命周期（硬约束：Jev 是可选增强层不是依赖）**：
   - **状态A 无 key**：装配层检测缺席 → 不挂 router（`Agent.router=None`）→ 行为与 v0.57 逐字节一致（条件装配，同 kb=None 模式）；假模型路径（mock/echo/repeat）同样不挂。启动日志显式说明。
   - **状态B 单次故障**：`route()` 内部捕获 → fail-open 返回 None（= 没有路由 = 原生路径，零新代码路径）+ 降级入账。单次调用无重试（重试伤延迟，下一轮自然再试）；超时 3s（路由在用户感知路径上等不起）。
   - **状态C 持续故障**：熔断三态 closed→open→half_open，参数与 RobustLLM 同款（连续 3 次 / 冷却 30s / 半开 1 次）——一致性优先于微调；v1 不抽公共基类（触发信号：第三个消费者）。route() 永不抛异常。

8. **主力切换**：默认 provider deepseek → deepseek-flash（`python -m agent deepseek` 可切回）。deepseek-flash 进 PROVIDERS 与 deepseek 共用 prefix/key；**备用链按 prefix 去重**（同供应商同故障域，挂同家等于故障时陪葬——落码时抓出的真 bug）。flash 价目暂按 chat 档占位待校准。

9. **可观测性**：ledger 新增 jev_calls/jev_degradations 字段（账单行「Jev 路由：N 次（降级 M 次走原生路径）」）；每轮路由结果 logger.info；降级 warning（[降级] 惯例）。**Web 前端 v1 零改动**——路由结果走日志；挂触发信号：想看「这轮走什么场景」时再加 router.decision 事件（S5b 已证 plan.* 事件转发通道可复用）。

10. **与 S5 的关系：独立、插队先行，S5c 顺延**。路由是核心层决策分工，spawn 是编排层扩展，互不依赖；不合并（两个大改动一次验收违背小步收官）。S5c 子 agent 将天然继承路由（Agent 属性自动复用）。

## 改造面

新建 core/jev.py（JevClient urllib 标准库 transport 零新依赖 + ScenarioRouter 三态）；PROVIDERS 加 deepseek-flash + 备用链去重修复；telemetry 加 jev 记账与账单行；Agent 加 router 字段（默认 None）；run_turn 加 `_route_first_menu` 轮首一针 + 循环尾归还；assemble 条件装配（JEV_API_KEY + 真模型双条件）+ 启动日志；__main__ 默认 provider 切换。tests/test_jev.py 16 个：三态生命周期（fail-open/熔断开/半开复位/半开重熔断）、路由三场景端到端（direct 藏菜单/single_tool 收窄后归还/complex 全量）、注入面检查、无 router 等价测试（状态A）、payload 形状（与 bench 协议同构）。

## 验收

ruff / mypy / pytest 三道门全绿（356 passed / 2 skipped，无 key 形态——CI 同款环境，Jev 路径全 FakeJev）。真模型 + 真 Jev 的实机验收留待日常使用（教学惯例：先实测再前进——已知注记：flash 价目待校准；Jev 路由质量在真实分布下的表现待观察）。
