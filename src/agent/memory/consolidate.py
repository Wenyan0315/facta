"""M6.4 记忆固化 + M6.5 用户级分流：把对话里「值得跨会话记住的东西」
按作用域沉淀到两个位置——项目级进 data/learned/（进 git），用户级进
仓库外 user.md（~/.personal-agent/，不进任何 git）。

M6.5 之前用户级信息「一律不记」是红线（仓库外位置没建，开桶=引导隐私
写进可能公开的 repo）——位置建成后红线升级为分流：能力跟着位置走。
阳澄湖行程类内容从此有合法出口（漏网实证见决策记录 032 执行记录）。

管线五段（M6.4 四段 + 分流）：
  ① 萃取   LLM 读「滚动摘要 + 尾窗」→ 候选条目（档案员不是评论员：
           只提炼对话明确说过的东西，禁补全禁推断），每条带 scope
  ② 审查   二次调用对照原文踢掉编造（critic 第一次值班：挂在不可逆写入前）；
           用户级条目从宽：证据不够直接即弃（跨项目影响所有会话，污染代价高）
  ③ 硬校验 程序管形状：作用域/类别白名单 / 条数上限 / 单条长度 / 敏感凭证
           禁令 / JSON 容错——信模型的部分是语义，不信的部分全都交给代码
  ④ 落盘   项目级 append 到 data/learned/{类别}.md；用户级 append 到
           user.md（同款行格式，读侧零翻译）；时间戳由程序加（不信模型）

v1 剪裁记录仍有效：长会话中途兜底→出现「会话后段记忆丢失」实测症状；
learned 入 RAG→注入成本越阈值（032 裁定二 v2 信号）；审查升级跨供应商
裁判→幻觉率实测 >0。
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agent.core.llm import LLM
from agent.core.types import Message
from agent.memory.store import Session

CATEGORIES = ("decisions", "constraints", "other")
SCOPES = ("project", "user")
MAX_ENTRIES_PER_RUN = 5      # 经验裁量非铁律：超过说明要么会话太长要么过滤失效，需要人审
MAX_CONTENT_LEN = 200        # 单条上限：条目是「一句事实」，不是段落
WINDOW = 12                  # 复盘窗口：摘要之外再带最近 12 条原文（与压缩同族参数）

# 敏感凭证禁令（M6.5，程序侧硬边界）：任何记忆（不分作用域）不得落盘的
# 模式。提示词也禁（语义引导），但代码才是边界——「信模型语义，不信模型纪律」。
_SENSITIVE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),            # API key 常见前缀（OpenAI/DeepSeek 系）
    re.compile(r"apikey_[A-Za-z0-9]{16,}"),        # Jev 系
    re.compile(r"(?:password|passwd|密码)\s*[:=：]\s*\S+", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{16,}"),
    re.compile(r"\d{17}[\dXx]"),                   # 身份证 18 位
    re.compile(r"\b(?:\d{3}-\d{4}-\d{4}|\d{11})\b"),  # 手机号（带分隔/裸 11 位）
)

EXTRACT_TEMPLATE = """你是个人 agent 的「记忆档案员」。下面是本会话的复盘材料。
你的任务：把对话中值得跨会话记住的信息提炼成记忆条目，并标注作用域。

项目级（scope=project，只提炼这三类）：
- decisions：项目相关的决定，以及做出该决定的理由
- constraints：项目的约束、教训、工程惯例
- other：不属于上面两类、但值得记住的项目硬事实

用户级（scope=user）：用户个人的偏好、习惯、称呼、行程安排、饮食起居等
——它们住仓库外的个人记忆库，跨项目生效，现在可以记（标 scope=user）。

铁律（违反任何一条的条目必须丢弃）：
1. 只提炼对话中明确出现过的内容，禁止补全、推断、联想、美化
2. 密码、API key、身份证号、手机号等敏感凭证一律不记（任何作用域都不记）
3. 每条只记一个事实，一句话说清，不超过 80 字
4. 三问过滤：跨会话还成立吗？以后大概率用得上吗？「已知记忆」里没记过吗？
   任一答案为否 → 丢弃
5. 最多 {max_entries} 条，宁缺毋滥

已知记忆（已有条目，不要再重复记；两段分别是项目桶与用户记忆）：
{known}

对话复盘材料：
{transcript}

只输出 JSON 数组，格式：
[{{"category": "constraints", "content": "...", "scope": "project"}}]
（scope 缺省视为 project；用户级条目 category 填 other 即可）"""

REVIEW_TEMPLATE = """你是记忆档案的「审查员」。下面是候选记忆条目和对应的对话复盘材料。
逐条对照：条目内容是否都能在复盘材料里找到明确依据？
- 找得到依据的保留；找不到的（编造/推断/过度概括）删除
- scope=user 的条目从宽处理：依据不够直接即删除（用户级记忆跨项目影响
  所有会话，污染代价远高于漏记）
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
    scope: str = "project"   # M6.5：user=仓库外个人记忆库；缺省 project（旧输出兼容）


def _transcript(session: Session, window: int) -> str:
    """复盘材料 = 滚动摘要 + 尾窗原文（与发送给模型的投影同构，但不带人设）。"""
    parts = []
    if session.summary:
        parts.append(f"【滚动摘要】{session.summary}")
    start = min(len(session.messages), session.summarized_upto)
    tail = session.messages[start:][-window:]
    parts.extend(f"{m.role}: {m.content}" for m in tail)
    return "\n".join(parts)


def _load_known(learned_dir: Path, user_memory_path: Path | None) -> str:
    """把已固化的全部条目读出来当「已知记忆」——写前比对的 v1 是提示词级。

    M6.5：两段拼装——项目桶 + 用户记忆（去重范围跨作用域：同一事实
    两边都记是双份噪音）。user_memory_path=None（未配置/测试）只读项目桶。
    """
    parts = []
    if learned_dir.is_dir():
        for path in sorted(learned_dir.glob("*.md")):
            parts.append(f"== {path.name} ==\n{path.read_text(encoding='utf-8')}")
    if user_memory_path is not None and user_memory_path.is_file():
        parts.append(f"== user.md（用户记忆）==\n{user_memory_path.read_text(encoding='utf-8')}")
    return "\n".join(parts) if parts else "（暂无）"


def _parse_json_array(text: str) -> tuple[list[dict], bool]:
    """容错解析：剥 ```json 围栏（模型的常见坏习惯），返回 (条目, 是否解析成功)。

    为什么返回元组而不是空列表一了百了：「模型明确输出 []」（档案员认为无话
    可说，正常）与「输出坏 JSON」（异常）是两种完全不同的状态，文案和后续
    动作都不同——把两者都折成空列表，就是冒烟测试里「档案员没有产出条目
    （或输出无法解析）」这句混淆文案的根因。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return [], False
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], True
    return [], False   # 解析成功但不是数组（如裸对象）：同样视为不可用


def _is_sensitive(content: str) -> bool:
    """敏感凭证检测（保守：宁误杀不漏放——11 位纯数字可能是订单号，
    但漏放一个手机号的代价远大于误杀一条硬事实）。"""
    return any(p.search(content) for p in _SENSITIVE_PATTERNS)


def _harden(items: list[dict]) -> list[Entry]:
    """程序侧硬校验：形状归代码管，语义才归提示词管。

    - 敏感凭证：弃（任何作用域——这是落盘前的最后一道闸）
    - scope 非法：归 project（写错位置的保守方向：用户级错进项目桶是
      分类噪音，反向是隐私泄漏）
    - category 白名单垃圾桶、批内去重：M6.4 原样
    """
    entries: list[Entry] = []
    seen: set[str] = set()
    for item in items[:MAX_ENTRIES_PER_RUN]:
        category = str(item.get("category", "other"))
        content = str(item.get("content", "")).strip()
        scope = str(item.get("scope", "project"))
        if category not in CATEGORIES:
            category = "other"   # 白名单垃圾桶：非法类别不炸管道，归 other
        if scope not in SCOPES:
            scope = "project"
        if not content or len(content) > MAX_CONTENT_LEN:
            continue
        if _is_sensitive(content):
            continue
        if content in seen:
            continue   # 批内去重（批间去重靠「已知记忆」提示词）
        seen.add(content)
        entries.append(Entry(category=category, content=content, scope=scope))
    return entries


def _append(entries: list[Entry], learned_dir: Path, user_memory_path: Path | None) -> list[str]:
    """按作用域落盘；时间戳由程序加——出处链条里程序是唯一可信作者。

    返回写入位置清单（如 ["decisions", "user"]）供文案汇报。
    user_memory_path=None 时的 user 条目已被上游过滤，这里不会再遇到。
    """
    written: list[str] = []
    learned_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    for entry in entries:
        if entry.scope == "user":
            assert user_memory_path is not None   # 上游过滤的契约（防御性）
            user_memory_path.parent.mkdir(parents=True, exist_ok=True)
            with open(user_memory_path, "a", encoding="utf-8") as f:
                f.write(f"- [{today}] {entry.content}\n")
            if "user" not in written:
                written.append("user")
        else:
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
    user_memory_path: Path | None = None,
) -> str:
    """退出复盘主入口。since = 本次启动时的消息数——无新对话则不白烧 LLM。

    user_memory_path（M6.5）：None=未配置用户级位置，user 条目照 v1 行为
    丢弃（防御默认；CLI/Web 装配层恒传 paths.user_memory_path()）。
    """
    if not any(m.role == "user" for m in session.messages[since:]):
        return "记忆固化：本轮无新对话，跳过复盘"

    transcript = _transcript(session, window)
    if not transcript.strip():
        return "记忆固化：无可复盘内容"

    extract_prompt = EXTRACT_TEMPLATE.format(
        max_entries=MAX_ENTRIES_PER_RUN,
        known=_load_known(learned_dir, user_memory_path),
        transcript=transcript,
    )
    raw, extract_ok = _parse_json_array(
        llm.generate([Message(role="user", content=extract_prompt)]).content
    )
    if not extract_ok:
        return "记忆固化：档案员输出无法解析（坏 JSON），未写入"
    if not raw:
        return "记忆固化：档案员明确表示无条目可沉淀，未写入"

    review_prompt = REVIEW_TEMPLATE.format(
        entries_json=json.dumps(raw, ensure_ascii=False),
        transcript=transcript,
    )
    kept, review_ok = _parse_json_array(
        llm.generate([Message(role="user", content=review_prompt)]).content
    )
    if not review_ok:
        return "记忆固化：审查输出无法解析（坏 JSON），未写入"
    entries = _harden(kept)

    # 未配置用户级位置：user 条目丢弃（v1 行为），文案如实说——不算驳回
    dropped_unplaced = 0
    if user_memory_path is None:
        dropped_unplaced = sum(1 for e in entries if e.scope == "user")
        entries = [e for e in entries if e.scope != "user"]

    if not entries:
        base = f"记忆固化：{len(raw)} 条候选全部被审查驳回（或未过硬校验），未写入"
        if dropped_unplaced:
            base += f"；另有 {dropped_unplaced} 条用户级候选因未配置位置丢弃"
        return base

    written = _append(entries, learned_dir, user_memory_path)
    n_user = sum(1 for e in entries if e.scope == "user")
    report = (
        f"记忆固化：新增 {len(entries)} 条（驳回 {len(raw) - len(entries)} 条）"
        f" → {', '.join(written)}"
    )
    if n_user:
        report += f"（其中用户级 {n_user} 条 → {user_memory_path}）"
    if dropped_unplaced:
        report += f"；{dropped_unplaced} 条用户级候选因未配置位置丢弃"
    return report
