"""Web 壳层（S2b）。

server/ 是最外层壳：把内核 run_turn 与装配 assemble 接上 HTTP。
分层向：server → orchestrator（内核/装配）→ memory/knowledge/tools → core。
"""
