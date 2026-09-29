"""个人 AI Agent 助手 —— 命令行入口。

用法：
    python -m facta          # 默认用 deepseek 真模型
    python -m facta mock     # 用 mock 假模型（不花钱、不联网）
    python -m facta echo     # 其他测试模型（只读第一个参数）
"""

import logging
import sys

from facta.cli import EXIT_NEW, run_chat
from facta.memory.store import Session
from facta.orchestrator.assemble import assemble, settle_session
from facta.orchestrator.checkpoint import heal, ledger_path, read_ledger

VERSION = "0.9.0"   # 与 pyproject [project].version 保持一致（版本号单一语义，改动时同步两处）


def main() -> None:
    # CLI 入口：配置 logging——内核库的 logger.info 在此可见（Web 由 server/app.py 配）
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"Facta v{VERSION}")

    # 命令行第一个参数 = 用哪个模型，不传默认 deepseek-flash
    # （M10 主力切换：bench 题库轨道综合 86% 第一 + ECE 0.042 最佳；
    #  python -m facta deepseek 可切回 chat 档，供应商表另有 siliconflow）
    provider = sys.argv[1] if len(sys.argv) > 1 else "deepseek-flash"

    # 组装依赖（单一真值源 S2a）：账本/embedder/双链/知识库/会话仓库/工具/MCP
    # 全在 assemble 里，本入口只解析 provider 再拿结果
    ctx = assemble(provider)

    # S8a：CLI 也住 sessions/ —— 启动接上最近聊过的那段（一段都没有就新开）。
    # 老口径的 active 固定位（MEMORY_PATH）已退役，assemble 里做一次性迁移。
    sid = ctx.store.latest() or ctx.store.create(Session())
    session = ctx.store.load(sid)
    agent = ctx.build_agent(session)   # 工厂保证人设；换新会话时跟着重造

    # P0-3（038）崩溃恢复：上次进程被杀可能在底片尾部留下「已点菜、结果没回填」的
    # 悬挂轮次——原样发给 API 直接 400。heal 按账本把每条补成合法 tool 消息
    # （有结果的原样回注，没结果的按幂等性告知模型能不能重做）。
    # 只在启动这一次做：/new 换的是全新空会话，没有残局可言。
    healed = heal(session, read_ledger(ledger_path(sid)), agent.registry)
    if healed:
        ctx.store.save(sid, session)
        print(f"[崩溃恢复] 上次中断遗留的 {healed} 条工具调用已补齐，可接着这段对话继续")

    # 多会话主循环（S1）：run_chat 归还 (会话, 退出原因)。
    #    quit/interrupt → 收官；new → 收官后另起一段。
    #    拆链（评审第 2 条）：压缩器内部调用走 summary_llm=内部链
    #    MCP 客户端只在最终退出时关闭——多会话循环期间关了，下一轮工具全死
    try:
        while True:
            session, reason = run_chat(ctx.llm, agent, session, summary_llm=ctx.internal_llm)

            # 收官三步（补标题 → 增量固化 → 落盘）与 Web worker 共用一份实现。
            # flush=True：退出与换新都没有「下一轮」了，阈值降到 1，剩下的全冲掉。
            print(settle_session(session, sid, ctx.store, ctx.internal_llm, flush=True))
            print(f"对话历史已保存：{len(session.messages)} 条 → sessions/{sid}.json")

            if reason != EXIT_NEW:
                break

            # /new 换新对象而非原地 clear —— agent 跟着重造，所以「绝不 rebind
            # session.messages」那条纪律在这里自然消解：老口径原地清是为了迁就
            # 常驻 agent 的闭包（search_history 抓的是列表对象本身，rebind 即失明），
            # 现在闭包与会话同生共死。计划板重置与人设补种同样不再需要——
            # 新 Session 天生空板，人设由工厂保证。
            print(f"「{session.title or '未命名'}」已收进会话清单，新会话开始")
            sid = ctx.store.create(Session())
            session = ctx.store.load(sid)
            agent = ctx.build_agent(session)
    finally:
        # MCP-b/r：无论正常退出还是异常崩掉，都关掉所有工具服务器——不留孤儿进程
        for client in ctx.mcp_clients:
            client.close()

    # 打印本次会话账单（M7.5）：钱花哪了，退出一目了然
    print(ctx.ledger.bill())


if __name__ == "__main__":
    main()
