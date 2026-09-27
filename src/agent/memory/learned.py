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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# 053 provenance：tag 词表是**闭集**，且单份真值（下面的正则由这几个常量拼出来，
# 加 tag 只改常量）。三个成员各回答一件事：
#   [已验证]   有客观背书（P0-7）——注入可见
#   [手改]     人碰过（本案）——注入可见，唯一产生点是 update_line
#   [固化:sid] 哪次会话固化出来的（本案）——只落盘，给人排查
# 闭集的理由：宽进（任意 `[…]` 都当 tag）会**静默吃字**——`- [2026-09-13] [TODO]
# 修一下` 里的 [TODO] 不在可见白名单，被剥成 tag 就等于从注入 prompt 里删掉了
# 模型该看见的一段文字。词表外的方括号一律留在正文。
VISIBLE_TAGS = ("[已验证]", "[手改]")
HAND_EDITED_TAG = "[手改]"
ORIGIN_TAG_PREFIX = "固化"

# 行格式：- [YYYY-MM-DD] [tag] [tag] 内容（consolidate._append 的落盘契约）。
# tag 组可选（053）：P0-7 之前的存量行没有 tag，照旧解析、零迁移。
# 三个捕获组 = 日期 / tag 串 / 纯正文——正文不含 tag，tag 单独成字段，
# 否则「注入时剥掉某个 tag」就只能靠字符串切割（两处真值，会漂）。
_TAG_NAMES = "|".join(re.escape(t[1:-1]) for t in VISIBLE_TAGS)
_TAG_PATTERN = rf"\[(?:{_TAG_NAMES}|{ORIGIN_TAG_PREFIX}:[^\]]*)\]"
_LINE_RE = re.compile(rf"^- \[(\d{{4}}-\d{{2}}-\d{{2}})\] ((?:{_TAG_PATTERN} )*)(.*)$")
_TAG_RE = re.compile(_TAG_PATTERN)


def origin_tag(sid: str) -> str:
    """会话 id → 行内来源 tag（consolidate._append 的写侧唯一入口）。"""
    return f"[{ORIGIN_TAG_PREFIX}:{sid}]"


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
    content: str       # 纯正文（不含 tag）；坏行为原行全文（前端按 date 区分渲染）
    tags: tuple[str, ...] = ()   # 行内 tag（053）；老行与坏行为空


def format_line(date: str, tags: Sequence[str], content: str) -> str:
    """(日期, tags, 正文) → 落盘行。

    写侧（consolidate._append）、编辑侧（update_line）、渲染侧（render）
    共用这一份——三处各拼各的就是漂移的开始（043「单一真值源」纪律）。
    """
    return f"- [{date}] {''.join(f'{t} ' for t in tags)}{content}"


def visible_text(entry: LearnedLine) -> str:
    """可见 tag + 正文（去重保序）。记忆面板的 `content` 字段就是它，PUT 原样回传。

    去重不是洁癖：consolidate 的 `_load_known` 把原行喂给内部 LLM 当「已知记忆」
    材料，模型可能把 tag 抄进 content，于是同一行落盘时 tag 出现两次。抄进来的
    那次会在下轮读盘时被 _LINE_RE 的 tag 组重新解析（结构自愈），但不去重就会
    一轮多一个、把注入 prompt 越滚越长。
    """
    prefix = "".join(f"{t} " for t in dict.fromkeys(entry.tags) if t in VISIBLE_TAGS)
    return f"{prefix}{entry.content}"


def render(entry: LearnedLine) -> str:
    """给模型看的一行：落盘格式，但只带 VISIBLE_TAGS。

    注入 prompt（agent._learned_block / _user_memory_block）与 MCP 召回
    （servers/memory_server.py）原来各自复制粘贴同一份表达式，收口在这里。
    坏行原样（既有宽容语义：手写行不因为不合规就消失）。
    """
    return entry.content if entry.date is None else f"- [{entry.date}] {visible_text(entry)}"


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
            result.append(LearnedLine(
                line=i, date=m.group(1), content=m.group(3),
                tags=tuple(_TAG_RE.findall(m.group(2))),
            ))
        else:
            result.append(LearnedLine(line=i, date=None, content=raw))
    return result


def _rewrite(path: Path, lines: list[str]) -> None:
    """行数组整重写（splitlines 丢了尾换行，统一补回；空文件写空串）。"""
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _split_tags(text: str) -> tuple[list[str], str]:
    """剥出开头的 tag 串：`[已验证] 正文` → (["[已验证]"], "正文")。

    面板回传的 content 就是 visible_text 给的「可见 tag + 正文」，编辑侧得
    把两者分开，才知道哪些 tag 是用户留下的、哪些要从磁盘原行补回。
    只认**词表内**的 tag（_TAG_RE，与 _LINE_RE 的 tag 组同一份正则）+ 后随
    空格：词表外的方括号（`[TODO] 修一下`）留在正文，否则它会被剥成 tag，
    而 tag 里不在 VISIBLE_TAGS 的部分注入时会被滤掉——等于静默吃掉用户写的字。
    收尾就是裸 tag、没有正文的情况也当正文处理——宽进，不因为格式怪就丢内容。
    """
    tags: list[str] = []
    while (m := _TAG_RE.match(text)) and text[m.end() :].startswith(" "):
        tags.append(m.group())
        text = text[m.end() + 1 :]
    return tags, text


def update_line(path: Path, line: int, content: str) -> None:
    """编辑一行：好行保留原日期与原 tags 并打 [手改]，坏行原样替换。

    053：这里是「人工改过」唯一的产生点。改完之后这一行必须与程序固化的
    行可区分，否则召回侧无从判断可信度（裁定三：只标人工改过）。tag 取舍：
    - 可见 tag 以回传为准——用户能在面板里删掉 [已验证]，这是 021「能改」
      的既有语义，不能因为加了 provenance 就悄悄收回
    - 不可见 tag（[固化:sid]）从磁盘原行补回——用户在面板里看不见它也就
      改不着它，来源记录不因为一次人工编辑而丢失
    - [手改] 强制在场且只出现一次（dict.fromkeys 去重保序）
    line 越界抛 IndexError（API 层转 404）。
    """
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        m = _LINE_RE.match(lines[line])
        if m:
            incoming, body = _split_tags(content)
            kept = [t for t in _TAG_RE.findall(m.group(2)) if t not in VISIBLE_TAGS]
            tags = list(dict.fromkeys([*incoming, HAND_EDITED_TAG, *kept]))
            lines[line] = format_line(m.group(1), tags, body)
        else:
            lines[line] = content
        _rewrite(path, lines)


def delete_line(path: Path, line: int) -> None:
    """删除一行（越界抛 IndexError）。删完的空文件保留（固化 append 的目标位）。"""
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        del lines[line]
        _rewrite(path, lines)
