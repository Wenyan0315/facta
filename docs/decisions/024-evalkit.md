# 决策记录 · evalkit 评测内核化（2026-09-19）

> 评测从项目代码长成可复用组件的第一步：仓内内核化。返回 [architecture.md](../architecture.md)

- **动机与解读裁定**：「把评测作为通用组件拿出来」的两种解读——A 学库设计（API 边界/抽象切分的学习主线延伸）vs B 给别人用（产品方向分叉）。裁定按 A 执行；B 的升格触发信号见下。资产盘点：现状 evals 七成是零项目耦合的纯函数（指标/指纹判定/归因/judge 解析），三成是项目壳（语料路径/embedder/闸门校准值）——比例健康，内核化是小站不是大手术。

- **为什么不引成熟轮子（Ragas/DeepEval/promptfoo）**：三层理由——①哲学层：评测指标是 agent 开发的**原理件**（数据驱动迭代的地基），手写才知道分数怎么来的、能按自己的形态改造（022 的三路归因/形态分层/生产参数对照，成熟轮子均无此形态）；与手写 MCP 客户端不引 SDK（009）、手写 Tavily REST（015）同一裁定的延续。②实操层：Ragas/DeepEval 拖 langchain 生态与 pydantic 版本地狱进干净 venv；面向英文 RAG 通用场景，硬套要扭曲自己适配。③划界：哲学适用于原理件不适用于工程件——**引轮子触发信号=评测从「学习原理」变成「生产工程」**（CI 大规模评测矩阵/对抗题库自动生成）。

- **切分**：`src/agent/evalkit/`（ranking.py：is_relevant 指纹判定 + P@k/R@k/MRR + attribute_miss 归因；judge.py：parse_judge_json 裁判解析）——**零 agent.* 依赖，拷走即用**；evals/ 退化为壳（CASES 题库/语料/KnowledgeBase 装配/grep 模拟/闸门值/打印表格），retrieval_eval 与 answer_eval 改吃内核。放 src/ 而非 evals/ 的关键理由：**mypy 只覆盖 src/agent**——内核自动进三道门，evals/ 则无类型检查。消费者=1（本仓库），按「至少两个真实消费者才升格独立包」纪律（cosine_similarity 搬迁史同款规律）；**升格触发信号=出现第二个真实消费者**。

- **搬家即增值的活案例**：内核化当天 mypy 就抓出 judge.py 的真问题——`re.search(...).group()` 在无匹配时炸（旧代码藏在 evals/ 时无类型检查，潜伏至今）；顺手把「`"{" in text` 优化 + AttributeError 捕获」简化为 match 守卫。行为保持验证：BGE 冒烟与 022 基线逐位一致（P@5=0.20/R@5=0.96/MRR=0.74，miss 5/26）。

- 验收：三道门全绿（mypy 覆盖 44→47 文件；pytest 295→304——test_evalkit 新增 12 用例锁死内核行为契约，test_answer_eval 的 3 个 parse 用例迁入，净 +9）；retrieval_eval 端到端冒烟。
