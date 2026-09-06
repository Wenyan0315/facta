"""个人 AI Agent 助手 —— 命令行入口。

用法：
    python -m agent              # 默认用 deepseek 真模型
    python -m agent mock         # 用 mock 假模型（不花钱、不联网）
    python -m agent echo repeat  # 其他测试模型
"""

import sys

from dotenv import load_dotenv

from agent.core.agent_loop import run_chat
from agent.core.llm import get_llm
from agent.knowledge.knowledge_base import KnowledgeBase, get_embedder
from agent.knowledge.loader import load_notes
from agent.tools.builtin import register_builtin
from agent.tools.registry import ToolRegistry

VERSION = "0.8.0"


def main() -> None:
    print(f"Personal Agent v{VERSION}")

    # 把项目根目录 .env 里的配置（API key 等）加载进环境变量
    load_dotenv()

    # 命令行第一个参数 = 用哪个模型，不传默认 deepseek
    provider = sys.argv[1] if len(sys.argv) > 1 else "deepseek"

    # 1) 模型
    llm = get_llm(provider)
    print(f"当前模型：{provider}")

    # 2) 知识库：从 data/notes/ 读笔记，BGE-M3 语义检索
    #    以后加笔记 = 往 data/notes/ 丢一个 md 文件，代码零改动
    notes = load_notes()
    kb = KnowledgeBase(get_embedder("siliconflow"))
    for note in notes:
        kb.add_document(note)
    print(f"知识库已装载 {len(notes)} 条笔记（BGE-M3 语义检索）。")

    # 3) 工具（M5）：登记内置工具，交给主循环
    #    kb 给 search/write 查重检索、llm 给 search_and_summarize 做内部摘要（闭包注入）
    registry = ToolRegistry()
    register_builtin(registry, kb, llm)
    print(f"已装载工具：{', '.join(registry.names())}")

    # 4) 进入多轮对话主循环
    #    M5.5 起检索权在模型手里（Agentic RAG）：run_chat 不再需要 kb，
    #    知识库完全通过工具层（search_notes）介入对话
    run_chat(llm, registry)


if __name__ == "__main__":
    main()
