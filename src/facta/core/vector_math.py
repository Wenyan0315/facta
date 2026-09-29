"""向量数学：全项目共享的最小数学函数（core 地基）。

cosine_similarity 的搬迁史——每次都被「新消费者」逼着往地基挪：
  1. 最初住在 knowledge_base.py（知识层自用）
  2. M7 时向量检索下放给 store，跟着唯一使用者搬到 knowledge/vector_store.py
  3. M7.5c 网关（core 层）的语义缓存也要算余弦——core 反向 import
     knowledge 就是依赖倒挂（P1-1 治过的病），所以再搬到 core 地基，
     知识层与网关都从这里拿。
规律：一个函数有跨层多个消费者时，它属于最低公共层。
"""

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度：两个向量夹角的余弦，越接近 1 越相似。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))   # 维度不同是调用方 bug，该炸
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
