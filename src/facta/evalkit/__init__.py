"""evalkit：评测内核组件（纯函数，零项目依赖）——指标/指纹判定/归因/裁判解析。

从 evals/ 内核化而来（决策记录 024）：evals/ 保留项目壳（语料、题库、
embedder 配置、闸门校准值），本包只放「换一个项目也能用」的部分。
设计约束：不 import agent.* 任何模块——拿走这一个包就是完整可用的。

为什么手写而不用 Ragas/DeepEval（024 裁定）：评测指标是 agent 开发的
原理件（数据驱动迭代的地基），手写才知道分数怎么来的、能按自己的形态
改造（022 的三路归因/形态分层即例）；成熟轮子面向英文 RAG 通用场景，
且拖着 langchain 生态的依赖面。引轮子的触发信号：评测从「学习原理」
变成「生产工程」（CI 大规模评测矩阵/对抗题库自动生成）时。
"""

from agent.evalkit.judge import parse_judge_json
from agent.evalkit.ranking import (
    attribute_miss,
    is_relevant,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

__all__ = [
    "attribute_miss",
    "is_relevant",
    "parse_judge_json",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
