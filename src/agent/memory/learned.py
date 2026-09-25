"""learned 记忆的读取与编辑原语（记忆面板 v1 的服务端，2026-09-19）。

固化管线（consolidate.py）是写入侧：萃取→审查→硬校验→append。
本模块是读取/编辑/删除侧——「护城河可视化」（021 定位）：用户能看、
能改、能删 agent 记住了什么，四段管线的产出不再是黑箱。

行号定位协议：GET 返回的 line = 文件 0-based 行号，原样传回 PUT/DELETE。
可行依据：固化 append-only（只加尾行），读写窗口内已有行号稳定。
写回用「原行数组整重写」保真——空行、手写行、注释原样保留。
已知边界：行号是 GET 时拍的快照，两个标签页并发互删仍会错位——
LEARNED_LOCK 能保证文件不被撕裂、固化 append 不被整重写抹掉，但保证
不了「你手里那个行号还指向那一行」（git 兜底恢复）。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

# 行格式：- [YYYY-MM-DD] 内容（consolidate._append 的落盘契约；P0-7 起
# verified 条目内容带 [已验证] 前缀——正则不挑食，前缀随内容组一起读出）
_LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.*)$")

# 记忆文件的进程级互斥（S8a）：写入侧（consolidate 的 append）与编辑侧
# （本模块的读改写整重写）共用同一批 LEARNED_DIR/*.md。整重写是「读全文
# → 改 → 覆盖」，不锁就会把窗口期内固化刚 append 的行连旧内容一起抹掉
# ——那是真丢记忆。多会话并发收官抬高了固化频率，v1 那句「单用户概率
# 极低」的前提已过期。
LEARNED_LOCK = threading.Lock()


@dataclass(frozen=True)
class LearnedLine:
    line: int          # 文件 0-based 行号（编辑/删除的定位键）
    date: str | None   # 解析出的日期；None = 手写/坏行（content 为原行全文）
    content: str       # formatted 行为正文；坏行为原行（前端按 date 区分渲染）


def read_learned(path: Path) -> list[LearnedLine]:
    """读一个类别文件，返回全部非空行。空行跳过显示但重写时按行号保留。"""
    if not path.is_file():
        return []
    result: list[LearnedLine] = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not raw.strip():
            continue
        m = _LINE_RE.match(raw)
        if m:
            result.append(LearnedLine(line=i, date=m.group(1), content=m.group(2)))
        else:
            result.append(LearnedLine(line=i, date=None, content=raw))
    return result


def _rewrite(path: Path, lines: list[str]) -> None:
    """行数组整重写（splitlines 丢了尾换行，统一补回；空文件写空串）。"""
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def update_line(path: Path, line: int, content: str) -> None:
    """编辑一行：好行保留原日期前缀（时间戳归程序管），坏行原样替换。

    line 越界抛 IndexError（API 层转 404）。
    """
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        m = _LINE_RE.match(lines[line])
        lines[line] = f"- [{m.group(1)}] {content}" if m else content
        _rewrite(path, lines)


def delete_line(path: Path, line: int) -> None:
    """删除一行（越界抛 IndexError）。删完的空文件保留（固化 append 的目标位）。"""
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        del lines[line]
        _rewrite(path, lines)
