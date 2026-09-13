"""个人 AI Agent 助手 —— 命令行入口。

用法：
    python -m agent          # 默认用 deepseek 真模型
    python -m agent mock     # 用 mock 假模型（不花钱、不联网）
    python -m agent echo     # 其他测试模型（只读第一个参数）
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.core.agent_loop import run_chat
from agent.core.gateway import SemanticCacheLLM
from agent.core.llm import get_llm
from agent.core.telemetry import UsageLedger
from agent.knowledge.knowledge_base import KnowledgeBase, get_embedder
from agent.knowledge.sync import sync_notes
from agent.knowledge.vector_store import ChromaVectorStore
from agent.memory.consolidate import consolidate
from agent.memory.store import load_session, save_session
from agent.paths import LEARNED_DIR, NOTES_DIR
from agent.tools.builtin import register_builtin
from agent.tools.context import ToolContext
from agent.tools.mcp_config import assemble_servers, load_server_specs
from agent.tools.registry import ToolRegistry

VERSION = "0.9.0"   # 与 pyproject [project].version 保持一致（版本号单一语义，改动时同步两处）
MEMORY_PATH = Path("data/memory/session.json")   # M6：会话记忆落盘位置（无工具用，不进 ctx；单消费者路径留本地）
VECTOR_DB_DIR = Path("data/vector_db")           # M7：向量库落盘位置（运行时数据，.gitignore 已排除）


def main() -> None:
    print(f"Personal Agent v{VERSION}")

    # 把项目根目录 .env 里的配置（API key 等）加载进环境变量
    load_dotenv()

    # 命令行第一个参数 = 用哪个模型，不传默认 deepseek
    provider = sys.argv[1] if len(sys.argv) > 1 else "deepseek"

    # 0) 账本（M7.5）：全进程一本账，LLM 与 embedding 都往里记，退出时打印
    ledger = UsageLedger()

    # 1) embedder：语义缓存与知识库共用一个（记账只注入这一处）
    #    练习模式（假模型）走词袋（离线不花一分钱）；真模型走 BGE-M3。
    #    注意两种 embedder 向量维度不同（词袋=词表长度、BGE=1024），绝不能混用
    #    同一个 Chroma 集合——所以教学组合根本不碰 Chroma，各自住各自的店
    if provider in ("mock", "echo", "repeat"):
        embedder = get_embedder("bow", ledger)
    else:
        embedder = get_embedder("siliconflow", ledger)

    # 2) 模型链（M7.5 网关 + 三方评审第 2 条拆链）：组装出两条链——
    #    内部链（internal_llm）：防护壳全套（记账/重试/精确缓存/熔断/降级），
    #        给压缩器与 search_and_summarize 的内部调用用；
    #    用户链（llm）：内部链再包一层语义档，只服务用户聊天流量——
    #        内部调用的（提示词, 回复）进缓存池有串味路径，且内部 prompt
    #        几乎不可能命中 0.92 阈值（白付 embed）
    internal_llm = get_llm(provider, ledger)
    llm = SemanticCacheLLM(internal_llm, embedder, ledger)
    print(f"当前模型：{provider}")

    # 3) 知识库（M7）：组装 embedder + store，索引走增量同步——
    #    只为真正新增/修改的笔记花 embedding 的钱；改过的自动删旧块重建
    if provider in ("mock", "echo", "repeat"):
        kb = KnowledgeBase(embedder)
    else:
        kb = KnowledgeBase(embedder, ChromaVectorStore(VECTOR_DB_DIR))
    report = sync_notes(kb, NOTES_DIR)
    print(f"知识库同步：新增 {report.added} / 删除 {report.removed} / 不变 {report.unchanged}")

    # 4) 会话记忆（M6）：启动时载入【完整会话状态】——底片(消息) + 压缩缓存(摘要游标)
    #    关键细节：必须在登记工具之前载入——search_history 的闭包要抓这个列表对象
    session = load_session(MEMORY_PATH)
    loaded_len = len(session.messages)   # M6.4：复盘起点——无新对话则退出时不白烧 LLM
    if session.messages:
        print(f"已恢复 {len(session.messages)} 条历史消息（{MEMORY_PATH}）")

    # 5) 工具（M5）：登记内置工具，交给主循环
    #    P1-2：依赖打包成 ToolContext——kb 给 search/write 查重检索、
    #    llm 给 search_and_summarize 做内部摘要（内部链，不穿语义档——评审第 2 条）、
    #    history 给会话内检索
    #    （闭包注入，传列表对象本身而非副本——run_chat 原地 append，
    #    工具才能实时看到全部历史）、notes_dir 消灭工具层写死的路径
    registry = ToolRegistry()
    ctx = ToolContext(
        notes_dir=NOTES_DIR,
        kb=kb,
        llm=internal_llm,
        history=session.messages,
    )
    register_builtin(registry, ctx)

    # 5.5) MCP 外部工具（MCP-config 配置化）：改 mcp_servers.json 加工具，零代码。
    #      命令型穿 stdio、URL 型穿 streamable HTTP；单台失败只警告不阻断；
    #      MCP_SERVERS 环境变量可指向个人配置（带 API key 的那种，不进仓库）
    try:
        specs = load_server_specs(Path(os.environ.get("MCP_SERVERS", "mcp_servers.json")))
    except ValueError as e:
        print(f"MCP 配置读取失败，本轮无外部工具：{e}")
        specs = []
    mcp_clients = assemble_servers(registry, specs)
    print(f"已装载工具：{', '.join(registry.names())}")

    # 6) 进入多轮对话主循环
    #    M5.5 起检索权在模型手里（Agentic RAG）：run_chat 不再需要 kb，
    #    知识库完全通过工具层（search_notes）介入对话
    #    M6 起：会话状态注入 → 跑完归还，本层负责落盘（组装层管策略）
    # 拆链（评审第 2 条）：压缩器内部调用走 summary_llm=内部链
    try:
        session = run_chat(llm, registry, session, summary_llm=internal_llm)
    finally:
        # MCP-b/r：无论正常退出还是异常崩掉，都关掉所有工具服务器——不留孤儿进程
        # （save 不放 finally：异常路径写回旧 session 会覆盖好数据，只在该跑时跑）
        for client in mcp_clients:
            client.close()

    # 7) 退出落盘（M6）：完整会话状态（消息 + 压缩缓存）存回 JSON，下次启动恢复
    save_session(session, MEMORY_PATH)
    print(f"对话历史已保存：{len(session.messages)} 条 → {MEMORY_PATH}")

    # 7.5) M6.4 记忆固化：退出复盘——把本轮长出来的「值得跨会话记住的项目级信息」
    #      沉淀到 data/learned/。内部调用走内部链（拆链原则）；since=启动时消息数，
    #      启动即退出（无新对话）→ consolidate 内部直接跳过
    print(consolidate(session, internal_llm, LEARNED_DIR, since=loaded_len))

    # 8) 打印本次会话账单（M7.5）：钱花哪了，退出一目了然
    print(ledger.bill())


if __name__ == "__main__":
    main()
