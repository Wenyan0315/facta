"""Agent 主循环：让用户和模型连续多轮对话。

核心理解：LLM 本身是无状态的——它不记得上一轮说过什么。
所谓的"会话记忆"，其实是程序里维护一个 messages 列表，
每一轮都把【完整的对话历史】一起发给模型。
"""

from agent.core.llm import LLM, Message

SYSTEM_PROMPT = "你是一个 AI 学习助手，帮助我学习 AI Agent 开发。"

# 用户输入这些词就结束对话
_EXIT_WORDS = {"quit", "exit", "q", "退出", "再见"}


def run_chat(llm: LLM) -> None:
    # 历史列表：先放一条 system 消息，给模型定"人设"
    messages: list[Message] = [Message(role="system", content=SYSTEM_PROMPT)]
    print("输入 quit / exit / 退出 可结束对话。")

    while True:
        user_input = input("你：").strip()
        if user_input.lower() in _EXIT_WORDS:
            print("再见！")
            break
        if not user_input:
            continue

        # 1) 把用户这句话追加进历史
        messages.append(Message(role="user", content=user_input))

        # 2) 把【整个历史列表】交给模型，而不是只交这一句
        reply = llm.generate(messages)

        # 3) 把模型回复也追加进历史，下一轮它就能"记得"
        messages.append(reply)

        print(f"agent：{reply.content}")