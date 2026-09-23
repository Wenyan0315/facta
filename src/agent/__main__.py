"""个人 AI Agent 助手 —— 命令行入口。

用法：
    python -m agent          # 默认用 deepseek 真模型
    python -m agent mock     # 用 mock 假模型（不花钱、不联网）
    python -m agent echo     # 其他测试模型（只读第一个参数）
"""

import logging
import sys

from agent.cli import EXIT_NEW, run_chat
from agent.memory.consolidate import consolidate
from agent.memory.plan import PlanBoard
from agent.memory.store import archive_session, derive_title, save_session
from agent.memory.title import summarize_title
from agent.orchestrator.assemble import MEMORY_PATH, assemble, ensure_persona
from agent.paths import LEARNED_DIR, SESSIONS_DIR, user_memory_path

VERSION = "0.9.0"   # 与 pyproject [project].version 保持一致（版本号单一语义，改动时同步两处）


def main() -> None:
    # CLI 入口：配置 logging——内核库的 logger.info 在此可见（Web 由 server/app.py 配）
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"Personal Agent v{VERSION}")

    # 命令行第一个参数 = 用哪个模型，不传默认 deepseek-flash
    # （M10 主力切换：bench 题库轨道综合 86% 第一 + ECE 0.042 最佳；
    #  python -m agent deepseek 可切回 chat 档，供应商表另有 siliconflow）
    provider = sys.argv[1] if len(sys.argv) > 1 else "deepseek-flash"

    # 组装依赖（单一真值源 S2a）：账本/embedder/双链/知识库/会话/工具/MCP
    # 全在 assemble 里，本入口只解析 provider 再拿结果
    ctx = assemble(provider)
    session = ctx.session
    llm, agent, internal_llm = ctx.llm, ctx.agent, ctx.internal_llm

    # 多会话主循环（S1）：run_chat 归还 (会话, 退出原因)。
    #    quit/interrupt → 收官；new → 先存后清再开一轮。
    #    拆链（评审第 2 条）：压缩器内部调用走 summary_llm=内部链
    #    MCP 客户端只在最终退出时关闭——多会话循环期间关了，下一轮工具全死
    try:
        while True:
            loaded_len = len(session.messages)   # M6.4 复盘起点（每轮重取：/new 后新会话从 0 起）
            session, reason = run_chat(llm, agent, session, summary_llm=internal_llm)

            # S2 验收修复轮：归档前提炼标题（写进 session.title）——save 落盘、
            # archive 复制都带它，列表读取零 LLM 调用。提炼失败 fallback 首句派生，
            # 不阻断归档。quit（不归档）不提炼，省一次 LLM 调用。
            if reason == EXIT_NEW:
                session.title = summarize_title(session, internal_llm) or derive_title(session)

            # 退出落盘（M6）：完整会话状态（消息 + 压缩缓存 + 标题）存回 JSON
            save_session(session, MEMORY_PATH)
            print(f"对话历史已保存：{len(session.messages)} 条 → {MEMORY_PATH}")

            # M6.4 记忆固化 + M6.5 用户级分流：退出复盘——since=本轮启动消息数，
            # 无新对话（启动即退出）→ consolidate 内部直接跳过
            print(consolidate(
                session, internal_llm, LEARNED_DIR,
                since=loaded_len, user_memory_path=user_memory_path(),
            ))

            if reason != EXIT_NEW:
                break

            # S1 先存后清：save（上一行）→ 归档成功 → 才清内存 → 写新 active。
            # 归档失败会抛异常中止，旧对话仍在 session.json，什么都没丢
            archived = archive_session(MEMORY_PATH, SESSIONS_DIR)
            title = session.title or derive_title(session)   # 清空前先取标签（clear 后 session 已空）
            # 原地清、绝不 rebind：search_history 工具的闭包抓的是 session.messages
            # 这个列表对象本身（列表身份陷阱的反面教材），rebind 会让工具失明
            session.messages.clear()
            session.summary = None
            session.summarized_upto = 1
            session.title = None
            session.plan = PlanBoard()   # 评审修复轮：与 Web 同修——旧计划不泄进新会话
            ensure_persona(session, agent)   # clear 连 system 一起清——补种，新 active 落盘即带人设（与 Web 同款修复）
            save_session(session, MEMORY_PATH)   # active 立即反映为新空会话
            print(f"已归档「{title}」→ {archived.name}，新会话开始")
    finally:
        # MCP-b/r：无论正常退出还是异常崩掉，都关掉所有工具服务器——不留孤儿进程
        for client in ctx.mcp_clients:
            client.close()

    # 打印本次会话账单（M7.5）：钱花哪了，退出一目了然
    print(ctx.ledger.bill())


if __name__ == "__main__":
    main()
