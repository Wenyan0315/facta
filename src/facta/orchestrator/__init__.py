"""编排层（S2a 分层正名）。

分层向视图：
- core/        地基——谁都不认识，只提供最通用能力（LLM 接口/类型/向量数学）
- memory/ knowledge/ tools/  中层——只认识 core
- 本层          顶层——编排，认识所有人

loop.py 是内核（run_turn：跑一轮对话，I/O 全走缝）；
cli.py 是壳（run_chat：input/print/退出词表，把内核接上终端）。
S6 多 agent 编排时，本层长成真正的 orchestrator（coordinator.py 编排多个 loop）。
"""
