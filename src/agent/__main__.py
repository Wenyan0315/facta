"""个人 AI Agent 助手 —— 命令行入口。

用法：
    python -m agent              # 默认用 deepseek 真模型
    python -m agent mock         # 用 mock 假模型（不花钱、不联网）
    python -m agent echo repeat  # 其他测试模型
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.core.agent_loop import run_chat
from agent.core.llm import get_llm
from agent.knowledge.knowledge_base import KnowledgeBase, get_embedder
from agent.knowledge.loader import load_notes
from agent.memory.store import load_session, save_session
from agent.tools.builtin import register_builtin
from agent.tools.context import ToolContext
from agent.tools.registry import ToolRegistry

VERSION = "0.8.0"
NOTES_DIR = Path("data/notes")                   # P1-3：全项目唯一的笔记目录真值源
MEMORY_PATH = Path("data/memory/session.json")   # M6：会话记忆落盘位置（无工具用，不进 ctx）


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
    notes = load_notes(NOTES_DIR)
    kb = KnowledgeBase(get_embedder("siliconflow"))
    for note in notes:
        kb.add_document(note)
    print(f"知识库已装载 {len(notes)} 条笔记（BGE-M3 语义检索）。")

    # 3) 会话记忆（M6）：启动时载入【完整会话状态】——底片(消息) + 压缩缓存(摘要游标)
    #    关键细节：必须在登记工具之前载入——search_history 的闭包要抓这个列表对象
    session = load_session(MEMORY_PATH)
    if session.messages:
        print(f"已恢复 {len(session.messages)} 条历史消息（{MEMORY_PATH}）")

    # 4) 工具（M5）：登记内置工具，交给主循环
    #    P1-2：依赖打包成 ToolContext——kb 给 search/write 查重检索、
    #    llm 给 search_and_summarize 做内部摘要、history 给会话内检索
    #    （闭包注入，传列表对象本身而非副本——run_chat 原地 append，
    #    工具才能实时看到全部历史）、notes_dir 消灭工具层写死的路径
    registry = ToolRegistry()
    ctx = ToolContext(
        notes_dir=NOTES_DIR,
        kb=kb,
        llm=llm,
        history=session.messages,
    )
    register_builtin(registry, ctx)
    print(f"已装载工具：{', '.join(registry.names())}")

    # 5) 进入多轮对话主循环
    #    M5.5 起检索权在模型手里（Agentic RAG）：run_chat 不再需要 kb，
    #    知识库完全通过工具层（search_notes）介入对话
    #    M6 起：会话状态注入 → 跑完归还，本层负责落盘（组装层管策略）
    session = run_chat(llm, registry, session)

    # 6) 退出落盘（M6）：完整会话状态（消息 + 压缩缓存）存回 JSON，下次启动恢复
    save_session(session, MEMORY_PATH)
    print(f"对话历史已保存：{len(session.messages)} 条 → {MEMORY_PATH}")


if __name__ == "__main__":
    main()
