"""M6.4 记忆固化 + M6.5 用户级分流：把对话里「值得跨会话记住的东西」
按作用域沉淀到两个位置——项目级进 data/learned/（进 git），用户级进
仓库外 user.md（~/.facta/，不进任何 git）。

M6.5 之前用户级信息「一律不记」是红线（仓库外位置没建，开桶=引导隐私
写进可能公开的 repo）——位置建成后红线升级为分流：能力跟着位置走。
阳澄湖行程类内容从此有合法出口（漏网实证见决策记录 032 执行记录）。

管线五段（M6.4 四段 + 分流）：
  ① 萃取   LLM 读「滚动摘要 + 尾窗」→ 候选条目（档案员不是评论员：
           只提炼对话明确说过的东西，禁补全禁推断），每条带 scope
  ② 审查   二次调用对照原文做枚举裁决 keep/drop/edit（ADR 064：改写必须显式
           声明并留 diff 痕迹，keep 取萃取原文不信回抄；critic 第一次值班：
           挂在不可逆写入前）；用户级条目从宽：证据不够直接即弃（跨项目影响
           所有会话，污染代价高）
  ③ 硬校验 程序管形状：作用域/类别白名单 / 条数上限 / 单条长度 / 敏感凭证
           禁令 / 易腐事实禁令（ADR 045）/ JSON 容错——信模型的部分是语义，
           不信的部分全都交给代码
  ④ 落盘   项目级 append 到 data/learned/{类别}.md；用户级 append 到
           user.md（同款行格式，读侧零翻译）；时间戳由程序加（不信模型）

v1 剪裁记录仍有效：长会话中途兜底→出现「会话后段记忆丢失」实测症状；
learned 入 RAG→注入成本越阈值（032 裁定二 v2 信号）；审查升级跨供应商
裁判→幻觉率实测 >0。
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from facta.core.llm import LLM
from facta.core.types import Message
from facta.memory.learned import (
    LEARNED_LOCK,
    LearnedLine,
    format_line,
    origin_tag,
    read_learned,
    render,
    set_statuses,
    sweep_tombstones,
)
from facta.memory.store import Session

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

# 易腐事实禁令（ADR 045，与敏感凭证同族的第二道代码闸门）：几天内就会失效、
# 且系统无机制发现（固化只追加、无校验、无过期）的形态。实证：other.md 记的
# 「run_turn 第 113 行、共 227 行」9 天后现实是第 340 行、共 501 行。
# 匹配保守（只拦明确形态，不拦裸数字）：「架构分三层」「窗口约 7–10 天」
# 「最多 5 条」都不含下列模式，不误杀。第一道闸门是提示词负面清单（带理由，
# 能泛化到未列举的形态），这里只兜住最常见的写法。
_PERISHABLE_PATTERNS = (
    re.compile(r"第\s*\d+\s*行"),                      # 行号
    re.compile(r"共\s*\d+\s*(?:行|篇|条|个|字|文件)"),   # 会随演进的计数
    # git 跟踪状态（真实写法是「尚未被 git 跟踪」，故容一个 git 词）。不含
    # 「不跟踪」：那会误杀「不跟踪用户位置」这类偏好条目
    re.compile(r"(?:未|已)(?:被)?(?:\s*git\s*)?跟踪"),
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
4. 三问过滤：跨会话还成立吗？以后大概率用得上吗？「已知记忆」（含基础人设与
   工具清单——那部分每轮都已注入，再记就是双份噪音）里没记过吗？
   任一答案为否 → 丢弃
5. 易腐事实不记：行号、文件行数、git 跟踪状态、「当前共 N 篇」这类会随演进的
   计数一律不记——它们几天就失效，而记忆只追加、不校验、不过期，入库后没有
   任何机制能发现，模型还会当真引用错答案。要记就只记稳定部分：文件路径、
   文件名、决定本身。同理「某时刻的待办/缺口/查询结果」也不记（价值随该时刻
   过去而归零，如「等 9/20 后再查」「某文件待补」）
6. 最多 {max_entries} 条，宁缺毋滥
7. 每条标注 verified（P0-7）：条目所依据的事实有客观背书——对话中出现
   测试通过、git 状态、工具验证结果等可复核证据 → true；纯口头结论、
   agent 自我总结的教训 → false

已知记忆（下面这些已经生效或已经记过，不要再重复记）：
{known}

对话复盘材料：
{transcript}

只输出 JSON 数组，格式：
[{{"id": "e1", "category": "constraints", "content": "...", "scope": "project", "verified": false}}]
（id 自编 e1/e2/… 递增，只作审查对照锚、无语义，缺省由程序按顺序补位；
scope 缺省视为 project；用户级条目 category 填 other 即可；verified 缺省视为 false）"""

REVIEW_TEMPLATE = """你是记忆档案的「审查员」。下面是候选记忆条目（每条带 id 对照锚）和
对应的对话复盘材料。逐条裁决，verdict 三选一：
- keep：条目内容在材料里有明确依据 → 保留原文
- drop：编造/推断/过度概括，或依据不足 → 删除
- edit：可以修正措辞使条目更忠实于原文（禁止添加原文没有的信息），
  必须给出改后全文——改写必须显式声明，不允许悄悄重写
- scope=user 的条目从宽处理：依据不够直接即 drop（用户级记忆跨项目影响
  所有会话，污染代价远高于漏记）
- 重复的条目只 keep 一条，其余 drop
- verified=true 的条目，其背书（测试通过/git 状态/工具验证结果）也必须
  能在材料中找到；找不到就在该条裁决上加 "verified": false（撤背书，
  审查只能撤不能补）——背书造假比条目失真更危险（P0-7）

候选条目：
{entries_json}

对话复盘材料：
{transcript}

只输出 JSON 数组，每条候选一个裁决，格式：
[{{"id": "e1", "verdict": "keep"}}, {{"id": "e2", "verdict": "edit", "content": "改后全文"}}]
（只有 edit 需要 content；一条都不留就输出 []；id 只能用候选清单里出现过的）"""

SUPERSEDE_TEMPLATE = """你是记忆库的「去重员」。下面是本批即将入库的新记忆条目，以及
该作用域下当前在用（active）的旧记忆条目（带序号）。

任务：判断哪些旧条目已被新条目**取代**（同一事实的更新版本），返回这些
旧条目的序号。取代判据：新旧讲的是同一件事，且新条目比旧条目更新、更准确
（日期/状态/事实已变化）。只是「相关」或「同主题」不算取代；拿不准一律
不取代（宁漏勿误删——误删会让已入库事实被标记废弃、退出召回）。

新条目：
{new_contents}

在用旧条目（序号. 内容）：
{active}

只输出 JSON 整数数组（如 [1, 3]），没有取代就输出 []。序号只能用旧条目清单里出现过的。"""


@dataclass
class Entry:
    category: str
    content: str
    scope: str = "project"   # M6.5：user=仓库外个人记忆库；缺省 project（旧输出兼容）
    verified: bool = False   # P0-7：有客观背书（测试通过/git 状态/工具验证）；缺省 false（保守）


def _transcript(session: Session, window: int) -> str:
    """复盘材料 = 滚动摘要 + 尾窗原文（与发送给模型的投影同构，但不带人设）。"""
    parts = []
    if session.summary:
        parts.append(f"【滚动摘要】{session.summary}")
    start = min(len(session.messages), session.summarized_upto)
    tail = session.messages[start:][-window:]
    parts.extend(f"{m.role}: {m.content}" for m in tail)
    return "\n".join(parts)


def _load_known(learned_dir: Path, user_memory_path: Path | None,
                base_prompt: str = "") -> str:
    """把已固化的全部条目读出来当「已知记忆」——写前比对的 v1 是提示词级。

    M6.5：两段拼装——项目桶 + 用户记忆（去重范围跨作用域：同一事实
    两边都记是双份噪音）。user_memory_path=None（未配置/测试）只读项目桶。

    base_prompt（ADR 045）：基础人设与工具清单也进对照材料。此前缺这段导致
    一类冗余在原理上查不出来——「与 system prompt 重复的条目」（如实证过的
    语义/逐字检索区分、历史压缩用 search_history）。由上层传入而非这里 import：
    agent.py 已 import 本模块（CATEGORIES），反向引用会循环 import 且违反分层
    （memory 是下层）。默认空 = 改动前行为。
    """
    parts = []
    if learned_dir.is_dir():
        for path in sorted(learned_dir.glob("*.md")):
            parts.append(f"== {path.name} ==\n{path.read_text(encoding='utf-8')}")
    if user_memory_path is not None and user_memory_path.is_file():
        parts.append(f"== user.md（用户记忆）==\n{user_memory_path.read_text(encoding='utf-8')}")
    if base_prompt.strip():
        parts.append("== 基础人设与工具清单（每轮已注入，不要再记）==\n" + base_prompt.strip())
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


def _is_perishable(content: str) -> bool:
    """易腐事实检测（ADR 045）。与敏感检测同款保守方向，但代价不同：
    敏感是隐私泄漏，易腐是**污染用户资产且不可逆**（固化只追加、无过期），
    且模型会当真引用错答案——消融首轮就有一题因腐化条目答错方向。"""
    return any(p.search(content) for p in _PERISHABLE_PATTERNS)


def _expire_perishable(learned_dir: Path, user_memory_path: Path | None) -> int:
    """出口过期（ADR 074 丁）：规则层扫在用条目命中 045 易腐正则 → 打 [已过期]。

    纯正则、零模型调用，与入口闸门复用同一份 _PERISHABLE_PATTERNS（单份真值）。
    read_learned 默认只回在用行，天然跳过已撤回/已过期/被取代的旧行（不重复打标）。
    """
    today = date.today().isoformat()
    paths: list[Path] = []
    if learned_dir.is_dir():
        paths.extend(sorted(learned_dir.glob("*.md")))
    if user_memory_path is not None and user_memory_path.is_file():
        paths.append(user_memory_path)
    total = 0
    for path in paths:
        changes = {e.line: "已过期" for e in read_learned(path) if _is_perishable(e.content)}
        if changes:
            set_statuses(path, today, changes)
            total += len(changes)
    return total


def _parse_int_list(text: str) -> list[int]:
    """把模型回的序号清单解析成整数列表。宽容（剥围栏 + 抓所有数字），
    非法文本 → 空列表（宁漏勿误删——序号在调用方再按范围过滤）。"""
    stripped = text.strip().removeprefix("```").removesuffix("```").strip()
    return [int(n) for n in re.findall(r"\d+", stripped)]


def _supersede_scope(
    new_contents: list[str],
    active: list[tuple[Path, LearnedLine]],
    llm: LLM,
    today: str,
) -> int:
    """单作用域去重（ADR 074 戊）：本批新条目 vs 该作用域在用旧条目（带序号），
    模型回要打 [被取代] 的旧条目序号。调用方保证 new_contents 与 active 均非空——
    ponytail: 有存量才触发一次模型调用，fresh 目录零调用省 token。"""
    prompt = SUPERSEDE_TEMPLATE.format(
        new_contents="\n".join(f"- {c}" for c in new_contents),
        active="\n".join(f"{i}. {e.content}" for i, (_, e) in enumerate(active)),
    )
    raw = llm.generate([Message(role="user", content=prompt)]).content
    idxs = [i for i in _parse_int_list(raw) if 0 <= i < len(active)]
    changes: dict[Path, dict[int, str]] = {}
    for i in idxs:
        path, entry = active[i]
        changes.setdefault(path, {})[entry.line] = "被取代"
    for path, per_path in changes.items():
        set_statuses(path, today, per_path)
    return sum(len(v) for v in changes.values())


def _supersede_duplicates(
    entries: list[Entry],
    learned_dir: Path,
    user_memory_path: Path | None,
    llm: LLM,
) -> int:
    """出口取代（ADR 074 戊）：按作用域分桶做语义去重。project 桶扫全部类别
    文件、user 桶扫 user.md，互不跨越（同一事实两边都记是双份噪音，入口闸门管）。"""
    today = date.today().isoformat()
    project_paths = sorted(learned_dir.glob("*.md")) if learned_dir.is_dir() else []
    user_paths = (
        [user_memory_path] if user_memory_path is not None and user_memory_path.is_file() else []
    )
    total = 0
    project_new = [e.content for e in entries if e.scope == "project"]
    if project_new:
        active = [(p, e) for p in project_paths for e in read_learned(p)]
        if active:
            total += _supersede_scope(project_new, active, llm, today)
    user_new = [e.content for e in entries if e.scope == "user"]
    if user_new:
        active = [(p, e) for p in user_paths for e in read_learned(p)]
        if active:
            total += _supersede_scope(user_new, active, llm, today)
    return total


def _mint_ids(items: list[dict]) -> tuple[list[dict], int]:
    """萃取条目 id 物化（ADR 064 ①）：「决定」先有身份才谈得上对账。

    模型给了唯一 id 就用模型的；缺省由程序按序补位（宽进——旧格式萃取
    输出零改动照跑，程序铸的锚同样确定）；重复 id 弃后到者（无法对账的
    保守方向，记数进报告）。id 只是管线内的对照锚，不落盘、不进 Entry。
    """
    seen: set[str] = set()
    out: list[dict] = []
    dup = 0
    for i, item in enumerate(items, start=1):
        eid = str(item.get("id", "")).strip()
        if eid and eid in seen:
            dup += 1
            continue
        if not eid:
            eid = f"e{i}"
            while eid in seen:   # 程序铸的锚撞上模型给的：加下划线让位
                eid += "_"
        seen.add(eid)
        out.append({**item, "id": eid})
    return out, dup


def _apply_verdicts(
    tagged: list[dict], review: list[dict]
) -> tuple[list[dict], list[dict], dict[str, int | bool]]:
    """枚举裁决对账（ADR 064 ②）：keep 取萃取原文（不信审查回抄——回抄本身
    就是一次再生成，可能漂移；id 物化的意义正在于能回去取原文）、drop 弃、
    edit 取审查改稿并留 diff；非法 verdict 归 drop（拍板 1 甲：审查连裁决
    都表述不清时，条目命运不该交给猜测）、编造 id 的裁决整案弃并记「审查
    幻觉」（枚举化白送的幻觉探针）、未获裁决的候选弃（审查没说 keep 就
    不替它留）。P0-7 语义保留：审查只能撤背书（"verified": false），不能补。

    旧格式宽进（拍板 2 甲）：审查输出缺 verdict 字段的条目清单＝现行行为
    照跑（按保留清单处理，含信任其回抄），报告标注「旧格式」。

    返回 (进入硬校验的条目, edit 留痕, 计数)。
    """
    counts: dict[str, int | bool] = {
        "old_format": False, "dropped": 0, "illegal": 0,
        "hallucinated": 0, "unruled": 0, "edit_no_content": 0,
    }
    if review and not any("verdict" in r for r in review):
        counts["old_format"] = True
        return review, [], counts
    by_id = {str(item.get("id")): item for item in tagged}
    kept: list[dict] = []
    edits: list[dict] = []
    ruled: set[str] = set()
    for r in review:
        rid = str(r.get("id", ""))
        src = by_id.get(rid)
        if src is None:
            counts["hallucinated"] += 1
            continue
        if rid in ruled:
            counts["illegal"] += 1   # 同一条的重复裁决：后到的算非法
            continue
        ruled.add(rid)
        demoted = {**src, "verified": src.get("verified") is True and r.get("verified") is not False}
        verdict = r.get("verdict")
        if verdict == "keep":
            kept.append(demoted)
        elif verdict == "drop":
            counts["dropped"] += 1
        elif verdict == "edit":
            content = str(r.get("content", "")).strip()
            if not content:
                counts["edit_no_content"] += 1   # edit 缺正文＝表述不清，保守归 drop
            else:
                kept.append({**demoted, "content": content})
                edits.append({"id": rid, "before": str(src.get("content", "")), "after": content})
        else:
            counts["illegal"] += 1
    counts["unruled"] = len(tagged) - len(ruled)
    return kept, edits, counts


def _log_edits(learned_dir: Path, edits: list[dict], sid: str) -> None:
    """edit 留痕 sidecar（ADR 064 拍板 3 甲）：learned_dir/.review.jsonl。

    append-only 事件日志——记「某时某刻审查对 eN 做了什么改写」，不承诺
    与当前 learned 内容一致（行被面板编辑/删除后对不上是预期，053 的
    .provenance.json 同语义：审计不因文件被改而失效）。落在 052 记忆写
    围栏内（程序写、伪造不了），gitignore 后 git archive 副本天然不带。
    """
    if not edits:
        return
    learned_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with LEARNED_LOCK, open(learned_dir / ".review.jsonl", "a", encoding="utf-8") as f:
        f.writelines(
            json.dumps({"ts": ts, "sid": sid, **e}, ensure_ascii=False) + "\n" for e in edits
        )


def _harden(items: list[dict]) -> tuple[list[Entry], list[Entry], list[Entry]]:
    """程序侧硬校验：形状归代码管，语义才归提示词管。

    - 敏感凭证：弃（任何作用域——这是落盘前的最后一道闸）
    - 易腐事实（ADR 045）：弃但**报出原文**——条目的稳定部分（如「run_turn 在
      loop.py」）可能值得保留，人看到原文才能剥掉易腐尾巴手工入库；静默弃
      等于信息全丢。不复用 candidates 通道：那条的文案语义是「教训缺客观
      背书」（P0-7），两个原因混报会让人误判
    - scope 非法：归 project（写错位置的保守方向：用户级错进项目桶是
      分类噪音，反向是隐私泄漏）
    - category 白名单垃圾桶、批内去重：M6.4 原样
    - P0-7 分流：project 桶 constraints（教训类）缺客观背书（verified
      非 true）→ 降级为候选不落盘，报告列出待二次确认；decisions/other
      与用户级条目不适用——用户拍板、硬事实、用户本人说的话本身就是
      权威来源。verified 判定严格（恒等 True）：模型输出 "true" 字符串
      也算无背书，保守方向=降级候选

    返回 (入库条目, 缺背书候选, 易腐拦截)。
    """
    entries: list[Entry] = []
    candidates: list[Entry] = []
    perishable: list[Entry] = []
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
        entry = Entry(category=category, content=content, scope=scope,
                      verified=item.get("verified") is True)
        if _is_perishable(content):
            perishable.append(entry)
        elif scope == "project" and category == "constraints" and not entry.verified:
            candidates.append(entry)
        else:
            entries.append(entry)
    return entries, candidates, perishable


def _append(entries: list[Entry], learned_dir: Path, user_memory_path: Path | None,
            sid: str = "") -> list[str]:
    """按作用域落盘；时间戳与来源 tag 由程序加——出处链条里程序是唯一可信作者。

    P0-7：verified 条目行内带 [已验证] 前缀——「入库条目有背书占比」
    事后可度量（grep 计数 / 总行数），不靠运行时记忆。
    053：sid 非空时行内再带 [固化:{sid}]（sid = 会话 id，store._alloc_id 的
    零填充时间戳）——「这条是哪次对话固化出来的」从此可查，不用去猜。
    sid 为空（测试、或调用点拿不到会话 id）就不写这个 tag，落盘形状与 053
    之前逐字节相同。它不在 learned.VISIBLE_TAGS 里 → 落盘但不进注入 prompt。
    返回写入位置清单（如 ["decisions", "user"]）供文案汇报。
    user_memory_path=None 时的 user 条目已被上游过滤，这里不会再遇到。
    """
    written: list[str] = []
    learned_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    origin = [origin_tag(sid)] if sid else []
    # 与记忆面板编辑侧（learned.update_line/delete_line 的读改写整重写）互斥：
    # 整重写会把窗口期内这里 append 的行连旧内容一起覆盖掉。一批条目一次
    # 拿锁写完，不逐条抢——锁内只有本地文件 append（微秒级），饿不死编辑请求。
    with LEARNED_LOCK:
        for entry in entries:
            tags = ["[已验证]", *origin] if entry.verified else origin
            line = format_line(today, tags, entry.content) + "\n"
            if entry.scope == "user":
                assert user_memory_path is not None   # 上游过滤的契约（防御性）
                user_memory_path.parent.mkdir(parents=True, exist_ok=True)
                with open(user_memory_path, "a", encoding="utf-8") as f:
                    f.write(line)
                if "user" not in written:
                    written.append("user")
            else:
                path = learned_dir / f"{entry.category}.md"
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
                if entry.category not in written:
                    written.append(entry.category)
    return written


def _brief(items: list[Entry], reason: str) -> str:
    """拦截简报片段：报出原文（最多 3 条）——静默弃等于信息全丢，人要看到原文
    才能判断哪部分值得手工入库。空列表返回空串，调用点直接相加。"""
    if not items:
        return ""
    shown = "；".join(e.content for e in items[:3])
    if len(items) > 3:
        shown += f" 等 {len(items)} 条"
    return f"；{len(items)} 条{reason}：{shown}"


def _review_brief(edits: list[dict], counts: dict[str, int | bool], dup_ids: int) -> str:
    """064 审查对账的报告段：edit diff（原文→改后各截 60 字）+ 各类计数，
    原因分开说不混报（050 先例）。空账本返回空串，调用点直接相加。"""
    parts: list[str] = []
    if edits:
        shown = "；".join(
            f"{e['id']}「{e['before'][:60]}」→「{e['after'][:60]}」" for e in edits[:3]
        )
        if len(edits) > 3:
            shown += f" 等 {len(edits)} 条"
        parts.append(f"；{len(edits)} 条经审查修改已留痕（.review.jsonl）：{shown}")
    if counts["old_format"]:
        parts.append("；审查输出为旧格式（按保留清单处理）")
    if counts["hallucinated"]:
        parts.append(f"；{counts['hallucinated']} 条裁决引用了不存在的条目 id（审查幻觉），已弃")
    if counts["illegal"]:
        parts.append(f"；{counts['illegal']} 条裁决非法（非 keep/drop/edit），按 drop 处置")
    if counts["edit_no_content"]:
        parts.append(f"；{counts['edit_no_content']} 条 edit 缺改后正文，按 drop 处置")
    if counts["unruled"]:
        parts.append(f"；{counts['unruled']} 条候选未获裁决，按 drop 处置")
    if dup_ids:
        parts.append(f"；{dup_ids} 条萃取条目 id 重复，已弃")
    return "".join(parts)


def consolidate(  # noqa: PLR0912
    session: Session,
    llm: LLM,
    learned_dir: Path,
    since: int = 0,
    window: int = WINDOW,
    user_memory_path: Path | None = None,
    base_prompt: str = "",
    sid: str = "",
) -> tuple[str, bool]:
    """退出复盘主入口。since = 本次启动时的消息数——无新对话则不白烧 LLM。

    返回 (report, ok)（P2-7 评审修复）：ok=True = 这批消息已被成功处理——
    包括「无条目可沉淀」「全部被审查驳回」这类写入 0 条的正常结局，游标可
    推进；ok=False = 可重试失败（档案员/审查员坏 JSON），调用方不得推进
    consolidated_upto，否则这批对话永远不会再被复盘（记忆静默丢失）。

    user_memory_path（M6.5）：None=未配置用户级位置，user 条目照 v1 行为
    丢弃（防御默认；CLI/Web 装配层恒传 paths.user_memory_path()）。

    base_prompt（ADR 045）：基础人设与工具清单，进「已知记忆」当冗余对照物；
    装配层传 DEFAULT_SYSTEM_PROMPT（见 _load_known 的依赖方向说明）。

    sid（ADR 053）：会话 id，落盘时写成 [固化:{sid}] 行内 tag——装配层的
    settle_session 手里本来就有它，往下传一行。空串 = 不带来源 tag。
    """
    if not any(m.role == "user" for m in session.messages[since:]):
        return "记忆固化：本轮无新对话，跳过复盘", True

    transcript = _transcript(session, window)
    if not transcript.strip():
        return "记忆固化：无可复盘内容", True

    extract_prompt = EXTRACT_TEMPLATE.format(
        max_entries=MAX_ENTRIES_PER_RUN,
        known=_load_known(learned_dir, user_memory_path, base_prompt),
        transcript=transcript,
    )
    raw, extract_ok = _parse_json_array(
        llm.generate([Message(role="user", content=extract_prompt)]).content
    )
    if not extract_ok:
        return "记忆固化：档案员输出无法解析（坏 JSON），未写入", False
    if not raw:
        return "记忆固化：档案员明确表示无条目可沉淀，未写入", True
    tagged, dup_ids = _mint_ids(raw)   # ADR 064 ①：决定先有身份才谈得上对账

    review_prompt = REVIEW_TEMPLATE.format(
        entries_json=json.dumps(tagged, ensure_ascii=False),
        transcript=transcript,
    )
    kept, review_ok = _parse_json_array(
        llm.generate([Message(role="user", content=review_prompt)]).content
    )
    if not review_ok:
        return "记忆固化：审查输出无法解析（坏 JSON），未写入", False
    kept_items, edits, counts = _apply_verdicts(tagged, kept)   # ADR 064 ②
    entries, candidates, perishable = _harden(kept_items)
    _log_edits(learned_dir, edits, sid)   # ADR 064 ③：改写留痕落盘

    # ADR 074 丁：出口过期（规则层扫在用条目，零模型调用）——与本批条目无关，
    # 独立清一次腐化库存，故放在有无新条目的分叉之前
    expired = _expire_perishable(learned_dir, user_memory_path)

    # 各类拦截都报出原文（P0-7 缺背书候选 / ADR 045 易腐 / 064 对账计数），
    # 原因分开说不混报
    brief = (
        _brief(candidates, "教训缺客观背书降为候选（待确认）")
        + _brief(perishable, "含易腐事实（行号/计数/跟踪状态）已拦，要保留请剥掉易腐部分手工入库")
        + _review_brief(edits, counts, dup_ids)
    )

    # 未配置用户级位置：user 条目丢弃（v1 行为），文案如实说——不算驳回
    dropped_unplaced = 0
    if user_memory_path is None:
        dropped_unplaced = sum(1 for e in entries if e.scope == "user")
        entries = [e for e in entries if e.scope != "user"]

    if not entries:
        base = f"记忆固化：{len(raw)} 条候选全部被审查驳回（或未过硬校验），未写入"
        base += brief
        if expired:
            base += f"；{expired} 条在用条目已过期（已打 [已过期]）"
        if dropped_unplaced:
            base += f"；另有 {dropped_unplaced} 条用户级候选因未配置位置丢弃"
        return base, True

    # ADR 074 戊：被取代走模型语义检测（有存量才触发）
    superseded = _supersede_duplicates(entries, learned_dir, user_memory_path, llm)
    written = _append(entries, learned_dir, user_memory_path, sid)
    n_user = sum(1 for e in entries if e.scope == "user")
    n_verified = sum(1 for e in entries if e.verified)
    report = (
        f"记忆固化：新增 {len(entries)} 条（已验证 {n_verified} 条，"
        f"驳回 {len(raw) - len(entries) - len(candidates) - len(perishable)} 条）"
        f"→ {', '.join(written)}"
    )
    if n_user:
        report += f"（其中用户级 {n_user} 条 → {user_memory_path}）"
    report += brief
    if expired:
        report += f"；{expired} 条在用条目已过期（已打 [已过期]）"
    if superseded:
        report += f"；{superseded} 条在用旧条目被新条目取代（已打 [被取代]）"
    if dropped_unplaced:
        report += f"；{dropped_unplaced} 条用户级候选因未配置位置丢弃"
    return report, True


# ---------- ADR 078：记忆块硬上限 + sleep-time 整理 ----------

# 墓碑保留期（天）：防复活可见性窗口（见 learned.sweep_tombstones 头注记）。
# env 可配；测试另可用 maintain_memory 的 keep_days 参数直接注入
TOMBSTONE_KEEP_DAYS = max(0, int(os.environ.get("FACTA_TOMBSTONE_KEEP_DAYS", "7")))


def memory_budget_units() -> int:
    """常驻注入预算（单位=字符，对中文 ≈ token 上界）：单一真值源——
    注入侧（agent 的装填截断）与整理侧（maintain_memory 的触发判定）
    同一份配置。0 = 摘要允许关闭（不截断、不整理）——不做，取 max(1,…)：
    预算的语义是护栏，护栏没有「关」挡。调用时读 env（monkeypatch 可测）。"""
    return max(1, int(os.environ.get("FACTA_MEMORY_BUDGET", "6000")))


def memory_footprint(learned_dir: Path, user_memory_path: Path | None) -> int:
    """常驻注入的当前总量（字符）：全部在用条目 render 尺寸和（user + 三桶）。
    与 agent 注入的计量同式（len(render(e))），只求和不装填——超限判定
    不需要模拟截断。"""
    paths: list[Path] = []
    if user_memory_path is not None:
        paths.append(user_memory_path)
    paths.extend(learned_dir / f"{c}.md" for c in CATEGORIES)
    return sum(len(render(e)) for p in paths for e in read_learned(p))


def maintain_memory(
    learned_dir: Path,
    user_memory_path: Path | None,
    *,
    keep_days: int | None = None,
) -> str:
    """sleep-time 整理 pass（ADR 078）：常驻注入超预算才跑，动作零模型——
    物理回收超 keep_days 的老墓碑（074 边界点名的 P2-2 活）。

    不到预算零动作（「超限触发」的字面）；超限时也只清墓碑——墓碑本来
    就不注入（read_learned 默认过滤），回收的是文件臃肿与 _load_known
    噪音；在用条目超限的正解是人工整理或检索分层（032 裁定二 v2 信号），
    本案权限刻意止步。返回报告（空串 = 什么都没做）。
    挂点在 settle_session 尾部：076 后 settle 是异步后台，天然
    「会话结束后跑、不占在线延迟」。"""
    budget = memory_budget_units()
    total = memory_footprint(learned_dir, user_memory_path)
    if total <= budget:
        return ""
    days = TOMBSTONE_KEEP_DAYS if keep_days is None else keep_days
    paths: list[Path] = [user_memory_path] if user_memory_path is not None else []
    paths.extend(learned_dir / f"{c}.md" for c in CATEGORIES)
    swept = sum(sweep_tombstones(p, days) for p in paths)
    report = (
        f"记忆整理：常驻注入 {total} 字符已超预算 {budget}，"
        f"物理回收 {swept} 条超 {days} 天的老墓碑"
    )
    if swept == 0:
        report += "（无老墓碑可清——超限来自在用条目，请人工整理记忆面板）"
    return report
