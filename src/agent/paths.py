"""全项目路径常量的唯一真值源（P1-3 补强，2026-09-10 code review）。

P1-3 先把 "data/notes" 收口到 __main__，但 evals 与 demo 是离线脚本、
不走 ToolContext，各自又带一份字面量副本——architecture.md 记录的不变量
「__main__ 与 evals 共用同一 loader → 评估与线上永远同一份语料」
就悬在「几处副本恰好没漂移」上。本模块让所有消费者 import 同一个常量。

规则：跨模块共享的路径常量住这里；只被一个模块用的路径（如 MEMORY_PATH
只归 __main__）留在消费者本地，不提前搬家。

已知边界：仍是相对路径（依赖从仓库根目录启动），换 cwd 启动的问题
已知且另行处理，不在本次范围。
"""

from pathlib import Path

NOTES_DIR = Path("data/notes")   # 知识库笔记目录（语料资产，进 git）
LEARNED_DIR = Path("data/learned")   # M6.4 记忆固化目录（项目级记忆资产，进 git；用户级记忆另行住仓库外）
