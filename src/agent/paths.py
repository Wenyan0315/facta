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
from __future__ import annotations

import os
from pathlib import Path

NOTES_DIR = Path("data/notes")   # 知识库笔记目录（语料资产，进 git）
LEARNED_DIR = Path("data/learned")   # M6.4 记忆固化目录（项目级记忆资产，进 git；用户级记忆另行住仓库外）
SESSIONS_DIR = Path("data/memory/sessions")   # S8a 会话仓库：所有会话同住这里，身份=文件名（`%Y%m%d-%H%M%S.json`）；S1~S7 时期它是「归档仓库」，老归档文件名本就是合法 id，原地即完成迁移
# P0-3（038）Run checkpoint 账本目录：每会话一个 append-only JSONL（`{sid}.jsonl`），
# 记「点了什么菜（intent）/ 回了什么结果（result）」。与 session.json 的分工——
# 底片真值源是 session.json，账本只存底片表达不了的东西：执行前意图（区分
# 「没跑过」vs「跑了但结果丢了」）与结果全文（healing 时原样回注，不截断）。
# 运行时数据，不进 git（同 audit/）。
CHECKPOINT_DIR = Path("data/checkpoints")

# M6.5 用户级记忆默认位置：仓库外单文件（~ 锚定绝对路径，与仓库内相对路径族
# 不同列）。跨项目共享、不进任何 git——M6.4 红线「位置没建好前不开桶」的
# 兑现：用户级信息（偏好/习惯/行程）从「一律不记」变「分流到这里」。
_USER_MEMORY_DEFAULT = Path.home() / ".personal-agent" / "user.md"


def user_memory_path() -> Path:
    """用户级记忆位置（默认值唯一真值源在此；CORTEX_USER_MEMORY 环境变量
    可覆写——测试 tmp_path 隔离的注入点，S6a WORKSPACE_ROOT 注入化同款）。

    函数而非常量：消费方三处（assemble / CLI 退出复盘 / Web 归档），
    覆写逻辑跟着真值源走，不散三份（P1「evals 各带副本漂移」的老病）。
    """
    return Path(os.environ.get("CORTEX_USER_MEMORY", str(_USER_MEMORY_DEFAULT)))

# S4 文件工具的 workspace 围栏根：项目仓库根（src/agent/paths.py 上两级）。
# 用 __file__ 锚定而非 Path.cwd()——已知边界「依赖从仓库根启动」就治了一半：
# 无论从哪个目录启动，agent 只碰得到本项目的文件（S4a 裁定：workspace=项目根）
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]

# S6a worktree 沙箱根：子 agent 的文件改动隔离区（运行时数据，gitignore 排除）。
# 放仓库内 data/ 下（与 memory/audit 同区）；files.py 黑名单挡住——否则
# search_code 的 rglob 会扫进 worktree 造成同文件双重命中
WORKTREES_DIR = WORKSPACE_ROOT / "data" / "worktrees"

# S7a 知识图谱落盘位置：notes 的结构化投影——文本可读、可审查、可 diff，
# 知识资产进 git（与 vector_db 二进制缓存相反的判断）
GRAPH_PATH = Path("data/graph.json")
