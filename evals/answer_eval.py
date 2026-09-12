"""回答质量评估：LLM-as-judge（先度量，再建设）。

背景（2026-09-12 架构讨论）：在决定「要不要给 agent 加运行时 critic
子代理」之前，需要一个基线数字——当前回答的错误率是多少。本评估离线量它：

  每个测试题 → BGE 检索（与生产同链路、同闸门）→ 被评模型基于检索块
  生成回答 → 裁判模型（与被评模型不同供应商，减少自我偏好偏差）打分。

评分维度 = 忠实度（1-5）：回答是否忠于所给资料、是否编造、是否答非所问；
资料不足时「承认不足、拒绝编造」得高分（与三级信息政策的诚实残差同源）。

已知局限（如实记录）：
- 只测「检索 + 有依据生成」链路，不测工具决策链（Agentic RAG 的查不查/查什么）
- 被评 prompt 是本评估专用的有依据 prompt，非完整 SYSTEM_PROMPT
- 检索用内存库（评审第 3 条：evals 换 Chroma 复用增量同步，随 MCP 收官做）

运行：.venv/bin/python -m evals.answer_eval（需要 .env 里的 DEEPSEEK 与
SILICONFLOW API key；账单在结尾打印——评测本身的花费也入账）
"""

import json
import re

from dotenv import load_dotenv

from agent.core.llm import get_llm
from agent.core.telemetry import UsageLedger
from agent.core.types import Message
from agent.knowledge.knowledge_base import get_embedder
from evals.dataset import CASES
from evals.retrieval_eval import build_kb

CANDIDATE_MODEL = "deepseek"    # 被评 = 线上主模型
JUDGE_MODEL = "siliconflow"     # 裁判 = 不同供应商（同家自评有自我偏好偏差）

CANDIDATE_SYSTEM = (
    "你是学习助手。只依据下面「资料」回答用户问题；资料不足以回答时，"
    "明确说「资料不足」并说明无法回答，绝不编造。\n资料：\n{chunks}"
)

JUDGE_PROMPT = """你是严格的评审员。评估候选回答的质量。

问题：{question}
模型拿到的资料（它唯一被允许的依据）：
{chunks}
候选回答：
{answer}

规则：
- 忠实度优先：回答必须忠于资料；资料不足时，「承认不足并拒绝编造」是正确行为，得高分
- 评分 1-5：5 完全正确且忠于资料；4 基本正确有小瑕疵；3 部分正确或轻微脱离资料；2 明显错误或大量编造；1 答非所问或纯编造
- 只输出 JSON，不要任何其他文字：{{"score": 数字, "reason": "一句话理由"}}
"""


def parse_judge_json(text: str) -> dict | None:
    """裁判输出解析：先试整段 JSON，再试正则抠 {..} 块；都失败返回 None。"""
    text = (text or "").strip()
    for candidate in (text, re.search(r"\{[^{}]*\}", text).group() if "{" in text else ""):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and isinstance(data.get("score"), int):
                return data
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def evaluate_once(
    candidate, judge, kb, question: str
) -> tuple[dict | None, str]:
    """单题全链路：检索 → 有依据生成 → 裁判打分。返回 (裁判 dict|None, 回答)。"""
    hits = kb.search(question, top_k=5)  # 默认闸门，与生产 search_notes 同参
    chunks = "\n".join(f"- {c}" for c, _ in hits) or "（资料为空）"

    answer_msg = candidate.generate(
        [
            Message(role="system", content=CANDIDATE_SYSTEM.format(chunks=chunks)),
            Message(role="user", content=question),
        ]
    )
    answer = answer_msg.content

    verdict = judge.generate(
        [
            Message(
                role="user",
                content=JUDGE_PROMPT.format(
                    question=question, chunks=chunks, answer=answer
                ),
            )
        ]
    )
    return parse_judge_json(verdict.content), answer


def main() -> None:
    load_dotenv()
    ledger = UsageLedger()
    candidate = get_llm(CANDIDATE_MODEL, ledger, with_mock_fallback=False)
    judge = get_llm(JUDGE_MODEL, ledger, with_mock_fallback=False)
    kb = build_kb(get_embedder("siliconflow", ledger))

    print(f"===== 回答质量评估（LLM-as-judge）=====")
    print(f"被评模型: {CANDIDATE_MODEL} ｜ 裁判: {JUDGE_MODEL} ｜ 题目: {len(CASES)}\n")

    scores: list[int] = []
    for i, (question, _expected) in enumerate(CASES, 1):
        verdict, answer = evaluate_once(candidate, judge, kb, question)
        if verdict is None:
            score = None
            reason = "裁判输出解析失败"
        else:
            score = verdict["score"]
            reason = verdict.get("reason", "")
        scores.append(score if score is not None else 0)  # 解析失败计 0（大声失败）
        print(f"[{i:2d}] {question}")
        print(f"     得分: {score if score is not None else '解析失败'} ｜ {reason}")
        print(f"     回答节选: {answer[:80]}{'…' if len(answer) > 80 else ''}")

    valid = [s for s in scores]
    passed = sum(1 for s in valid if s >= 4)
    failed = sum(1 for s in valid if s <= 2)
    print("\n===== 汇总 =====")
    print(f"合格率(≥4分): {passed}/{len(valid)} = {passed / len(valid):.0%}")
    print(f"错误率(≤2分): {failed}/{len(valid)} = {failed / len(valid):.0%}")
    print(f"平均分: {sum(valid) / len(valid):.2f}")
    print(f"\n{ledger.bill()}")


if __name__ == "__main__":
    main()