"""组装层：assemble —— 把各层依赖按 provider 组装成一个可运行的 AppContext。

单一真值源（S2a）：CLI（__main__）与未来的 Web 入口都调这里，不在各自
入口重复装配——否则 Web 入口一加，就是第二个「组装真值源」（P1 治过的病）。

provider 从参数进、不碰 sys.argv：sys.argv 是 CLI 的衣服，Web 的 provider
可能来自环境变量或默认值。谁解析 provider 谁决定，assemble 只管装配。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.core.types import Message
from agent.core.gateway import SemanticCacheLLM
from agent.core.llm import LLM, get_llm
from agent.core.telemetry import UsageLedger
from agent.knowledge.knowledge_base import KnowledgeBase, get_embedder
from agent.knowledge.sync import sync_notes
from agent.knowledge.vector_store import ChromaVectorStore
from agent.memory.store import Session, load_session
from agent.orchestrator.loop import SYSTEM_PROMPT
from agent.paths import NOTES_DIR
from agent.tools.builtin import register_builtin
from agent.tools.context import ToolContext
from agent.tools.mcp_config import assemble_servers, load_server_specs
from agent.tools.registry import ToolRegistry
from agent.tools.web import TavilySearch, register_web_tools

# 组装层唯一真值源：CLI / Web 都从这里拿路径，不在各自入口重定义
MEMORY_PATH = Path("data/memory/session.json")   # M6：会话记忆落盘位置（无工具用，不进 ctx）
VECTOR_DB_DIR = Path("data/vector_db")           # M7：向量库落盘位置（运行时数据，.gitignore 已排除）


@dataclass
class AppContext:
    """assemble 的产物：一套完整的运行依赖，CLI / Web 共用。"""

    provider: str
    ledger: UsageLedger
    embedder: Any
    llm: LLM            # 用户链（带语义档），服务用户聊天流量
    internal_llm: LLM   # 内部链（无语义档），给压缩器/工具内调用
    kb: KnowledgeBase
    session: Session
    registry: ToolRegistry
    mcp_clients: list = field(default_factory=list)   # 最终退出时统一 close，不留孤儿进程


def ensure_persona(session: Session) -> None:
    """人设保证（装配不变量，S2 验收修复轮）：会话必须带着 SYSTEM_PROMPT 开工。

    「空会话种人设」原本只住在 CLI 壳——Web 入口曾跑过无人设会话（真实使用
    踩中：语言漂移、信息政策失效、自我认知靠模型编）。两分支：
    - 空会话：种人设（与 cli.py 的守卫幂等——双方都判 messages 是否为空）
    - 历史遗留的无 system 会话（早期 Web 保存的文件）：头部补插；
      摘要游标随位移 +1 对齐（summarized_upto 数的是消息位置）

    调用时机（S2 验收修复轮#4 补）：①服务启动（assemble 内）②归档清空后
    （Web _archive_current / CLI /new）——第二场会话起 clear 会把 system 一并
    清掉，而本函数只在启动跑一次的话，新会话=裸会话（真实复踩：英文回复
    再现）。幂等，多处调用无副作用。
    """
    if not session.messages:
        session.messages.append(Message(role="system", content=SYSTEM_PROMPT))
    elif session.messages[0].role != "system":
        session.messages.insert(0, Message(role="system", content=SYSTEM_PROMPT))
        if session.summarized_upto:
            session.summarized_upto += 1


def assemble(provider: str) -> AppContext:
    """按 provider 组装全部依赖，返回 CLI / Web 共用的运行上下文。

    副作用：读 .env、同步知识库、载入会话、拉起 MCP 子进程——都是「让依赖
    可用」的必要动作，随组装一起发生。
    """
    # 把项目根目录 .env 里的配置（API key 等）加载进环境变量
    load_dotenv()

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
    restored = len(session.messages)
    ensure_persona(session)   # 人设是装配不变量：CLI/Web 两个入口都必须带着开工
    if restored:
        print(f"已恢复 {restored} 条历史消息（{MEMORY_PATH}）")

    # 5) 工具（M5）：登记内置工具，交给主循环
    #    P1-2：依赖打包成 ToolContext——kb 给 search/write 查重检索、
    #    llm 给 search_and_summarize 做内部摘要（内部链，不穿语义档——评审第 2 条）、
    #    history 给会话内检索
    #    （闭包注入，传列表对象本身而非副本——run_turn 原地 append，
    #    工具才能实时看到全部历史）、notes_dir 消灭工具层写死的路径
    # 5.5) 联网工具（2026-09-16）：有 TAVILY_API_KEY 才建 client、才上菜单
    #      （条件注册，与 kb=None 同语义——mock 路径不背联网依赖）。
    #      key 纪律同 .env 其余条目：代码只读环境变量
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    web_client = TavilySearch(tavily_key) if tavily_key else None
    registry = ToolRegistry()
    ctx = ToolContext(
        notes_dir=NOTES_DIR,
        kb=kb,
        llm=internal_llm,
        history=session.messages,
        web=web_client,
    )
    register_builtin(registry, ctx)
    register_web_tools(registry, ctx)

    # 6) MCP 外部工具（MCP-config 配置化）：改 mcp_servers.json 加工具，零代码。
    #      命令型穿 stdio、URL 型穿 streamable HTTP；单台失败只警告不阻断；
    #      MCP_SERVERS 环境变量可指向个人配置（带 API key 的那种，不进仓库）
    try:
        specs = load_server_specs(Path(os.environ.get("MCP_SERVERS", "mcp_servers.json")))
    except ValueError as e:
        print(f"MCP 配置读取失败，本轮无外部工具：{e}")
        specs = []
    mcp_clients = assemble_servers(registry, specs)
    print(f"已装载工具：{', '.join(registry.names())}")

    return AppContext(
        provider=provider,
        ledger=ledger,
        embedder=embedder,
        llm=llm,
        internal_llm=internal_llm,
        kb=kb,
        session=session,
        registry=registry,
        mcp_clients=mcp_clients,
    )
