"""个人 AI Agent 助手 —— 命令行入口。"""

from agent.core.agent_loop import run_chat
from agent.core.llm import get_llm

VERSION = "0.3.0"


def main() -> None:
    print(f"Personal Agent v{VERSION}")

    # 拿到一个模型实现（现在是 mock，可改成 echo / 未来 deepseek）
    llm = get_llm("mock")

    # 进入多轮对话主循环
    run_chat(llm)


if __name__ == "__main__":
    main()