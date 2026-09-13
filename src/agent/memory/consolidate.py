"""M6.4 记忆固化：把对话里「值得跨会话记住的东西」沉淀到 data/learned/。

补的是记忆层与知识层之间的真空区：此前这类信息只有三条不可靠出路——
底片淹没（子串检索）、滚动摘要（会话内、压缩稀释）、write_note（被动靠模型自觉）。

管线四段：
  ① 萃取   LLM 读「滚动摘要 + 尾窗」→ 候选条目（档案员不是评论员：
           只提炼对话明确说过的东西，禁补全禁推断）
  ② 审查   二次调用对照原文踢掉编造（critic 第一次值班：挂在不可逆写入前）
  ③ 硬校验 程序管形状：类别白名单 / 条数上限 / 单条长度 / JSON 容错——
           信模型的部分是语义，不信的部分全都交给代码
  ④ 落盘   append 到 data/learned/{类别}.md，时间戳由程序加（不信模型）

v1 三桶刻意不含 preferences：偏好绝大多数是「用户级」信息，而用户级
位置（仓库外）尚未实现——留着这个桶等于引导模型把个人隐私写进
可能公开的 repo（红线联动：能力跟着位置走，位置没建好前不开桶）。
categories 里每一桶都是「项目级」：decisions（决定与理由）、
constraints（项目约束与教训）、other（兜底，也是白名单的垃圾桶）。

剪裁记录（各自带触发信号）：用户级记忆→等仓库外位置实现；长会话
中途兜底→出现「会话后段记忆丢失」实测症状；learned 入 RAG→learned
文件数 >10 或需要跨会话检索时；审查升级跨供应商裁判→幻觉率实测 >0。
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agent.core.llm import LLM
from agent.core.types import Message
from agent.memory.store import Session

CATEGORIES = ("decisions", "constraints", "other")
MAX_ENTRIES_PER_RUN = 5      # 经验裁量非铁律：超过说明要么会话太长要么过滤失效，需要人审
MAX_CONTENT_LEN = 200        # 单条上限：条目是「一句事实」，不是段落
WINDOW = 12                  # 复盘窗口：摘要之外再带最近 12 条原文（与压缩同族参数）

EXTRACT_TEMPLATE = """你是个人 agent 的「记忆档案员」。下面是本会话的复盘材料。
你的任务：把对话中「值得跨会话记住的【项目级】信息」提炼成记忆条目。

只提炼这三类，其余一律不记：
- decisions：项目相关的决定，以及做出该决定的理由
- constraints：项目的约束、教训、工程惯例
- other：不属于上面两类、但值得记住的项目硬事实

铁律（违反任何一条的条目必须丢弃）：
1. 只提炼对话中明确出现过的内容，禁止补全、推断、联想、美化
2. 用户个人偏好、习惯、学习背景等【用户级】信息一律不记
   （它们属于另一个仓库外的记忆库，写进这里就是泄漏）
3. 每条只记一个事实，一句话说清，不超过 80 字
4. 三问过滤：跨会话还成立吗？以后大概率用得上吗？「已知记忆」里没记过吗？
   任一答案为否 → 丢弃
5. 最多 {max_entries} 条，宁缺毋滥

已知记忆（已有条目，不要再重复记）：
{known}

对话复盘材料：
{transcript}

只输出 JSON 数组，格式：[{{"category": "constraints", "content": "..."}}]"""

REVIEW_TEMPLATE = """你是记忆档案的「审查员」。下面是候选记忆条目和对应的对话复盘材料。
逐条对照：条目内容是否都能在复盘材料里找到明确依据？
- 找得到依据的保留；找不到的（编造/推断/过度概括/用户级信息）删除
- 可以修正措辞使条目更忠实于原文，但禁止添加原文没有的信息
- 重复的条目只留一条

候选条目：
{entries_json}

对话复盘材料：
{transcript}

只输出审查后保留的 JSON 数组（同格式）。一条都不留就输出 []。"""


@dataclass
class Entry:
    category: str
    content: str


def _transcript(session: Session, window: int) -> str:
    """复盘材料 = 滚动摘要 + 尾窗原文（与发送给模型的投影同构，但不带人设）。"""
    parts = []
    if session.summary:
        parts.append(f"【滚动摘要】{session.summary}")
    start = session.summarized_upto if session.summarized_upto < len(session.messages) else len(session.messages)
    tail = session.messages[start:][-window:]
    parts.extend(f"{m.role}: {m.content}" for m in tail)
    return "\n".join(parts)


def _load_known(learned_dir: Path) -> str:
    """把已固化的全部条目读出来当「已知记忆」——写前比对的 v1 是提示词级。"""
    if not learned_dir.is_dir():
        return "（暂无）"
    parts = []
    for path in sorted(learned_dir.glob("*.md")):
        parts.append(f"== {path.name} ==\n{path.read_text(encoding='utf-8')}")
    return "\n".join(parts) if parts else "（暂无）"


def _parse_json_array(text: str) -> list[dict]:
    """容错解析：剥掉 ```json 围栏（模型的常见坏习惯），坏 JSON 返回空。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _harden(items: list[dict]) -> list[Entry]:
    """程序侧硬校验：形状归代码管，语义才归提示词管。"""
    entries: list[Entry] = []
    seen: set[str] = set()
    for item in items[:MAX_ENTRIES_PER_RUN]:
        category = str(item.get("category", "other"))
        content = str(item.get("content", "")).strip()
        if category not in CATEGORIES:
            category = "other"   # 白名单垃圾桶：非法类别不炸管道，归 other
        if not content or len(content) > MAX_CONTENT_LEN:
            continue
        if content in seen:
            continue   # 批内去重（批间去重靠「已知记忆」提示词）
        seen.add(content)
        entries.append(Entry(category=category, content=content))
    return entries


def _append(entries: list[Entry], learned_dir: Path) -> list[str]:
    """按类别 append 落盘；时间戳由程序加——出处链条里程序是唯一可信作者。"""
    learned_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    written: list[str] = []
    for entry in entries:
        path = learned_dir / f"{entry.category}.md"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- [{today}] {entry.content}\n")
        if entry.category not in written:
            written.append(entry.category)
    return written


def consolidate(
    session: Session,
    llm: LLM,
    learned_dir: Path,
    since: int = 0,
    window: int = WINDOW,
) -> str:
    """退出复盘主入口。since = 本次启动时的消息数——无新对话则不白烧 LLM。"""
    if not any(m.role == "user" for m in session.messages[since:]):
        return "记忆固化：本轮无新对话，跳过复盘"

    transcript = _transcript(session, window)
    if not transcript.strip():
        return "记忆固化：无可复盘内容"

    extract_prompt = EXTRACT_TEMPLATE.format(
        max_entries=MAX_ENTRIES_PER_RUN,
        known=_load_known(learned_dir),
        transcript=transcript,
    )
    raw = _parse_json_array(
        llm.generate([Message(role="user", content=extract_prompt)]).content
    )
    if not raw:
        return "记忆固化：档案员没有产出条目（或输出无法解析）"

    review_prompt = REVIEW_TEMPLATE.format(
        entries_json=json.dumps(raw, ensure_ascii=False),
        transcript=transcript,
    )
    kept = _parse_json_array(
        llm.generate([Message(role="user", content=review_prompt)]).content
    )
    entries = _harden(kept)

    if not entries:
        return f"记忆固化：{len(raw)} 条候选全部被审查驳回，未写入"

    categories = _append(entries, learned_dir)
    return (
        f"记忆固化：新增 {len(entries)} 条"
        f"（驳回 {len(raw) - len(entries)} 条）→ {', '.join(categories)}"
    )