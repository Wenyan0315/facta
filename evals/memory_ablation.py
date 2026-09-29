"""记忆注入消融（B 路）：同一批问题，有/无记忆注入各跑一遍对比（2026-09-26）。

对照点唯一：build_default_agent 的 learned_dir 参数——
  with-memory  传真实 data/learned（_learned_block 注入 24 条快照）
  no-memory    传 None（文档承诺「与三桶全空同收敛：无注入」，agent.py:181）
这是现成的开关，不需要 env 变量——装配期参数本身就是消融闸门。

工具菜单：空 registry（一个工具都不装）。这些问题的答案只可能来自注入的
记忆快照；装上工具反而引入变量（模型可能去 search_notes 蒙到重叠主题）。

首轮跑出的方法学教训（2026-09-26）：光给空菜单不够——基础 prompt 教模型
「先查再说」，模型拿不到工具就把调用标记当正文吐出来（`<｜｜DSML｜｜ invoke
name="search_notes">`），14 题里 7 题没真答，其中 3 题还因标记里的 query
关键词**假命中**指纹（d-php 命中「工具层」纯粹是 query 带了"工具"二字）。
故追加 CLOSED_BOOK 闭卷指令——两组同样追加，对照仍然只差记忆注入。
副作用是把无记忆组变成诚实度测量：答不出时坦白还是瞎编。

判定离线（不花 LLM）：回答含任一答案指纹即「用上了记忆」。

运行（项目根目录）：
    .venv/bin/python -m evals.memory_ablation [--provider deepseek-flash]
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from evals.memory_cases import CASES
from facta.core.llm import get_llm
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.orchestrator.agent import build_default_agent
from facta.paths import LEARNED_DIR
from facta.tools.registry import ToolRegistry

# 闭卷指令（追加在问题后，不改 system prompt——改 prompt 就不止消融记忆了）
CLOSED_BOOK = (
    "\n\n（本轮闭卷：你没有任何工具可用，请直接依据你已掌握的项目知识回答。"
    "不知道就明说不知道，不要输出工具调用标记，不要编造。）"
)


def run_group(llm, learned_dir, label: str) -> dict[str, tuple[list[str], str]]:
    """一组跑完 CASES 全部题：{id: (命中的指纹列表, 回答前 120 字)}。

    返回命中指纹而非布尔值：对照时要区分「答对」与「蒙到宽松词」
    （如 ["日志"] 这种通用词，无记忆也能命中 = 假阳性）。
    """
    agent = build_default_agent(
        registry=ToolRegistry(),      # 空菜单：见模块 docstring
        learned_dir=learned_dir,
        router=None,
        user_memory_path=None,        # user.md 为空，注入与否无差异，干脆不挂
    )
    print(f"\n===== {label} =====")
    out: dict[str, tuple[list[str], str]] = {}
    for cid, question, fingerprints, _bucket in CASES:
        reply = llm.generate([
            Message(role="system", content=agent.system_prompt),
            Message(role="user", content=question + CLOSED_BOOK),
        ])
        answer = reply.content or ""
        hits = [fp for fp in fingerprints if fp in answer]
        out[cid] = (hits, answer[:120].replace("\n", " "))
        print(f"[{','.join(hits) or '—':<14}] {cid:<16} {answer[:120].strip()!r}", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记忆注入消融（B 路）")
    parser.add_argument("--provider", default="deepseek-flash", help="默认 deepseek-flash")
    args = parser.parse_args(argv)

    load_dotenv()
    ledger = UsageLedger()
    # with_mock_fallback=False：消融被 mock 顶替 = 两组拿到同一份假回答，对比失效
    llm = get_llm(args.provider, ledger=ledger, with_mock_fallback=False)

    with_mem = run_group(llm, LEARNED_DIR, "有记忆（learned_dir=data/learned）")
    without_mem = run_group(llm, None, "无记忆（learned_dir=None）")

    print("\n===== 对照 =====")
    gain = 0
    for cid, _q, _fp, bucket in CASES:
        w = with_mem[cid][0]
        wo = without_mem[cid][0]
        delta = "记忆带来" if (w and not wo) else ("一致" if w == wo else "反常(无记忆反而中)")
        gain += 1 if (w and not wo) else 0
        print(
            f"{cid:<16} [{bucket:<11}] 有={','.join(w) or '×':<18} "
            f"无={','.join(wo) or '×':<18} {delta}"
        )

    w_hits = sum(1 for h, _ in with_mem.values() if h)
    wo_hits = sum(1 for h, _ in without_mem.values() if h)
    n = len(CASES)
    print(f"\n有记忆命中 {w_hits}/{n}，无记忆命中 {wo_hits}/{n}，净增量 {gain} 题")
    print(f"token in={ledger.tokens_in} out={ledger.tokens_out}，cost={ledger.llm_cost:.4f}")
    print("注：题目从真实 learned 反推（memory_cases.py 有剔除清单对账），属方向性证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
