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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from facta.core.audit import AuditLog
from facta.core.gateway import SemanticCacheLLM
from facta.core.jev import JevClient, ScenarioRouter
from facta.core.llm import LLM, get_llm
from facta.core.telemetry import UsageLedger
from facta.core.types import Message
from facta.knowledge.extract import sync_graph
from facta.knowledge.graph import GraphStore
from facta.knowledge.knowledge_base import (
    EMBED_PROVIDERS,
    KnowledgeBase,
    configured_embed_provider,
    get_embedder,
)
from facta.knowledge.sync import sync_notes
from facta.knowledge.vector_store import ChromaVectorStore
from facta.memory.consolidate import consolidate
from facta.memory.store import Session, SessionStore, derive_title
from facta.memory.title import summarize_title
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import DEFAULT_SYSTEM_PROMPT, Agent, build_default_agent
from facta.paths import (
    GRAPH_PATH,
    LEARNED_DIR,
    NOTES_DIR,
    SESSIONS_DIR,
    WORKSPACE_ROOT,
    user_memory_path,
)
from facta.tools.builtin import register_builtin
from facta.tools.context import ToolContext
from facta.tools.graph import register_graph_tools

logger = logging.getLogger(__name__)

from facta.tools.files import register_file_tools  # noqa: E402  # 历史结构：logger 居中，保持原样
from facta.tools.history import register_history_tools  # noqa: E402
from facta.tools.mcp_config import assemble_servers, load_server_specs
from facta.tools.plan import register_plan_tools
from facta.tools.registry import ToolRegistry
from facta.tools.spawn import register_spawn_tools
from facta.tools.terminal import register_terminal_tools
from facta.tools.todo import register_todo_tools
from facta.tools.web import get_web_search, register_web_tools
from facta.tools.worktree import cleanup_stale_worktrees

# 组装层唯一真值源：CLI / Web 都从这里拿路径，不在各自入口重定义
# 四个都锚 WORKSPACE_ROOT（S8a 边界①收口同款）：换 cwd 启动时相对路径会静默
# 指错——迁移源找不到（老会话「消失」）、审计/待办/向量库写到别处
MEMORY_PATH = WORKSPACE_ROOT / "data/memory/session.json"   # S8a 退役为「一次性迁移源」：老 active 固定位，启动时 move 进 SESSIONS_DIR
VECTOR_DB_DIR = WORKSPACE_ROOT / "data/vector_db"           # M7：向量库落盘位置（运行时数据，.gitignore 已排除）
TODOS_PATH = WORKSPACE_ROOT / "data/todos.json"             # 个人待办（2026-09-17）：跨会话资产，独立于 session
AUDIT_DIR = WORKSPACE_ROOT / "data/audit"                   # S3 审计日志（2026-09-17）：工具调用 append-only jsonl 按天滚动

# 增量固化阈值（S8a）：距上次固化攒够这么多条消息才跑一次复盘。
# 老口径是「归档/退出时全量固化一次」——S8a 没有归档动作了，触发点必须换成
# 「攒够就固化」，否则一段长对话的记忆永远不落 learned/。
CONSOLIDATE_THRESHOLD = max(1, int(os.environ.get("FACTA_CONSOLIDATE_THRESHOLD", "20")))


def _rag_missing_reason(provider: str) -> str | None:
    """语义 RAG 是否需要降级及原因（P1-1 评审修复）：None=不降级。

    教学组合（mock/echo/repeat）本来就词袋，不算「降级」——返回 None，
    走不走 Chroma 由调用方按 provider 另判。真模型只查两件事：
    所选 embedding 供应商的 key 在不在、chromadb 装没装（[rag] extra）。
    ADR 070：供应商不再写死硅基——FACTA_EMBED_PROVIDER 选谁，就查谁的 key。
    """
    if provider in ("mock", "echo", "repeat"):
        return None
    embed_name = configured_embed_provider()
    cfg = EMBED_PROVIDERS.get(embed_name)
    if cfg is None:
        return f"未知的 FACTA_EMBED_PROVIDER: {embed_name}（可选：{', '.join(EMBED_PROVIDERS)}）"
    key_name = f"{cfg['prefix']}_API_KEY"
    if not os.environ.get(key_name):
        return f"缺 {key_name}"
    try:
        import chromadb  # noqa: F401  # 探依赖：缺了降级，别让用户崩在 ChromaVectorStore.__init__
    except ImportError:
        return "未安装 [rag] 依赖（chromadb）"
    return None


@dataclass
class AppContext:
    """assemble 的产物：一套完整的运行依赖，CLI / Web 共用。

    S8a 起不再持有单数的 session / agent / registry —— 多会话并发要求
    「一段对话一套 agent」。取而代之的是 store（会话仓库，纯磁盘）+
    build_agent（per-session agent 工厂）。全局资源（kb/todos/graph/llm/
    MCP 子进程）仍是单例，被工厂造的每个 agent 共享。
    """

    provider: str
    ledger: UsageLedger
    embedder: Any
    llm: LLM            # 用户链（带语义档），服务用户聊天流量
    internal_llm: LLM   # 内部链（无语义档），给压缩器/工具内调用
    kb: KnowledgeBase
    store: SessionStore   # S8a：会话仓库（身份=文件名，见 memory/store.py 头注记）
    # S8a：per-session agent 工厂。契约：给一个 Session，还一个【已保证带人设】
    # 的 Agent（ensure_persona 在工厂内部执行，调用方无处可忘）。
    # 副作用：会往空会话里种 system 消息 —— 那是人设不变量本身，不是意外。
    build_agent: Callable[[Session], Agent]
    todos: TodoStore    # 个人待办仓库（2026-09-17）：工具与 Web API 共用同一实例
    mcp_clients: list = field(default_factory=list)   # 最终退出时统一 close，不留孤儿进程
    graph: GraphStore = field(default_factory=GraphStore)   # S7a/S7b 知识图谱（活对象：查询原语与图表面板共用最新图）


def ensure_persona(session: Session, agent: Agent) -> None:
    """人设保证（装配不变量，S2 验收修复轮）：会话必须带着 agent 的 system_prompt 开工。

    「空会话种人设」原本只住在 CLI 壳——Web 入口曾跑过无人设会话（真实使用
    踩中：语言漂移、信息政策失效、自我认知靠模型编）。三分支：
    - 空会话：种人设（与 cli.py 的守卫幂等——双方都判 messages 是否为空）
    - 历史遗留的无 system 会话（早期 Web 保存的文件）：头部补插；
      摘要游标随位移 +1 对齐（summarized_upto 数的是消息位置）
    - 有 system 但内容过期（P1-4 评审修复）：就地刷新为 agent.system_prompt。
      messages[0] 的 system 是【人设 + learned/用户记忆快照】的冻结副本，
      不是历史存档真值——记忆面板新增/编辑/删除后，旧快照若不刷新，旧会话
      仍把被删的记忆发给模型（评审实测：删了照样进 payload）。Web 每轮
      build_agent 重读盘，刷新后下一轮即生效；CLI 固化发生在收尾，下次
      进程自然拿新快照。替换只动 content 不动位置：summarized_upto（位置
      计数）与压缩器/账本（都只碰尾部）不受影响。

    调用时机（S8a 收口）：只有一个——build_agent 工厂内部。
    S8a 之前有三个调用点（服务启动 / 归档清空后 / 切回换血后），漏一个就是
    裸会话（真实复踩过：英文回复再现）。现在 agent 与人设同时诞生，
    「有 agent 但没人设」这个状态在结构上不存在了。
    幂等，重复调用无副作用（CLI 壳里的老守卫可安全并存）。

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
    elif session.messages[0].content != agent.system_prompt:
        # P1-4：快照过期 → 刷新。不把历史存档里的 system 当当前记忆真值。
        session.messages[0].content = agent.system_prompt


def settle_session(
    session: Session,
    sid: str,
    store: SessionStore,
    internal_llm: LLM,
    *,
    flush: bool = False,
) -> str:
    """收尾一段对话的 LLM 重活：补标题 → 增量固化 → 窄写落盘。返回固化报告。

    **保底落盘不在本函数里（ADR 076）**：对话本体的 save 必须发生在
    异步化的关键路径上（Web worker finally 在 run.finish 之前；CLI 在
    /new 换新之前同步做）——本函数跑在终态之后，此刻同会话下一轮可能
    已进场 append，任何全量 save 都会把新消息盖掉。P1-5 的教训
    「可失败的标题/固化不得拖死对话保存」由此升格为分工：保底归主轴，
    重活归后台。调用方契约：进场前盘上必须已有本轮对话。

    游标语义（P2-7）：consolidate 返回 (report, ok)，ok=False（坏 JSON 等
    可重试失败）不推进 consolidated_upto，下轮重烧同一批——宁可重复萃取，
    不可静默丢记忆。游标只活在磁盘上：推进后由终态窄写落盘兜住。

    调用时机（ADR 076 起改为异步旁路）：Web 在 worker 的 finally 里、但已
    挪到 run.finish **之后**——终态推送与准入释放不再等固化；CLI 在
    /new 换新/退出的后台线程里跑，退出前 join。终态落盘是窄写合并
    （store.update）：title 只在盘上仍为 None 时填（用户 rename 优先）；
    游标单调推进且仅当前缀共享（len(fresh) ≥ 目标）时才推，否则
    不动——下轮重烧，重复优于跳过（P2-7 语义）。
    flush=True → 阈值降到 1：没有「下一轮」了，把剩下的全冲掉。
    """

    # 标题（每段对话只提炼一次）：手工名优先——title 非空说明用户 rename 过，
    # 不用 LLM 顶掉（「自动生成用于填空，不覆盖用户主动编辑」，计划名/记忆标签同此原则）
    if session.title is None and any(m.role == "user" for m in session.messages):
        try:
            session.title = summarize_title(session, internal_llm) or derive_title(session)
        except Exception:
            logger.warning("标题提炼失败，退回首句", exc_info=True)
            session.title = derive_title(session)

    since = session.consolidated_upto
    report = "记忆固化：未达阈值，跳过复盘"
    if len(session.messages) - since >= (1 if flush else CONSOLIDATE_THRESHOLD):
        try:
            report, ok = consolidate(
                session,
                internal_llm,
                LEARNED_DIR,
                since=since,
                user_memory_path=user_memory_path(),
                # ADR 045：基础 prompt 当冗余对照物（memory 层不能反向 import
                # orchestrator——agent.py 已 import consolidate，会循环）
                base_prompt=DEFAULT_SYSTEM_PROMPT,
                # ADR 053：sid 本来就在作用域里，往下传一行 → 落盘行带 [固化:{sid}]
                sid=sid,
            )
        except Exception as exc:
            ok, report = False, f"记忆固化：异常中断（{exc}），游标未推进，下轮重试"
        # ok=False（含异常）不推进：失败就下轮重来，宁可重复萃取也不丢记忆
        if ok:
            session.consolidated_upto = len(session.messages)

    # ② 终态：窄写合并（ADR 076）——title 与游标落到盘上【最新】副本，
    # messages 的所有权在轮次 worker 手里，这里绝不整包回写
    def _merge(fresh: Session) -> None:
        if fresh.title is None and session.title is not None:
            fresh.title = session.title   # 用户 rename 过（非 None）就不动
        target = session.consolidated_upto
        if target > fresh.consolidated_upto and len(fresh.messages) >= target:
            # 前缀共享（下一轮只 append）才安全推进；fresh 已被压缩截短
            # → 位置语义撕裂 → 不动，下轮重烧
            fresh.consolidated_upto = target

    store.update(sid, _merge)
    return report


def assemble(provider: str) -> AppContext:
    """按 provider 组装全部依赖，返回 CLI / Web 共用的运行上下文。

    副作用：读 .env、同步知识库、载入会话、拉起 MCP 子进程——都是「让依赖
    可用」的必要动作，随组装一起发生。
    """
    # 把项目根目录 .env 里的配置（API key 等）加载进环境变量
    load_dotenv()

    # -1) worktree 残留回收（S6a）：上次进程被杀可能留下半截沙箱——启动即清
    #     （「先杀进程再删文件」血案同款防线：不动运行中的，只清启动前的）
    stale = cleanup_stale_worktrees()
    if stale:
        logger.info("已回收 %s 个残留 worktree（上次进程未正常退出）", stale)

    # 0) 账本（M7.5）：全进程一本账，LLM 与 embedding 都往里记，退出时打印
    ledger = UsageLedger()

    # 1) embedder：语义缓存与知识库共用一个（记账只注入这一处）
    #    练习模式（假模型）走词袋（离线不花一分钱）；真模型走所选 embedding
    #    供应商（ADR 070 前写死硅基 BGE-M3，现由 FACTA_EMBED_PROVIDER 决定）。
    #    注意两种 embedder 向量维度不同（词袋=词表长度、BGE=1024），绝不能混用
    #    同一个 Chroma 集合——所以教学组合根本不碰 Chroma，各自住各自的店
    #    P1-1 评审修复：真模型不再无条件要求 embedding key——缺 key 或缺
    #    chromadb 时降级词袋 + 内存库（教学组合同款路径）。语义 RAG 是检索
    #    增益，不该挡住主聊天；README「最低只要一个 LLM key」由此兑现。
    rag_missing = _rag_missing_reason(provider)
    embedder = (
        get_embedder("bow", ledger)
        if provider in ("mock", "echo", "repeat") or rag_missing
        else get_embedder(configured_embed_provider(), ledger)
    )
    if rag_missing:
        logger.info("语义 RAG 降级词袋（%s）：仅影响检索质量，不影响对话", rag_missing)

    # 2) 模型链（M7.5 网关 + 三方评审第 2 条拆链）：组装出两条链——
    #    内部链（internal_llm）：防护壳全套（记账/重试/精确缓存/熔断/降级），
    #        给压缩器与 search_and_summarize 的内部调用用；
    #    用户链（llm）：内部链再包一层语义档，只服务用户聊天流量——
    #        内部调用的（提示词, 回复）进缓存池有串味路径，且内部 prompt
    #        几乎不可能命中 0.92 阈值（白付 embed）
    internal_llm = get_llm(provider, ledger)
    # 语义缓存默认关闭（评审修复轮）：它只比最后一条 user 消息，忽略历史/
    # system/计划——「继续」在不同任务里含义完全不同，外部评审探针实证了
    # 跨上下文串味；M10 direct 路由把纯聊天送进 tools=None 命中区后风险
    # 被进一步放大。先保证「回答的是当前任务」，再谈省调用。
    # 精确缓存（完整输入哈希）不受影响仍在 RobustLLM 内生效。
    # FACTA_SEMANTIC_CACHE=1 显式开启（无状态 FAQ 场景）
    llm: LLM = internal_llm   # 标注基类：if/else 两分支类型不同，mypy 不自动合并
    if os.environ.get("FACTA_SEMANTIC_CACHE"):
        llm = SemanticCacheLLM(internal_llm, embedder, ledger)
        logger.info("语义缓存：已开启（实验性，注意跨上下文串味风险）")
    logger.info("当前模型：%s", provider)

    # 3) 知识库（M7）：组装 embedder + store，索引走增量同步——
    #    只为真正新增/修改的笔记花 embedding 的钱；改过的自动删旧块重建
    #    P1-1 同款：降级路径用内存库（词袋维度与既有 Chroma 的 BGE 集合不兼容，
    #    降级时绝不打开磁盘库——补齐 key/依赖后自动回到增量路径，旧向量仍在）
    if rag_missing or provider in ("mock", "echo", "repeat"):
        kb = KnowledgeBase(embedder)   # 教学组合 / 降级路径：内存库，不碰 Chroma
    else:
        kb = KnowledgeBase(embedder, ChromaVectorStore(VECTOR_DB_DIR))
    report = sync_notes(kb, NOTES_DIR)
    logger.info("知识库同步：新增 %s / 删除 %s / 不变 %s", report.added, report.removed, report.unchanged)

    # 3.5) 知识图谱（S7a）：notes 的结构化投影——向量管模糊相似，图管精确关系。
    #      指纹差集增量：笔记没改不重抽（省 LLM 的钱）；教学路径（假模型）
    #      不抽不记指纹（切真模型自动补抽）。graph.json 是知识资产进 git
    #      ——只在真有变更时落盘（extracted/removed>0），避免每次启动把
    #      git 工作区弄脏
    graph = GraphStore.load(GRAPH_PATH)
    graph_llm = None if provider in ("mock", "echo", "repeat") else internal_llm
    g_report = sync_graph(graph, NOTES_DIR, graph_llm)
    logger.info(
        "图谱同步：抽取 %s / 不变 %s / 删除 %s / 跳过 %s / 失败 %s",
        g_report.extracted, g_report.unchanged, g_report.removed, g_report.skipped, g_report.failed,
    )
    if g_report.extracted or g_report.removed:
        graph.save(GRAPH_PATH)

    # 4) 会话仓库（S8a）：启动时不再载入「那一个」会话——身份=文件名，
    #    载入动作下沉到真正要用它的时候（Web：worker 每轮进场 load、出场 save；
    #    CLI：启动时 load 一次，进程内常驻）。
    #    一次性迁移：老布局的 active 固定位搬进仓库（老归档文件名本就是合法 id，
    #    原地不动即完成迁移，历史清单一条不丢）
    store = SessionStore(SESSIONS_DIR)
    migrated = store.migrate_legacy_active(MEMORY_PATH)
    if migrated:
        logger.info("已迁移老 active 会话 → sessions/%s.json", migrated)

    # 5) 母 registry（S8a）：只装【全局资源】工具——锚 notes_dir / kb / todos /
    #    graph / web / workspace_root 的那些，与「正在聊哪一段」无关，
    #    全进程一份，被所有会话的 agent 共享（同一批 Tool 对象，闭包锚的资源随之共享）。
    #    母 ctx 的 session / history 恒为 None：历史两件与计划三件靠既有的条件注册
    #    惯例自然缺席（history.py:86 / plan.py:45），spawn 两件则干脆不在这里调
    #    register_spawn_tools —— 于是「母 registry 里没有会话绑定工具」是结构事实，
    #    不靠一份需要人肉维护的 skip 名单维持。
    # 5.5) 联网工具（2026-09-16）：工厂选搜索 Provider（BOCHA 优先/TAVILY 兜底），
    #      有 key 才上菜单（条件注册，与 kb=None 同语义——mock 路径不背联网依赖）
    web_client = get_web_search()
    if web_client is not None:
        logger.info("联网搜索：%s", web_client.name)
    todos = TodoStore(TODOS_PATH)   # 待办仓库：无外部依赖，恒构造（工具+API 共用）
    audit = AuditLog(AUDIT_DIR)     # S3 审计：registry 收口注入——所有工具调用自动落审
    mother = ToolRegistry(audit=audit)
    mother_ctx = ToolContext(
        notes_dir=NOTES_DIR,
        kb=kb,
        llm=internal_llm,
        web=web_client,
        todos=todos,
        workspace_root=WORKSPACE_ROOT,   # S6a：主 agent 文件/终端锚点（子 agent 由 spawn 覆盖为 worktree）
        graph=graph,   # S7a：知识图谱（活对象注入——查询原语读最新图）
    )
    register_builtin(mother, mother_ctx)   # time + notes（history 两件因 ctx.history is None 自跳）
    register_file_tools(mother, mother_ctx)   # S4a 文件四件：恒注册（S6a 起锚点随 ctx 注入）
    register_terminal_tools(mother, mother_ctx)   # S4b 终端执行：L2 确认缝裁决，白名单只读免确认
    register_web_tools(mother, mother_ctx)
    register_todo_tools(mother, todos)
    register_graph_tools(mother, mother_ctx)   # S7a 图谱查询：ctx.graph 在即注册（图空时工具如实报空）

    # 6) MCP 外部工具（MCP-config 配置化）：改 mcp_servers.json 加工具，零代码。
    #      命令型穿 stdio、URL 型穿 streamable HTTP；单台失败只警告不阻断；
    #      MCP_SERVERS 环境变量可指向个人配置（带 API key 的那种，不进仓库）
    #      装进母 registry —— 子进程全进程一套，per-session registry 搬运同一批 Tool 对象
    #      默认值锚 WORKSPACE_ROOT（S8a 边界①同款）：相对 cwd 时换目录启动会让
    #      FileNotFoundError 走 load_server_specs 的「空清单」分支——MCP 工具静默
    #      全消失，不崩不报错，最难查
    try:
        specs = load_server_specs(
            Path(os.environ.get("MCP_SERVERS", str(WORKSPACE_ROOT / "mcp_servers.json")))
        )
    except ValueError as e:
        logger.warning("MCP 配置读取失败，本轮无外部工具：%s", e)
        specs = []
    mcp_clients = assemble_servers(mother, specs)
    logger.info("全局工具（%d 个）：%s", len(mother.names()), ", ".join(mother.names()))

    def session_registry(session: Session) -> ToolRegistry:
        """per-session registry = 母 registry 全套搬运 + 会话绑定七件重注册。

        搬运：`mother.get(name)` 返回的是 Tool 对象引用，同一个对象注册进多个
        registry 是 registry.py 明确支持的语义（闭包锚定的 kb/todos/graph/
        MCP client 随之共享，子进程不重启）。
        重注册：七件（history 二 + plan 三 + spawn 二）的闭包必须锚【本会话】的
        messages 列表与 plan 棋盘 —— List identity trap：传对象本身，绝不 copy，
        否则 run_turn 原地 append 后工具安静变瞎。
        母 registry 里根本没有这七个名字，所以搬运不会覆盖它们（无需 skip 名单）。
        """
        sub = ToolRegistry(audit=audit)   # 审计同源（S3 单一必经点不分叉）
        for name in mother.names():
            tool = mother.get(name)
            if tool is not None:
                sub.register(tool)
        sctx = replace(mother_ctx, session=session, history=session.messages)
        register_history_tools(sub, sctx)
        register_plan_tools(sub, sctx)   # S5b 计划三件：恒注册（无外部依赖）
        register_spawn_tools(sub, sctx)   # S5c 子 agent 分派：ctx.llm 在即注册（内部链）
        return sub

    # 7) Agent 工厂（S5a → S8a）：agent = 完整工具菜单 + learned 快照注入
    #    （AGENTS.md 式：构建时读盘一次拼 prompt 尾部，会话中途固化不热刷新）。
    #    M10 场景路由（条件装配，同 kb=None 不注册 notes 工具的模式）：
    #    JEV_API_KEY 缺席（CI/其他 clone 者）→ 不挂 router，行为与 v0.57
    #    逐字节一致；假模型路径（mock/echo/repeat）同样不挂——教学组合不背
    #    外部依赖。运行时故障（状态B）与熔断（状态C）不在这里：router 内部
    #    fail-open，装配期只管「有没有」
    #
    #    鸡生蛋：router 要的是【完整菜单】的说明书（少了计划三件，模型就不知道
    #    自己能规划），而完整 registry 得先有一个会话才造得出来。解法是拿一个
    #    空 Session 探一份 probe registry，只取 tool_descriptions()，永不执行。
    #    router 本身与 session 无关（它只挑工具名），全进程一份被所有 agent 共享。
    router = None
    jev_key = os.environ.get("JEV_API_KEY")
    if jev_key and provider not in ("mock", "echo", "repeat"):
        probe = session_registry(Session())
        router = ScenarioRouter(
            JevClient(
                api_key=jev_key,
                base_url=os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai/v1/systemone"),
            ),
            tools=probe.tool_descriptions(),   # dict：名字→说明书（Jev 的 choice criteria 原料）
            ledger=ledger,
        )
        logger.info("已启用 Jev 场景路由（工具 %d 个）", len(probe.names()))
    else:
        logger.info("未启用 Jev 场景路由，走 LLM 原生路径")

    # M6.5 用户级记忆：快照注入主 agent prompt 尾（子 agent 不注入——
    # 执行器不是陪伴者，spawn.py 头注记）；位置经 paths.user_memory_path()
    #（env 可覆写，测试注入点）。文件不存在=空收敛，无注入零开销
    umem = user_memory_path()

    def build_agent(session: Session) -> Agent:
        """给一个会话造一个专属 agent，并保证它带着人设开工。

        ensure_persona 收口在这里（S8a）：agent 与人设同时诞生，
        「有 agent 但没人设」在结构上不存在——不用靠三个调用点各自记得。
        """
        agent = build_default_agent(
            session_registry(session), LEARNED_DIR, router=router, user_memory_path=umem
        )
        ensure_persona(session, agent)
        return agent

    return AppContext(
        provider=provider,
        ledger=ledger,
        embedder=embedder,
        llm=llm,
        internal_llm=internal_llm,
        kb=kb,
        store=store,
        build_agent=build_agent,
        todos=todos,
        mcp_clients=mcp_clients,
        graph=graph,   # S7b：图谱挂上 AppContext——surface 面板端点在 server 层读
    )
