"""learned 记忆的读取与编辑原语（记忆面板 v1 的服务端，2026-09-19）。

固化管线（consolidate.py）是写入侧：萃取→审查→硬校验→append。
本模块是读取/编辑/删除侧——「护城河可视化」（021 定位）：用户能看、
能改、能删 agent 记住了什么，四段管线的产出不再是黑箱。

定位协议：
- 旧路径用行号（line 字段，文件 0-based 行号）。固化 append-only（只加尾行），
  读写窗口内已有行号稳定；但两标签页并发互删仍会错位——LEARNED_LOCK
  能保证文件不被撕裂、固化 append 不被整重写抹掉，但保证不了「你手里
  那个行号还指向那一行」（git 兜底恢复）。评审挂信号叫它「记忆行号身份」。
- ADR 071：每行落盘时尾部追加 HTML 注释 `<!--id:xxx-->` 携带稳定 id。
  id 由 hashlib.sha256(date+content+序号) 截 8 字节生成，存活到内容被
  编辑或删除。新增 update_line_by_id / delete_line_by_id；原 line 版本
  保留向后兼容——面板 UI 不动，未来按需切换。前端一旦用 id，行号身份的
  并发错位就不再是问题（两边拿到同一个 id 就是同一行）。
"""

from __future__ import annotations

import hashlib
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
# ADR 071：稳定 id 注释。HTML 注释不被 Markdown / 文本渲染器输出，落到行尾
# 不影响人眼与 prompt 注入；老文件没这个注释 → read_learned 时 id=None。
_ID_COMMENT_RE = re.compile(r"<!--id:([0-9a-f]{8})-->\s*$")


def make_id(date: str, content: str, seq: int) -> str:
    """生成稳定 id（日期+正文+序号 → sha256 截 8 字节）。

    序号用于同秒同内容撞车（极小概率但理论存在，固化 append 极密时是窗口）。
    同 (date, content, seq) 同 id——同一行不论 append 多少轮 id 不变；
    内容改了 id 自然改（hash 输入变了）。
    """
    h = hashlib.sha256(f"{date}|{content}|{seq}".encode()).digest()
    return h[:4].hex()


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
    line: int          # 文件 0-based 行号（编辑/删除的定位键，向后兼容）
    date: str | None   # 解析出的日期；None = 手写/坏行（content 为原行全文）
    content: str       # 纯正文（不含 tag）；坏行为原行全文
    tags: tuple[str, ...] = ()   # 行内 tag（053）；老行与坏行为空
    id: str | None = None         # ADR 071：稳定 id；老文件/未启用=None


def format_line(date: str, tags: Sequence[str], content: str, id: str | None = None) -> str:
    """(日期, tags, 正文[, id]) → 落盘行。

    写侧（consolidate._append）、编辑侧（update_line）、渲染侧（render）
    共用这一份——三处各拼各的就是漂移的开始（043「单一真值源」纪律）。
    ADR 071：可选 id 落到 `<!--id:xxx-->` 尾注释；老管线不传 id 时不写注释，
    向后兼容。
    """
    line = f"- [{date}] {''.join(f'{t} ' for t in tags)}{content}"
    return f"{line}<!--id:{id}-->" if id else line


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
    """读一个类别文件，返回全部非空行。空行跳过显示但重写时按行号保留。

    ADR 071：解析 `<!--id:xxx-->` 尾注释填进 id 字段；解析失败（坏行或
    老文件）→ id=None。读到的行与磁盘原文可能不一致：尾注释剥出后写入
    update_line_by_id 仍能正确回填（format_line 自动加新 id 注释）。
    """
    if not path.is_file():
        return []
    result: list[LearnedLine] = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not raw.strip():
            continue
        m = _LINE_RE.match(raw)
        if m:
            # 剥尾注释：解析到的尾注释既属本行，不属于 content
            content_with_comment = m.group(3)
            cm = _ID_COMMENT_RE.search(content_with_comment)
            content = (content_with_comment[:cm.start()] if cm else content_with_comment).rstrip()
            result.append(LearnedLine(
                line=i, date=m.group(1), content=content,
                tags=tuple(_TAG_RE.findall(m.group(2))),
                id=cm.group(1) if cm else None,
            ))
        else:
            cm = _ID_COMMENT_RE.search(raw)
            content = (raw[:cm.start()] if cm else raw).rstrip()
            result.append(LearnedLine(line=i, date=None, content=content, id=cm.group(1) if cm else None))
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
    ADR 071：保留原 id 不变（content 改了 id 由 hash 输入重算，但用户看
    不见 id——稳定性对调用者不可见，对落盘可见）。
    """
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        m = _LINE_RE.match(lines[line])
        existing_id = None
        if m:
            cm = _ID_COMMENT_RE.search(m.group(3))
            existing_id = cm.group(1) if cm else None
        # 调内容后的 id：内容/日期变化都会改 hash。保留旧 id 仅当显式传 None 时
        new_id = existing_id  # 默认沿用——edit-by-line 不重算
        if m:
            incoming, body = _split_tags(content)
            kept = [t for t in _TAG_RE.findall(m.group(2)) if t not in VISIBLE_TAGS]
            tags = list(dict.fromkeys([*incoming, HAND_EDITED_TAG, *kept]))
            lines[line] = format_line(m.group(1), tags, body, id=new_id)
        else:
            lines[line] = content
        _rewrite(path, lines)


def delete_line(path: Path, line: int) -> None:
    """删除一行（越界抛 IndexError）。删完的空文件保留（固化 append 的目标位）。"""
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        del lines[line]
        _rewrite(path, lines)


def update_line_by_id(path: Path, line_id: str, content: str) -> None:
    """按稳定 id 编辑一行（ADR 071）。

    比 update_line 多一道「id 解析 → 转行号」的桥——并发互删场景下两个标签页
    拿到同一个 id 就是同一行，行号被前端读时是哪行不重要（不再错位）。
    id 不存在抛 KeyError（API 层转 404，与 line 版本越界 IndexError 区分）。
    """
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        target = None
        for i, raw in enumerate(lines):
            m = _LINE_RE.match(raw)
            cm = (_ID_COMMENT_RE.search(m.group(3)) if m else _ID_COMMENT_RE.search(raw))
            if cm and cm.group(1) == line_id:
                target = i
                break
        if target is None:
            raise KeyError(f"id 不存在：{line_id!r}")
        # 复用行版逻辑——反射调用 update_line 但需要把锁域扩大（避免死锁）
        # 这里直接内联简化版：行号已知 + 内容更新 + 保留 id
        m = _LINE_RE.match(lines[target])
        if m:
            incoming, body = _split_tags(content)
            kept = [t for t in _TAG_RE.findall(m.group(2)) if t not in VISIBLE_TAGS]
            tags = list(dict.fromkeys([*incoming, HAND_EDITED_TAG, *kept]))
            lines[target] = format_line(m.group(1), tags, body, id=line_id)
        else:
            lines[target] = content
        _rewrite(path, lines)


def delete_line_by_id(path: Path, line_id: str) -> None:
    """按稳定 id 删除一行（ADR 071）。id 不存在抛 KeyError。"""
    with LEARNED_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        target = None
        for i, raw in enumerate(lines):
            m = _LINE_RE.match(raw)
            cm = (_ID_COMMENT_RE.search(m.group(3)) if m else _ID_COMMENT_RE.search(raw))
            if cm and cm.group(1) == line_id:
                target = i
                break
        if target is None:
            raise KeyError(f"id 不存在：{line_id!r}")
        del lines[target]
        _rewrite(path, lines)
