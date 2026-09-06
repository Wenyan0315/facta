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

from agent.core.llm import LLM, Message
from agent.memory.compressor import build_payload, maybe_compress, trim_incomplete_round
from agent.tools.registry import ToolRegistry

SYSTEM_PROMPT = (
    "你是一个 AI 学习助手，帮助我学习 AI Agent 开发。"
    "信息使用政策（按优先级）："
    "①优先用 search_notes 检索我的个人知识库，基于笔记回答；"
    "②资料不足时，可用其他工具（如读取完整笔记）补充；"
    "③以上都没有时，用你自己的知识回答，"
    "但必须标注「以下来自我的通用知识，非笔记内容」。"
    "需要事实信息（比如当前时间）时，主动使用工具获取。"
    "你的历史对话由系统自动保存，过长时自动压缩为摘要；"
    "摘要中的信息等同于你的亲历记忆，可直接引用，不要声称自己记不住。"
)

# 用户输入这些词就结束对话
_EXIT_WORDS = {"quit", "exit", "q", "退出", "再见"}

# 工具循环保险丝：模型理论上可能一直点菜不收敛，永远要给循环设上限
_MAX_TOOL_ROUNDS = 5


def run_chat(
    llm: LLM,
    registry: ToolRegistry | None = None,
    messages: list[Message] | None = None,
) -> list[Message]:
    # 历史列表：M6 起可从外部注入（__main__ 从 session.json 载入后传入）
    # 必须写 if not messages 而不是 is None——load_messages 首跑返回的是 []
    # 不是 None：空列表也要种人设，否则第一次运行的 agent 会没有 system prompt
    # （M5.5 起：三级信息政策也从"每轮拼进消息"升级为写进人设，一次设定全程生效）
    if not messages:
        messages = [Message(role="system", content=SYSTEM_PROMPT)]
    # 菜单只生成一次，整个会话复用
    tools = registry.schemas() if registry else None
    print("输入 quit / exit / 退出 可结束对话。")

    # M6.2 压缩状态：滚动摘要 + 覆盖进度（第 0 条 system 人设永不入摘要，从 1 起算）
    summary: str | None = None
    summarized_upto = 1

    try:
        while True:
            user_input = input("你：").strip()
            if user_input.lower() in _EXIT_WORDS:
                print("再见！")
                break
            if not user_input:
                continue

            # 1) 用户这句话存进历史（底片照常全量生长，append-only 不变）
            messages.append(Message(role="user", content=user_input))

            # 2) M6.2 发送前投影：触发式摘要（内部调用）→ 切 payload
            summary, summarized_upto = maybe_compress(
                llm, messages, summary, summarized_upto
            )
            payload = build_payload(messages, summary, summarized_upto)

            # 3) 工具循环：决策 → 执行 → 观察 → 再决策（M5 心脏）
            for _round in range(_MAX_TOOL_ROUNDS):
                reply = llm.generate(payload, tools)   # 发的是投影，不是底片

                if not reply.tool_calls:   # 模型不点菜了 → 最终回答，退出循环
                    break

                # 双写：底片入史（落盘用）+ 投影同步（本轮内模型必须看得见）
                messages.append(reply)
                payload.append(reply)

                for tc in reply.tool_calls:   # 模型一次可能点多个菜
                    print(f"  [调用工具] {tc['name']}({tc['arguments']})")
                    result = registry.execute(tc["name"], tc["arguments"])
                    print(f"  [工具结果] {result}")
                    # 结果以 role="tool" 回填，tool_call_id 对应是哪次调用
                    tool_msg = Message(role="tool", tool_call_id=tc["id"], content=result)
                    messages.append(tool_msg)
                    payload.append(tool_msg)
            else:
                # for 循环跑满都没 break（模型点菜上瘾）→ 强制收尾
                print("  [已达到工具调用轮数上限，强制结束本轮]")
                reply = llm.generate(payload, None)  # 最后一问不递菜单，逼它说话

            # 4) 收尾：最终回答只入底片（投影本轮作废，下轮重切）
            messages.append(reply)

            print(f"agent：{reply.content}")
    except (KeyboardInterrupt, EOFError):
        # M6.2：修掉 M6.1 的 tradeoff②——Ctrl+C / Ctrl+D 不再丢历史。
        # 先掐掉可能不完整的工具轮（孤儿 tool 消息落盘 = 下次启动 API 400），
        # 再把干净的历史交还给 __main__ 落盘
        print("\n[中断] 丢弃未完成的一轮，保存历史后退出。")
        trim_incomplete_round(messages)
        summarized_upto = min(summarized_upto, len(messages))   # 覆盖进度不越界

    # 历史交还给调用方。本函数不碰文件——落盘策略归 __main__（组装层）管
    return messages
