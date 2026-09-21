"""组装层：assemble —— 把各层依赖按 provider 组装成一个可运行的 AppContext。

单一真值源（S2a）：CLI（__main__）与未来的 Web 入口都调这里，不在各自
入口重复装配——否则 Web 入口一加，就是第二个「组装真值源」（P1 治过的病）。

provider 从参数进、不碰 sys.argv：sys.argv 是 CLI 的衣服，Web 的 provider
可能来自环境变量或默认值。谁解析 provider 谁决定，assemble 只管装配。

日志：内核库一律 logging.getLogger(__name__)，CLI 保留 print（用户界面）。
Web 模式由 server/app.py 配置 logging，CLI 由 __main__.py 配置。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.core.audit import AuditLog
from agent.core.gateway import SemanticCacheLLM
from agent.core.jev import JevClient, ScenarioRouter
from agent.core.llm import LLM, get_llm
from agent.core.telemetry import UsageLedger
from agent.core.types import Message
from agent.knowledge.knowledge_base import KnowledgeBase, get_embedder
from agent.knowledge.sync import sync_notes
from agent.knowledge.vector_store import ChromaVectorStore
from agent.memory.store import Session, load_session
from agent.memory.todos import TodoStore
from agent.orchestrator.agent import Agent, build_default_agent
from agent.paths import LEARNED_DIR, NOTES_DIR
from agent.tools.builtin import register_builtin
from agent.tools.context import ToolContext

logger = logging.getLogger(__name__)

from agent.tools.files import register_file_tools  # noqa: E402  # 历史结构：logger 居中，保持原样
from agent.tools.mcp_config import assemble_servers, load_server_specs
from agent.tools.plan import register_plan_tools
from agent.tools.registry import ToolRegistry
from agent.tools.spawn import register_spawn_tools
from agent.tools.terminal import register_terminal_tools
from agent.tools.todo import register_todo_tools
from agent.tools.web import get_web_search, register_web_tools

# 组装层唯一真值源：CLI / Web 都从这里拿路径，不在各自入口重定义
MEMORY_PATH = Path("data/memory/session.json")   # M6：会话记忆落盘位置（无工具用，不进 ctx）
VECTOR_DB_DIR = Path("data/vector_db")           # M7：向量库落盘位置（运行时数据，.gitignore 已排除）
TODOS_PATH = Path("data/todos.json")             # 个人待办（2026-09-17）：跨会话资产，独立于 session
AUDIT_DIR = Path("data/audit")                   # S3 审计日志（2026-09-17）：工具调用 append-only jsonl 按天滚动


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
    agent: Agent        # 主 agent（S5a）：行为定义收口——prompt/菜单/预算/learned 快照
    todos: TodoStore    # 个人待办仓库（2026-09-17）：工具与 Web API 共用同一实例
    mcp_clients: list = field(default_factory=list)   # 最终退出时统一 close，不留孤儿进程


def ensure_persona(session: Session, agent: Agent) -> None:
    """人设保证（装配不变量，S2 验收修复轮）：会话必须带着 agent 的 system_prompt 开工。

    「空会话种人设」原本只住在 CLI 壳——Web 入口曾跑过无人设会话（真实使用
    踩中：语言漂移、信息政策失效、自我认知靠模型编）。两分支：
    - 空会话：种人设（与 cli.py 的守卫幂等——双方都判 messages 是否为空）
    - 历史遗留的无 system 会话（早期 Web 保存的文件）：头部补插；
      摘要游标随位移 +1 对齐（summarized_upto 数的是消息位置）

    调用时机（S2 验收修复轮#4 补）：①服务启动（assemble 内，S5a 起挪到
    agent 构建后——人设来自 agent.system_prompt，而 agent 要等 registry
    装完；不变量语义不变）②归档清空后（Web _archive_current / CLI /new）
    ③切回换血后（_switch_session）——清空/换血动作发生在运行时，本函数
    只在启动跑一次的话，新会话=裸会话（真实复踩：英文回复再现）。
    幂等，多处调用无副作用。

    自愈（浏览器验收补）：头部连续多条 system（换血 bug 时期的残留）合并为
    一条——保留第一条，删其余；摘要游标随删除数左移。
    """
    # 自愈：合并头部重复 system（历史残留数据修复，游标对齐）
    if session.messages and session.messages[0].role == "system":
        dup = 0
        for m in session.messages[1:]:
            if m.role == "system":
                dup += 1
            else:
                break
        if dup:
            del session.messages[1 : 1 + dup]
            if session.summarized_upto:
                session.summarized_upto = max(1, session.summarized_upto - dup)

    if not session.messages:
        session.messages.append(Message(role="system", content=agent.system_prompt))
    elif session.messages[0].role != "system":
        session.messages.insert(0, Message(role="system", content=agent.system_prompt))
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
    logger.info("当前模型：%s", provider)

    # 3) 知识库（M7）：组装 embedder + store，索引走增量同步——
    #    只为真正新增/修改的笔记花 embedding 的钱；改过的自动删旧块重建
    if provider in ("mock", "echo", "repeat"):
        kb = KnowledgeBase(embedder)
    else:
        kb = KnowledgeBase(embedder, ChromaVectorStore(VECTOR_DB_DIR))
    report = sync_notes(kb, NOTES_DIR)
    logger.info("知识库同步：新增 %s / 删除 %s / 不变 %s", report.added, report.removed, report.unchanged)

    # 4) 会话记忆（M6）：启动时载入【完整会话状态】——底片(消息) + 压缩缓存(摘要游标)
    #    关键细节：必须在登记工具之前载入——search_history 的闭包要抓这个列表对象
    session = load_session(MEMORY_PATH)
    restored = len(session.messages)
    # （S5a）人设种入挪到 agent 构建后：ensure_persona 需要 agent.system_prompt，
    # 而 agent 要等 registry 装完——顺序：载入 → 工具 → MCP → agent → 人设。
    # 不变量语义不变（启动时装一次）；restored 口径反而更准（纯载入条数）
    if restored:
        logger.info("已恢复 %s 条历史消息（%s）", restored, MEMORY_PATH)

    # 5) 工具（M5）：登记内置工具，交给主循环
    #    P1-2：依赖打包成 ToolContext——kb 给 search/write 查重检索、
    #    llm 给 search_and_summarize 做内部摘要（内部链，不穿语义档——评审第 2 条）、
    #    history 给会话内检索
    #    （闭包注入，传列表对象本身而非副本——run_turn 原地 append，
    #    工具才能实时看到全部历史）、notes_dir 消灭工具层写死的路径
    # 5.5) 联网工具（2026-09-16）：工厂选搜索 Provider（BOCHA 优先/TAVILY 兜底），
    #      有 key 才上菜单（条件注册，与 kb=None 同语义——mock 路径不背联网依赖）
    web_client = get_web_search()
    if web_client is not None:
        logger.info("联网搜索：%s", web_client.name)
    todos = TodoStore(TODOS_PATH)   # 待办仓库：无外部依赖，恒构造（工具+API 共用）
    audit = AuditLog(AUDIT_DIR)     # S3 审计：registry 收口注入——所有工具调用自动落审
    registry = ToolRegistry(audit=audit)
    ctx = ToolContext(
        notes_dir=NOTES_DIR,
        kb=kb,
        llm=internal_llm,
        history=session.messages,
        web=web_client,
        todos=todos,
        session=session,   # S5b：计划工具的操作载体（传 Session 对象本身，与 history 同款身份契约）
    )
    register_builtin(registry, ctx)
    register_file_tools(registry)   # S4a 文件四件：恒注册（workspace 围栏即安全边界）
    register_terminal_tools(registry)   # S4b 终端执行：L2 确认缝裁决，白名单只读免确认
    register_web_tools(registry, ctx)
    register_todo_tools(registry, todos)
    register_plan_tools(registry, ctx)   # S5b 计划三件：恒注册（无外部依赖）
    register_spawn_tools(registry, ctx)   # S5c 子 agent 分派：ctx.llm 在即注册（内部链）

    # 6) MCP 外部工具（MCP-config 配置化）：改 mcp_servers.json 加工具，零代码。
    #      命令型穿 stdio、URL 型穿 streamable HTTP；单台失败只警告不阻断；
    #      MCP_SERVERS 环境变量可指向个人配置（带 API key 的那种，不进仓库）
    try:
        specs = load_server_specs(Path(os.environ.get("MCP_SERVERS", "mcp_servers.json")))
    except ValueError as e:
        logger.warning("MCP 配置读取失败，本轮无外部工具：%s", e)
        specs = []
    mcp_clients = assemble_servers(registry, specs)
    logger.info("已装载工具：%s", ", ".join(registry.names()))

    # 7) Agent 对象（S5a）：主 agent = 默认全量工具 + learned 快照注入
    #    （AGENTS.md 式：装配时读盘一次拼 prompt 尾部，会话中途固化不热刷新）。
    #    人设保证（装配不变量）随 agent 到位：CLI/Web 两个入口都带着开工
    #    M10 场景路由（条件装配，同 kb=None 不注册 notes 工具的模式）：
    #    JEV_API_KEY 缺席（CI/其他 clone 者）→ 不挂 router，行为与 v0.57
    #    逐字节一致；假模型路径（mock/echo/repeat）同样不挂——教学组合不背
    #    外部依赖。运行时故障（状态B）与熔断（状态C）不在这里：router 内部
    #    fail-open，装配期只管「有没有」
    router = None
    jev_key = os.environ.get("JEV_API_KEY")
    if jev_key and provider not in ("mock", "echo", "repeat"):
        router = ScenarioRouter(
            JevClient(
                api_key=jev_key,
                base_url=os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai"),
            ),
            tool_names=registry.names(),
            ledger=ledger,
        )
        logger.info("已启用 Jev 场景路由（工具 %d 个）", len(registry.names()))
    else:
        logger.info("未启用 Jev 场景路由，走 LLM 原生路径")
    agent = build_default_agent(registry, LEARNED_DIR, router=router)
    ensure_persona(session, agent)

    return AppContext(
        provider=provider,
        ledger=ledger,
        embedder=embedder,
        llm=llm,
        internal_llm=internal_llm,
        kb=kb,
        session=session,
        registry=registry,
        agent=agent,
        todos=todos,
        mcp_clients=mcp_clients,
    )
