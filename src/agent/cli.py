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
from agent.orchestrator.agent import DEFAULT_SYSTEM_PROMPT, Agent
from agent.orchestrator.loop import RunResult, run_turn
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

def _stream_print(text: str) -> None:
    """流式打印回调：块一到就上屏。打印是副作用，从 on_text 缝注入。"""
    print(text, end="", flush=True)


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
    elif event_type == "stuck":
        print(f"  [检测到原地重复调用（{'、'.join(data['tools'])}），已停止本轮——可换个说法或缩小任务重试]")
    elif event_type == "error":
        print(f"[模型不可用] {data['message']}\n本轮到此为止，网络/额度恢复后重新提问即可。")
    elif event_type == "plan.created":
        print("  [计划创建]")
        print(f"  {format_plan_event_steps(data)}")
    elif event_type == "plan.revised":
        print(f"  [计划修订] {data.get('reason', '')}")
        print(f"  {format_plan_event_steps(data)}")
    elif event_type == "plan.step_updated":
        note = f" —— {data['note']}" if data.get("note") else ""
        print(f"  [计划] 步骤 #{data['id']} → {data['status']}{note}")
    elif event_type == "plan.finished":
        print(f"  [计划] 任务收官：{data.get('summary', '')}")


def format_plan_event_steps(data: dict) -> str:
    """计划创建/修订事件的步骤表渲染（CLI 版；来源 data['steps']）。"""
    return " → ".join(f"{s['id']}.{s['title']}" for s in data.get("steps", []))


def _cli_on_confirm(name: str, args: dict) -> bool:
    """L2 确认缝的 CLI 实现（S4b）：命令全文上屏，input 裁决。

    默认拒绝（空回车/任意非 y 输入都算拒）——高危操作的保守默认，
    批准必须是显式动作。Ctrl+C 中断也算拒（异常沿既有中断通道上抛）。
    """
    print(f"\n  ⚠️ 高危操作待确认：{name}")
    print(f"  {args.get('command', args)}")
    answer = input("  批准执行？输入 y 确认，其余任意键拒绝：").strip().lower()
    return answer == "y"


def run_chat(
    llm: LLM,
    agent: Agent | None = None,
    session: Session | None = None,
    summary_llm: LLM | None = None,
) -> tuple[Session, str]:
    """多轮对话主循环（CLI 壳）。返回 (会话状态, 退出原因)。

    退出原因是「信号上抛、执行下放」的载体：cli 不碰文件（分层约定），但
    用户敲 quit 还是 /new 只有它知道——于是把原因编码进返回值，让装配层
    按原因决定「收官」还是「收官后另起一段」。

    agent（S5a）：执行单元。None=裸会话兜底（无工具 + 默认人设）——壳层
    舒适原则，测试传 None 照常工作；内核 run_turn 仍要求显式 agent。

    summary_llm（拆链）：摘要压缩的内部 LLM 调用走这条链，默认沿用 llm——
    语义档只该服务用户聊天流量，内部调用的（提示词, 回复）进缓存池有串味
    路径，且内部 prompt 几乎不可能命中阈值（白付 embed）。
    """
    summarizer = summary_llm or llm
    if agent is None:
        # 裸会话兜底：无工具 + 默认人设（与旧 registry=None 语义等价）
        agent = Agent(
            name="bare", system_prompt=DEFAULT_SYSTEM_PROMPT, registry=ToolRegistry()
        )
    # 会话状态：从外部注入（__main__ 从会话仓库 load 后传入），底片（messages）
    # + 压缩缓存（summary/summarized_upto）整体进出。
    # 人设两段式：Session.messages 永远是个列表（可能是空），空则原地种人设。
    # 本函数内绝不 rebind（重新赋值）session.messages——search_history 工具的
    # 闭包抓的是传进来的那个列表对象本身；一旦 rebind 成新列表，工具看到的
    # 永远是旧空列表（列表身份陷阱）。
    # S8a 后「换新会话」不再靠原地 clear + 补种人设，而是 __main__ 另建 Session
    # 并重造 agent —— 闭包与会话同生共死，这条纪律的适用范围缩回本函数内部。
    if session is None:
        session = Session()
    if not session.messages:
        session.messages.append(Message(role="system", content=agent.system_prompt))
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
            # COMPLETED = 正常结束；CANCELLED/FAILED = 本轮无产出（内核已处理）
            result, reply = run_turn(
                session,
                user_input,
                agent=agent,
                llm=llm,
                summarizer=summarizer,
                on_text=_stream_print,
                on_event=_cli_on_event,
                on_confirm=_cli_on_confirm,
            )
            if result is RunResult.COMPLETED and reply is not None:
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
