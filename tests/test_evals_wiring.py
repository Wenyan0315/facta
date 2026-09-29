"""evals 接线回归：build_kb 必须可运行（评估与线上同一份语料的不变量）。

来历（2026-09-10 code review 的 Critical 教训）：evals 曾以 NameError 状态
被提交——pytest 不跑 evals，单纯 import 模块又不执行函数体，断链直到
人工调用 build_kb 才炸。把「evals 可运行」钉成断言，这类断链当场暴露。
"""

from facta.knowledge.knowledge_base import BagOfWordsEmbedder


def test_evals_build_kb_runs():
    """build_kb 用离线词袋模型搭库：不崩 + 语料真的装进去了。"""
    from evals.retrieval_eval import build_kb

    kb = build_kb(BagOfWordsEmbedder())
    # min_score=0.0 关掉闸门：只要有块就该有返回——证明语料非空加载，
    # 而不是「没崩但空库」的假绿
    assert kb.search("PHP", top_k=1, min_score=0.0)
