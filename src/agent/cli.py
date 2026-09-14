"""CLI 壳：run_chat —— input/print/退出词表，把内核 run_turn 接上终端。

内核/外设分离（S2a）的另一半：内核（orchestrator/loop.py::run_turn）只做
「一轮对话」的决策执行，本文件是它的 CLI 外设——收输入、判退出、把两条缝
（on_text/on_event）接上终端打印。

退出协议常量（EXIT_QUIT/NEW/INTERRUPT）住这里：它们是 run_chat 与装配层
（__main__）之间的契约，是壳层概念——内核 run_turn 不关心「为什么出来」，
它只关心一轮的输入输出。
"""

from __future__ import annotations

from agent.core.llm import LLM
from agent.core.types import Message
from agent.memory.compressor import trim_incomplete_round
from agent.memory.store import Session
from agent.orchestrator.loop import SYSTEM_PROMPT, run_turn
from agent.tools.registry import ToolRegistry

# 用户输入这些词就结束对话
_EXIT_WORDS = {"quit", "exit", "q", "退出", "再见"}
# S1 多会话：输入这些词开新会话。cli 只发信号不碰文件——落盘/归档归 __main__
_NEW_SESSION_WORDS = {"/new", "新会话"}

# 退出原因（S1）：让 __main__ 知道「为什么出来了」
#   quit      → 用户主动收工，正常收官
#   new       → 用户要开新会话，__main__ 执行「先存后清」再进下一轮
#   interrupt → Ctrl+C / Ctrl+D 中断（M6.2 已清理半截轮）
EXIT_QUIT = "quit"
EXIT_NEW = "new"
EXIT_INTERRUPT = "interrupt"

# 流式打印回调：块一到就上屏。打印是副作用，从 on_text 缝注入
_STREAM_PRINT = lambda text: print(text, end="", flush=True)


def _cli_on_event(event_type: str, data: dict) -> None:
    """把内核语义事件还原成 CLI 排版（与重构前的 print 逐行对齐）。"""
    if event_type == "tool_started":
        # 点菜轮收尾换行：先冒字再点菜的残字不会和工具行挤一行
        print()
        print(f"  [调用工具] {data['name']}({data['arguments']})")
    elif event_type == "tool_result":
        print(f"  [工具结果] {data['result']}")
    elif event_type == "max_rounds":
        print("  [已达到工具调用轮数上限，强制结束本轮]")
    elif event_type == "error":
        print(f"[模型不可用] {data['message']}\n本轮到此为止，网络/额度恢复后重新提问即可。")


def run_chat(
    llm: LLM,
    registry: ToolRegistry | None = None,
    session: Session | None = None,
    summary_llm: LLM | None = None,
) -> tuple[Session, str]:
    """多轮对话主循环（CLI 壳）。返回 (会话状态, 退出原因)。

    退出原因是「信号上抛、执行下放」的载体：cli 不碰文件（分层约定），但
    用户敲 quit 还是 /new 只有它知道——于是把原因编码进返回值，让装配层
    按原因决定「收官」还是「先存后清再开一轮」。

    summary_llm（拆链）：摘要压缩的内部 LLM 调用走这条链，默认沿用 llm——
    语义档只该服务用户聊天流量，内部调用的（提示词, 回复）进缓存池有串味
    路径，且内部 prompt 几乎不可能命中阈值（白付 embed）。
    """
    summarizer = summary_llm or llm
    # 会话状态：从外部注入（__main__ 从 session.json 载入 Session 后传入），
    # 底片（messages）+ 压缩缓存（summary/summarized_upto）整体进出。
    # 人设两段式：Session.messages 永远是个列表（可能是空），空则原地种人设。
    # 绝不 rebind（重新赋值）session.messages——search_history 工具的闭包抓的
    # 是 __main__ 传入的那个列表对象本身；一旦 rebind 成新列表，工具看到的
    # 永远是旧空列表，首次运行的新会话会静默失明（列表身份陷阱）
    if session is None:
        session = Session()
    if not session.messages:
        session.messages.append(Message(role="system", content=SYSTEM_PROMPT))
    print("输入 quit / exit / 退出 可结束对话；/new 开新会话。")

    try:
        while True:
            user_input = input("你：").strip()
            if user_input.lower() in _EXIT_WORDS:
                print("再见！")
                return session, EXIT_QUIT
            if user_input.lower() in _NEW_SESSION_WORDS:
                # /new：只发信号不做文件操作——会话连同「开新会话」的意图
                # 一起交给 __main__，由它执行先存后清（分层约定）
                return session, EXIT_NEW
            if not user_input:
                continue

            # 一轮交给内核跑：用户消息入底片、投影、工具循环、收尾全在 run_turn 内。
            # 返回 None = 模型不可用（内核已掐半截轮 + 发 error 事件），本轮跳过。
            reply = run_turn(
                session,
                user_input,
                llm=llm,
                registry=registry,
                summarizer=summarizer,
                on_text=_STREAM_PRINT,
                on_event=_cli_on_event,
            )
            if reply is not None:
                print()   # 回答收尾换行（模型挂时 error 事件已自带换行语义，不加）
    except (KeyboardInterrupt, EOFError):
        # M6.2：修掉 M6.1 的 tradeoff②——Ctrl+C / Ctrl+D 不再丢历史。
        # 先掐掉可能不完整的工具轮（孤儿 tool 消息落盘 = 下次启动 API 400），
        # 再把干净的历史交还给 __main__ 落盘
        print("\n[中断] 丢弃未完成的一轮，保存历史后退出。")
        trim_incomplete_round(session.messages)
        session.summarized_upto = min(session.summarized_upto, len(session.messages))   # 覆盖进度不越界

    # 会话状态交还给调用方。本函数不碰文件——落盘策略归 __main__（组装层）管
    return session, EXIT_INTERRUPT
