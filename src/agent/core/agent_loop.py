"""Agent 主循环：让用户和模型连续多轮对话。

核心理解：LLM 本身是无状态的——它不记得上一轮说过什么。
所谓的"会话记忆"，其实是程序里维护一个 messages 列表，
每一轮都把【完整的对话历史】一起发给模型。

M3 起：RAG 增强——提问前检索相关笔记拼进消息。
M5 起：工具调用循环（ReAct 雏形）——

    决策 → 执行 → 观察 → 再决策 → ... → 最终回答

模型说"我想调用工具"（tool_calls）→ 我们执行 → 结果以 role="tool"
回填 → 模型看到结果再决策。循环直到模型给出最终文本回复。
"""

from __future__ import annotations

from datetime import datetime

from agent.core.llm import LLM, LLMUnavailableError
from agent.core.types import Message
from agent.memory.compressor import build_payload, maybe_compress, trim_incomplete_round
from agent.memory.store import Session
from agent.tools.registry import ToolRegistry

SYSTEM_PROMPT = (
    "你是一个 AI 学习助手，帮助我学习 AI Agent 开发。"
    "信息使用政策（按优先级）："
    "①优先用 search_notes 检索我的个人知识库，基于笔记回答；"
    "②资料不足时，可用其他工具（如读取完整笔记）补充；"
    "③以上都没有时，用你自己的知识回答，"
    "但必须标注「以下来自我的通用知识，非笔记内容」。"
    "需要事实信息（比如当前时间）时，主动使用工具获取。"
    "你的历史对话由系统自动保存、跨重启恢复——恢复的历史与当前对话属于"
    "同一个持续会话；用户说'这轮对话''这轮对话''我们聊过的'时，指含恢复历史的"
    "整个会话，而非最近一次问答。历史过长时自动压缩为摘要；"
    "摘要中的信息等同于你的亲历记忆，可直接引用，不要声称自己记不住。"
    "需要早前对话的逐字原话时，用 search_history 检索完整历史。"
)

# 用户输入这些词就结束对话
_EXIT_WORDS = {"quit", "exit", "q", "退出", "再见"}

# 工具循环保险丝：模型理论上可能一直点菜不收敛，永远要给循环设上限
_MAX_TOOL_ROUNDS = 5

_WEEKDAYS = "一二三四五六日"


def _time_stamp(now: datetime | None = None) -> Message:
    """当前时间戳（投影专用，绝不入底片）。

    为什么要它（真实使用经验逼出来的）：跨会话恢复时，模型没有「现在」的
    概念，会拿上次对话的时间当锚点，安静地算错一切相对时间——"更新数据"
    取到半个月前的日期还不报错。时间戳管「今天是哪天」这个锚点；
    get_current_time 工具继续管秒级精度与未来时间点。

    进投影不进底片的理由：时间属于「本轮视野」而非「对话内容」——
    入底片会堆日期垃圾、被摘要吸收；投影每轮现切、随轮作废。
    now 参数留给测试注入固定时刻。
    """
    now = now or datetime.now()
    return Message(
        role="system",
        content=f"今天：{now:%Y-%m-%d}（周{_WEEKDAYS[now.weekday()]}）{now:%H:%M}",
    )


def run_chat(
    llm: LLM,
    registry: ToolRegistry | None = None,
    session: Session | None = None,
) -> Session:
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
    # （M5.5 起：三级信息政策写进人设，一次设定全程生效）
    # 菜单只生成一次，整个会话复用
    tools = registry.schemas() if registry else None
    print("输入 quit / exit / 退出 可结束对话。")

    try:
        while True:
            user_input = input("你：").strip()
            if user_input.lower() in _EXIT_WORDS:
                print("再见！")
                break
            if not user_input:
                continue

            # 1) 用户这句话存进历史（底片照常全量生长，append-only 不变）
            session.messages.append(Message(role="user", content=user_input))

            # 2) M6.2 发送前投影：触发式摘要（内部调用）→ 切 payload
            #    压缩缓存记在 Session 上——随底片一起落盘，重启不从头再压
            # 3) 工具循环：决策 → 执行 → 观察 → 再决策（M5 心脏）
            # 4) 收尾：最终回答只入底片（投影本轮作废，下轮重切）
            # —— 2/3/4 都罩在 LLMUnavailableError 保护下（M7.5d F 契约）：
            #    模型全挂 → 优雅结束本轮而非崩溃；半截工具轮掐掉（防孤儿 tool
            #    落盘）；用户消息留在底片；提示语只打印、不进历史
            try:
                session.summary, session.summarized_upto = maybe_compress(
                    llm, session.messages, session.summary, session.summarized_upto
                )
                payload = build_payload(session.messages, session.summary, session.summarized_upto)
                # 时间锚点注入投影（不入底片）：位置固定在第 2 条（system 之后、
                # 摘要/对话之前）；本轮工具循环共享同一个时间戳
                payload.insert(1, _time_stamp())

                for _round in range(_MAX_TOOL_ROUNDS):
                    reply = llm.generate(payload, tools)   # 发的是投影，不是底片

                    if not reply.tool_calls:   # 模型不点菜了 → 最终回答，退出循环
                        break

                    # 双写：底片入史（落盘用）+ 投影同步（本轮内模型必须看得见）
                    session.messages.append(reply)
                    payload.append(reply)

                    for tc in reply.tool_calls:   # 模型一次可能点多个菜
                        print(f"  [调用工具] {tc['name']}({tc['arguments']})")
                        result = registry.execute(tc["name"], tc["arguments"])
                        print(f"  [工具结果] {result}")
                        # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
                        tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
                        session.messages.append(tool_msg)
                        payload.append(tool_msg)
                else:
                    # for 循环跑满都没 break（模型点菜上瘾）→ 强制收尾
                    print("  [已达到工具调用轮数上限，强制结束本轮]")
                    reply = llm.generate(payload, None)  # 最后一问不递菜单，逼它说话

                session.messages.append(reply)
                print(f"agent：{reply.content}")
            except LLMUnavailableError as exc:
                trim_incomplete_round(session.messages)
                print(f"[模型不可用] {exc}\n本轮到此为止，网络/额度恢复后重新提问即可。")
    except (KeyboardInterrupt, EOFError):
        # M6.2：修掉 M6.1 的 tradeoff②——Ctrl+C / Ctrl+D 不再丢历史。
        # 先掐掉可能不完整的工具轮（孤儿 tool 消息落盘 = 下次启动 API 400），
        # 再把干净的历史交还给 __main__ 落盘
        print("\n[中断] 丢弃未完成的一轮，保存历史后退出。")
        trim_incomplete_round(session.messages)
        session.summarized_upto = min(session.summarized_upto, len(session.messages))   # 覆盖进度不越界

    # 会话状态交还给调用方。本函数不碰文件——落盘策略归 __main__（组装层）管
    return session
