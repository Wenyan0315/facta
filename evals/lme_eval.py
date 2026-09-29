"""LongMemEval 外部锚点（P0-9 解①：拿别人出的题考自己）。

为什么是它而不是 roadmap 原文点名的 SWE-bench（原文留档不改写，理由记 ADR 058）：
  - SWE-bench full / Verified / Lite **三档全部**要 Docker，本机 `docker` 未安装
  - 官方 harness 的 Python 支持矩阵停在 3.12，本机 3.14
  - LongMemEval 考的是「长程对话记忆里捞事实」，正对本项目的记忆层；
    SWE-bench 考的是改代码，与本项目的机制面正交

数据集：`xiaowu0162/longmemeval-cleaned`（MIT，原作者维护）。S 臂单文件 277MB，
每题约 48 个历史会话 / 49 万字符（~115K tokens）——真「大海捞针」。
huggingface.co 直连超时，走 hf-mirror.com；本机 stdlib SSL 的 cafile=None
（urllib 报 CERTIFICATE_VERIFY_FAILED），所以下载用 curl 而不是 urllib。

子集口径（`build_subset`）：6 个 question_type × 每型按 question_id 排序取前 6
= 36 题。排序取前 N 而非随机抽样 —— 确定性可复现，重跑同一批题才有可比性
（046 纪律）。原始文件与子集都落在 gitignored 的 `data/bench/`，靠本文件重建。

隔离（与 frozen_eval 的副本方案不同，这里不需要副本）：
  每题一个 `tempfile.TemporaryDirectory()` 当 notes 目录，KB 用默认
  InMemoryVectorStore —— 天然零跨题污染、零磁盘副作用。**绝不碰 `data/notes/`**：
  那 16 篇是冻结集 r1/i3/i4 的 verify 计数基准，进 git 的语料资产。
  也不走 `assemble()`：它会在三处消费模块级 NOTES_DIR 并写 vector_db/graph/sessions，
  跑 36 题要 monkeypatch 五六个常量还会污染 `data/graph.json`。改为最小装配：
  `ToolContext(notes_dir, kb, llm)` + `register_builtin` = 只上 time + notes 工具族。

两个读数（互相归因，这是外部锚点最值钱的地方）：
  accuracy   LLM-judge 判对错。judge 用 siliconflow（与被测 deepseek 不同供应商，
             避自评偏好，口径同 answer_eval）。同时出 1-5 的 score 与冻结集同尺子。
             ⚠ 与论文数字**非严格同尺**（论文用 GPT-4o judge），只作量级锚定。
  hit@3      LME 自带 `answer_session_ids`（金标证据会话）⇒ 离线跑一次
             `kb.search` 就能免费区分「检索没捞到」vs「捞到了答错」。
             出两档：生产闸门（min_score=0.55）与原始召回（min_score=0）。
             两者之差 = 阈值吃掉的召回，是可直接行动的信号。

一键出分（仓库根，要真模型与 .env 里的 API key）：
    .venv/bin/python -m evals.lme_eval --download      # 一次：拉 277MB 原始文件
    .venv/bin/python -m evals.lme_eval --build         # 一次：切 36 题子集
    .venv/bin/python -m evals.lme_eval --limit 2       # 冒烟
    .venv/bin/python -m evals.lme_eval                 # 全量（36 题，含 1724 次 embed）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from facta.core.llm import get_llm
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.evalkit import parse_judge_json
from facta.knowledge.knowledge_base import KnowledgeBase, get_embedder
from facta.knowledge.sync import sync_notes
from facta.memory.store import Session
from facta.orchestrator.agent import build_default_agent
from facta.orchestrator.assemble import ensure_persona
from facta.orchestrator.loop import run_turn
from facta.tools.builtin import register_builtin
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "data" / "bench"          # gitignored（数百 MB，可一键重下）
RAW_PATH = BENCH_DIR / "longmemeval_s_cleaned.json"
SUBSET_PATH = BENCH_DIR / "lme_subset.json"
RESULTS_DIR = REPO_ROOT / "data" / "evals"        # gitignored
HF_URL = (
    "https://hf-mirror.com/datasets/xiaowu0162/longmemeval-cleaned"
    "/resolve/main/longmemeval_s_cleaned.json"
)

PER_TYPE = 6              # 每个 question_type 取几题（6 型 × 6 = 36）
TOP_K = 3                 # 检索深度，与生产 search_notes 同参
CANDIDATE_MODEL = "deepseek"    # 被评 = 线上主模型
JUDGE_MODEL = "siliconflow"     # 裁判 = 不同供应商

JUDGE_PROMPT = """你是严格的评审员。对照标准答案，判断候选回答是否正确。

问题：{question}
标准答案：{answer}
候选回答：{reply}

只输出一个 JSON 对象，不要任何其他文字：
{{"score": 1到5的整数, "correct": true或false, "reason": "一句话理由"}}

score：5=完全正确 4=基本正确有小瑕疵 3=部分正确 2=沾边但结论错 1=完全错或该答没答
correct（二值口径）：候选回答是否给出了标准答案里的关键事实。
标准答案本身表示「无法回答/信息不足」时，候选明确表示不知道才算 true，
硬编一个答案算 false。"""


def download() -> None:
    """拉原始数据（277MB）。用 curl：本机 stdlib SSL 缺 CA bundle。"""
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"下载 {HF_URL}\n  → {RAW_PATH}")
    subprocess.run(  # noqa: S603
        ["curl", "-L", "--fail", "--progress-bar", "-o", str(RAW_PATH), HF_URL],
        check=True,
    )


def build_subset() -> list[dict]:
    """切子集：6 型 × 每型按 question_id 排序取前 6 = 36 题（确定性，可复现）。"""
    if not RAW_PATH.exists():
        raise SystemExit(f"缺原始文件 {RAW_PATH}：先跑 --download")
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    by_type: dict[str, list[dict]] = defaultdict(list)
    for item in raw:
        by_type[str(item["question_type"])].append(item)
    subset: list[dict] = []
    for qtype in sorted(by_type):
        picked = sorted(by_type[qtype], key=lambda x: str(x["question_id"]))[:PER_TYPE]
        subset.extend(picked)
        print(f"  {qtype:28s} 全量 {len(by_type[qtype]):3d} → 取 {len(picked)}")
    SUBSET_PATH.write_text(json.dumps(subset, ensure_ascii=False), encoding="utf-8")
    print(f"子集 {len(subset)} 题 → {SUBSET_PATH}")
    return subset


def write_sessions(notes: Path, item: dict) -> dict[str, str]:
    """把一题的历史会话渲染成一篇篇 md（sync_notes 按文件 embed，一篇=一次 HTTP）。

    返回 文件名 → session_id：检索归因要把命中块的 source 翻回金标 session_id。
    """
    mapping: dict[str, str] = {}
    triples = zip(
        item["haystack_session_ids"], item["haystack_dates"], item["haystack_sessions"],
        strict=True,     # 长度不齐就大声炸，不静默截断
    )
    for idx, (sid, date, turns) in enumerate(triples):
        # 文件名带日期：`haystack_dates` 本就是数据集的一部分，真实对话记忆语料
        # 也按日期组织。冒烟题实测过反面——用 `000-answer_a7b44747_1.md` 这种
        # 无语义 hex 命名时，agent 调完 list_notes 就判「看不出内容」直接反问，
        # 根本没走 search_notes（测量的是命名混淆，不是记忆能力）。已记进 ADR 058。
        # `haystack_dates` 的日期分隔符是斜杠（`2023/08/11 (Fri) 15:58`），直接当
        # 文件名会被解析成子目录 —— 36/36 全炸 FileNotFoundError 踩过一次。
        # ⚠ 终端回显会把 0x2f 渲染成 `-`，肉眼核不出来，必须 `hex(ord(c))` 验。
        day = str(date).split(" ")[0].replace("/", "-")
        name = f"{day}-{idx:03d}.md"
        lines = [f"# {date}", ""]
        lines += [f"- **{t.get('role', '')}**: {t.get('content', '')}" for t in turns]
        (notes / name).write_text("\n".join(lines), encoding="utf-8")
        mapping[name] = str(sid)
    return mapping


def retrieval_hit(kb: KnowledgeBase, item: dict, mapping: dict[str, str], prod: bool) -> bool | None:
    """金标证据会话有没有进 top-3。None = 弃答题（无金标证据，不参与归因）。

    prod=True 用生产及格线（agent 实际看得见的），prod=False 降到 0 量原始召回。
    """
    gold = {str(s) for s in item.get("answer_session_ids") or []}
    if not gold:
        return None
    hits = kb.search(str(item["question"]), top_k=TOP_K, min_score=None if prod else 0.0)
    return any(mapping.get(h.source, "") in gold for h in hits)


def judge_answer(judge, item: dict, reply: str) -> dict:
    """LLM 裁判 → {"score": int, "correct": bool, "reason": str}；解析失败返回 {}。"""
    prompt = JUDGE_PROMPT.format(
        question=item["question"], answer=item["answer"], reply=reply or "（无回答）"
    )
    verdict = judge.generate([Message(role="user", content=prompt)])
    return parse_judge_json(verdict.content) or {}


def run_one(item: dict, llm, internal_llm, judge, embedder) -> dict:
    """跑一题：灌库 → agent 答题 → 裁判 → 检索归因。临时目录退出即删。"""
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        notes = Path(tmp)
        mapping = write_sessions(notes, item)
        kb = KnowledgeBase(embedder)     # InMemory：零跨题污染
        report = sync_notes(kb, notes)   # 生产同款同步路径（花钱的唯一环节）

        registry = ToolRegistry()        # audit=None：不落盘
        register_builtin(registry, ToolContext(notes_dir=notes, kb=kb, llm=internal_llm))
        agent = build_default_agent(registry, None)   # learned_dir=None：不注入项目知识
        session = Session()
        # 人设必须显式种：run_turn 不注入 system prompt，生产链路靠装配工厂
        # build_agent 内部的 ensure_persona（S8a「agent 与人设同时诞生」）。
        # 第一轮 36 题全灭的根因就是漏了这步——模型裸跑（无人设、无信息政策、
        # 无语言规则），对英文题直接回罐头客服问候，0 工具轮。
        ensure_persona(session, agent)

        tools: list[str] = []
        maxed = False

        def on_event(type_: str, data: dict) -> None:
            nonlocal maxed
            if type_ == "tool_started":
                tools.append(str(data.get("name", "")))
            elif type_ == "max_rounds":
                maxed = True

        # 题面带上 LME 的提问日期：时间推理型问题离不开「今天是哪天」
        prompt = f"今天是 {item['question_date']}。\n{item['question']}"
        result, reply = run_turn(
            session, prompt, agent=agent, llm=llm, summarizer=internal_llm,
            on_event=on_event, on_confirm=lambda name, args: True,
        )
        answer = reply.content if reply is not None else ""
        verdict = judge_answer(judge, item, answer)
        hit_prod = retrieval_hit(kb, item, mapping, prod=True)
        hit_raw = retrieval_hit(kb, item, mapping, prod=False)

    return {
        "question_id": item["question_id"],
        "question_type": item["question_type"],
        "status": result.name,
        "answer": answer,
        "gold": item["answer"],
        "correct": bool(verdict.get("correct", False)),
        "score": verdict.get("score"),
        "reason": verdict.get("reason", ""),
        "hit_at_3": hit_prod,
        "hit_at_3_raw": hit_raw,
        "chunks": report.added,
        "tools": tools,
        "max_rounds": maxed,
        "seconds": round(time.time() - t0, 1),
    }


def _rate(rows: list[dict], key: str) -> str:
    """把 bool|None 列表压成 `命中/可判 (xx%)`。None（弃答题无金标）不进分母。"""
    valid = [r for r in rows if r[key] is not None]
    if not valid:
        return "  —  "
    hit = sum(1 for r in valid if r[key])
    return f"{hit}/{len(valid)} ({hit / len(valid):.0%})"


def main() -> None:
    parser = argparse.ArgumentParser(description="LongMemEval 外部锚点")
    parser.add_argument("--download", action="store_true", help="拉原始 277MB 数据后退出")
    parser.add_argument("--build", action="store_true", help="重切 36 题子集后退出")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    parser.add_argument("--only", default="", help="只跑某个 question_type")
    parser.add_argument("--provider", default=CANDIDATE_MODEL, help="被测模型供应商")
    args = parser.parse_args()

    load_dotenv()
    if args.download:
        download()
        return
    if args.build:
        build_subset()
        return
    if not SUBSET_PATH.exists():
        raise SystemExit(f"缺子集 {SUBSET_PATH}：先跑 --download 再 --build")

    items = json.loads(SUBSET_PATH.read_text(encoding="utf-8"))
    if args.only:
        items = [x for x in items if x["question_type"] == args.only]
    if args.limit:
        items = items[: args.limit]

    ledger = UsageLedger()
    llm = get_llm(args.provider, ledger, with_mock_fallback=False)
    internal_llm = get_llm(args.provider, ledger, with_mock_fallback=False)
    judge = get_llm(JUDGE_MODEL, ledger, with_mock_fallback=False)
    embedder = get_embedder("siliconflow", ledger)

    print("===== LongMemEval S 臂（外部锚点）=====")
    print(f"被测: {args.provider} ｜ 裁判: {JUDGE_MODEL} ｜ 题目: {len(items)}\n")

    rows: list[dict] = []
    for i, item in enumerate(items, 1):
        try:
            row = run_one(item, llm, internal_llm, judge, embedder)
        except Exception as exc:  # noqa: BLE001 —— 单题炸不拖垮整轮，大声记账
            row = {
                "question_id": item["question_id"],
                "question_type": item["question_type"],
                "status": "ERROR", "answer": "", "gold": item["answer"],
                "correct": False, "score": None, "reason": f"{type(exc).__name__}: {exc}",
                "hit_at_3": None, "hit_at_3_raw": None, "chunks": 0,
                "tools": [], "max_rounds": False, "seconds": 0.0,
            }
        rows.append(row)
        flag = "✔" if row["correct"] else "✘"
        print(
            f"[{i:2d}/{len(items)}] {flag} {row['question_type']:26s} "
            f"score={row['score']} hit@3={row['hit_at_3']} raw={row['hit_at_3_raw']} "
            f"块={row['chunks']} 轮={len(row['tools'])} {row['seconds']}s",
            flush=True,
        )
        if not row["correct"]:
            print(f"        Q: {item['question'][:70]}")
            print(f"        金标: {str(row['gold'])[:70]}")
            print(f"        答: {row['answer'][:70]}")
            print(f"        裁判: {row['reason'][:70]}", flush=True)

    correct = sum(1 for r in rows if r["correct"])
    scores = [r["score"] for r in rows if isinstance(r["score"], int)]
    print("\n===== 总分 =====")
    print(f"accuracy（外部锚点二值口径）: {correct}/{len(rows)} = {correct / max(len(rows), 1):.1%}")
    print(f"质量分均值（1-5，与冻结集同尺子）: {sum(scores) / max(len(scores), 1):.2f}")
    print(f"检索 hit@3（生产闸门）: {_rate(rows, 'hit_at_3')}")
    print(f"检索 hit@3（原始召回）: {_rate(rows, 'hit_at_3_raw')}")
    print(f"工具预算跑满（max_rounds）: {sum(1 for r in rows if r['max_rounds'])}/{len(rows)}")
    print(
        f"成本: tokens_in={ledger.tokens_in} tokens_out={ledger.tokens_out} "
        f"embed_tokens={ledger.embed_tokens} ¥{ledger.llm_cost + ledger.embed_cost:.4f}"
    )

    print("\n===== 按题型 =====")
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[str(r["question_type"])].append(r)
    for qtype in sorted(by_type):
        g = by_type[qtype]
        n = sum(1 for r in g if r["correct"])
        print(f"  {qtype:28s} acc {n}/{len(g)}  hit@3 {_rate(g, 'hit_at_3')}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"lme-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "stamp": stamp, "provider": args.provider, "judge": JUDGE_MODEL,
                "n": len(rows), "correct": correct,
                "accuracy": round(correct / max(len(rows), 1), 4),
                "score_mean": round(sum(scores) / max(len(scores), 1), 3),
                "tokens_in": ledger.tokens_in,
                "embed_tokens": ledger.embed_tokens,
                "cost": round(ledger.llm_cost + ledger.embed_cost, 6),
                "rows": rows,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n出分落盘：{out}")


if __name__ == "__main__":
    main()
