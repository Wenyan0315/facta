"""M6.4 记忆固化验收：ScriptedLLM 驱动的全离线管线。

不变量：
- 四段管线一次走通：萃取 JSON（含围栏容错）→ 审查过滤 → 硬校验 → append 落盘
- 时间戳是程序写的（出处链条只信程序，不信模型）
- 审查层真的会踢条目（编造/重复被拒 → 落盘只有保留条目）
- 类别白名单：非法类别进 other 垃圾桶，不炸管道
- 已知记忆进萃取提示词（写前比对的 v1 是提示词级——不重复记）
- 无新对话（since 之后没有 user 消息）→ 直接跳过，不白烧 LLM
"""

import json

from agent.core.llm import ScriptedLLM
from agent.core.types import Message
from agent.memory.consolidate import consolidate
from agent.memory.store import Session


def _session(dialogue: list[Message], summary: str | None = None, upto: int = 1) -> Session:
    messages = [Message(role="system", content="sys")] + dialogue
    return Session(messages=messages, summary=summary, summarized_upto=upto)


def _dialogue() -> list[Message]:
    return [
        Message(role="user", content="以后讲解先给全景再讲细节"),
        Message(role="assistant", content="好，按全景到细节来"),
        Message(role="user", content="项目路径统一放 paths.py"),
        Message(role="assistant", content="已记录"),
    ]


def test_full_pipeline_writes_and_reports(tmp_path):
    script = [
        Message(
            role="assistant",
            content=(
                '```json\n'
                '[{"category": "preferences", "content": "用户偏好全景到细节的讲解"},'
                ' {"category": "decisions", "content": "项目路径统一放 paths.py 管理"}]\n'
                '```'
            ),
        ),
        Message(
            role="assistant",
            content='[{"category": "decisions", "content": "项目路径统一放 paths.py 管理"}]',
        ),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "新增 1 条" in report
    decisions = (tmp_path / "learned" / "decisions.md").read_text(encoding="utf-8")
    # 时间戳是程序加的（- [YYYY-MM-DD] 前缀），内容忠实于原文
    assert "项目路径统一放 paths.py 管理" in decisions
    assert decisions.startswith("- [")
    # preferences 在 v1 不是合法桶 → 直接拒绝/不落（用户级信息不进 repo）
    assert not (tmp_path / "learned" / "preferences.md").exists()


def test_reviewer_drops_fabricated_entries(tmp_path):
    """审查层值一次班：萃取两条，审查只留忠实的那条。"""
    script = [
        Message(
            role="assistant",
            content='[{"category": "constraints", "content": "用户喜欢用 Go 语言写后端"},'
            ' {"category": "constraints", "content": "项目路径统一放 paths.py 管理"}]',
        ),
        Message(
            role="assistant",
            content='[{"category": "constraints", "content": "项目路径统一放 paths.py 管理",'
            ' "verified": true}]',
        ),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "新增 1 条" in report
    assert "驳回 1 条" in report
    constraints = (tmp_path / "learned" / "constraints.md").read_text(encoding="utf-8")
    assert "Go 语言" not in constraints


def test_invalid_category_falls_into_other(tmp_path):
    script = [
        Message(
            role="assistant",
            content='[{"category": "mystery_bucket", "content": "某个项目硬事实"}]',
        ),
        Message(
            role="assistant",
            content='[{"category": "mystery_bucket", "content": "某个项目硬事实"}]',
        ),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    consolidate(session, llm, tmp_path / "learned")

    # 白名单垃圾桶：非法类别不炸管道，归 other.md
    other = (tmp_path / "learned" / "other.md").read_text(encoding="utf-8")
    assert "某个项目硬事实" in other


def test_bad_json_is_graceful(tmp_path):
    llm = ScriptedLLM([Message(role="assistant", content="这绝不是 JSON")])
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "无法解析" in report
    assert "坏 JSON" in report
    assert not (tmp_path / "learned").exists()


def test_empty_array_means_explicitly_nothing(tmp_path):
    """输出 [] ≠ 输出坏 JSON：前者是档案员明确表示无话可说，不烧审查调用。"""
    llm = ScriptedLLM([Message(role="assistant", content="```json\n[]\n```")])
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "无条目可沉淀" in report
    assert len(llm.calls) == 1   # 明确无产出 → 不白烧第二次（审查）调用
    assert not (tmp_path / "learned").exists()


def test_review_bad_json_stops_before_disk(tmp_path):
    """萃取正常但审查输出坏 JSON：异常文案明确指向审查段，不落盘。"""
    script = [
        Message(
            role="assistant",
            content='[{"category": "constraints", "content": "项目路径统一放 paths.py 管理"}]',
        ),
        Message(role="assistant", content="审查员打了个喷嚏"),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "审查输出无法解析" in report
    assert not (tmp_path / "learned").exists()


def test_known_entries_are_fed_to_extract_prompt(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir(parents=True)
    (learned / "decisions.md").write_text(
        "- [2026-01-01] 路径统一放 paths.py 管理\n", encoding="utf-8"
    )
    script = [
        Message(role="assistant", content="[]"),
        Message(role="assistant", content="[]"),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    consolidate(session, llm, learned)

    assert len(llm.calls) >= 1
    extract_input = llm.calls[0][-1].content
    # 已知记忆进了萃取提示词（写前比对 v1 = 提示词级，绝不重复记）
    assert "路径统一放 paths.py 管理" in extract_input
    assert "已知记忆" in extract_input


def test_no_new_dialogue_skips_without_llm_call(tmp_path):
    llm = ScriptedLLM([])
    session = _session(_dialogue())

    # since = 全部消息数 → 之后没有任何 user 消息（启动即退出的场景）
    report = consolidate(session, llm, tmp_path / "learned", since=len(session.messages))

    assert "跳过复盘" in report
    assert llm.calls == []   # 一次 LLM 调用都没发生——不白烧钱
    assert not (tmp_path / "learned").exists()


def test_entries_are_capped_and_deduped(tmp_path):
    """条数上限 + 批内去重：程序管形状（模型吐 8 条也只落 5 条）。"""
    items = [{"category": "constraints", "content": f"约束条目 {i}", "verified": True} for i in range(8)]
    items.append({"category": "constraints", "content": "约束条目 0"})   # 批内重复
    script = [
        Message(role="assistant", content=json.dumps(items, ensure_ascii=False)),
        Message(role="assistant", content=json.dumps(items, ensure_ascii=False)),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "新增 5 条" in report
    lines = (tmp_path / "learned" / "constraints.md").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5


# ---------- M6.5 用户级分流 ----------

def _both_scope_script() -> list[Message]:
    """萃取与审查都放行两条：一条项目级、一条用户级（阳澄湖同款）。"""
    entries = json.dumps(
        [
            {"category": "constraints", "content": "搜索结果需按目的地核对", "scope": "project", "verified": True},
            {"category": "other", "content": "用户偏好行程室内外交错排", "scope": "user"},
        ],
        ensure_ascii=False,
    )
    return [
        Message(role="assistant", content=entries),
        Message(role="assistant", content=entries),
    ]


def test_user_scope_splits_to_user_md(tmp_path):
    """分流主路径：user 条目 → 仓库外 user.md；project 条目照旧进桶。"""
    llm = ScriptedLLM(_both_scope_script())
    session = _session(_dialogue())
    user_md = tmp_path / "personal" / "user.md"

    report = consolidate(session, llm, tmp_path / "learned", user_memory_path=user_md)

    assert "新增 2 条" in report and "用户级 1 条" in report
    constraints = (tmp_path / "learned" / "constraints.md").read_text(encoding="utf-8")
    assert "按目的地核对" in constraints
    user_content = user_md.read_text(encoding="utf-8")
    assert "行程室内外交错排" in user_content
    assert user_content.startswith("- [")   # 同款行格式：读侧零翻译
    assert "交错排" not in constraints      # 用户级不进项目桶（泄漏方向）


def test_user_scope_dropped_when_unconfigured(tmp_path):
    """未配置位置（None）：user 条目照 v1 行为丢弃，文案如实说。"""
    llm = ScriptedLLM(_both_scope_script())
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned", user_memory_path=None)

    assert "新增 1 条" in report
    assert "1 条用户级候选因未配置位置丢弃" in report
    assert (tmp_path / "learned" / "constraints.md").exists()
    for md in (tmp_path / "learned").glob("*.md"):
        assert "交错排" not in md.read_text(encoding="utf-8")


def test_sensitive_entries_never_land(tmp_path):
    """敏感凭证禁令（程序侧硬边界）：API key / 身份证 / 手机号，萃取审查
    都放行也拦在落盘前——信模型语义，不信模型纪律。"""
    entries = json.dumps(
        [
            {"category": "other", "content": "用户的 key 是 sk-abc123def456ghi789jkl012"},
            {"category": "other", "content": "用户身份证号 110101199003077777"},
            {"category": "other", "content": "用户手机号 13800138000"},
            {"category": "constraints", "content": "合法的项目约束条目", "verified": True},
        ],
        ensure_ascii=False,
    )
    script = [
        Message(role="assistant", content=entries),
        Message(role="assistant", content=entries),   # 审查全放行（测的就是硬校验）
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned", user_memory_path=tmp_path / "u.md")

    assert "新增 1 条" in report   # 只剩合法条目
    for md in list((tmp_path / "learned").glob("*.md")) + [tmp_path / "u.md"]:
        if not md.exists():
            continue   # 文件不存在 = 该作用域全被拦下，本身就是断言的一部分
        text = md.read_text(encoding="utf-8")
        assert "sk-abc123" not in text
        assert "110101199003077777" not in text
        assert "13800138000" not in text


def test_invalid_scope_falls_back_to_project(tmp_path):
    """scope 非法归 project（保守方向：错进项目桶是噪音，反向是泄漏）。"""
    entries = json.dumps(
        [{"category": "other", "content": "某条记忆", "scope": "global"}],
        ensure_ascii=False,
    )
    script = [
        Message(role="assistant", content=entries),
        Message(role="assistant", content=entries),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    consolidate(session, llm, tmp_path / "learned", user_memory_path=tmp_path / "u.md")

    assert (tmp_path / "learned" / "other.md").exists()
    assert not (tmp_path / "u.md").exists()


def test_user_memory_known_fed_to_extract_prompt(tmp_path):
    """去重范围跨作用域：user.md 已有内容也要进「已知记忆」。"""
    learned = tmp_path / "learned"
    learned.mkdir(parents=True)
    user_md = tmp_path / "user.md"
    user_md.write_text("- [2026-01-01] 用户偏好全景到细节的讲解\n", encoding="utf-8")
    llm = ScriptedLLM([
        Message(role="assistant", content="[]"),
        Message(role="assistant", content="[]"),
    ])
    session = _session(_dialogue())

    consolidate(session, llm, learned, user_memory_path=user_md)

    extract_input = llm.calls[0][-1].content
    assert "用户偏好全景到细节的讲解" in extract_input
    assert "user.md（用户记忆）" in extract_input


# ---------- P0-7 验证过的经验优先（LongHorizon：自我反思给廉价教训） ----------


def test_unverified_lessons_become_candidates(tmp_path):
    """缺客观背书的教训（project/constraints）降级为候选不落盘，报告待人确认；
    decisions/other 不适用降级（用户拍板与硬事实本身即权威来源）。"""
    entries = json.dumps(
        [
            {"category": "constraints", "content": "agent 自我总结的教训"},          # → 候选
            {"category": "constraints", "content": "测试全过的经验", "verified": True},  # → 入库
            {"category": "decisions", "content": "用户拍板用 SQLite"},                # → 入库（不受限）
        ],
        ensure_ascii=False,
    )
    script = [
        Message(role="assistant", content=entries),
        Message(role="assistant", content=entries),   # 审查全放行（测的是硬校验分流）
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "新增 2 条" in report
    assert "已验证 1 条" in report
    assert "1 条教训缺客观背书降为候选（待确认）" in report
    assert "agent 自我总结的教训" in report            # 候选内容列出供二次确认
    constraints = (tmp_path / "learned" / "constraints.md").read_text(encoding="utf-8")
    assert "测试全过的经验" in constraints
    assert "自我总结" not in constraints               # 缺背书的教训不落盘
    # 背书占比事后可度量：verified 条目行内带 [已验证] 前缀
    assert "[已验证] 测试全过的经验" in constraints
    decisions = (tmp_path / "learned" / "decisions.md").read_text(encoding="utf-8")
    assert "用户拍板用 SQLite" in decisions


def test_all_candidates_reported_when_nothing_written(tmp_path):
    """候选全数降级、零入库时：文案如实说无入库 + 候选清单，不算「审查驳回」。"""
    entries = json.dumps(
        [{"category": "constraints", "content": "没背书也别瞎记"}],
        ensure_ascii=False,
    )
    script = [
        Message(role="assistant", content=entries),
        Message(role="assistant", content=entries),
    ]
    llm = ScriptedLLM(script)
    session = _session(_dialogue())

    report = consolidate(session, llm, tmp_path / "learned")

    assert "未写入" in report
    assert "降为候选（待确认）" in report
    assert not (tmp_path / "learned").exists()


# ---------- ADR 045 易腐事实不进 learned（第二道代码闸门） ----------


def test_perishable_facts_blocked_but_reported(tmp_path):
    """含行号/计数/跟踪状态的条目不落盘，但原文进报告——稳定部分（文件路径）
    值得保留，人要看到原文才能剥掉易腐尾巴手工入库；静默弃 = 信息全丢。"""
    entries = json.dumps(
        [
            {"category": "other", "content": "run_turn 在 loop.py 第 113 行"},
            {"category": "other", "content": "笔记库当前共 14 篇"},
            {"category": "other", "content": "loop.py 尚未被 git 跟踪"},
            {"category": "other", "content": "run_turn 的循环体在 loop.py"},   # 稳定 → 入库
        ],
        ensure_ascii=False,
    )
    script = [Message(role="assistant", content=entries)] * 2
    llm = ScriptedLLM(script)

    report = consolidate(_session(_dialogue()), llm, tmp_path / "learned")

    other = (tmp_path / "learned" / "other.md").read_text(encoding="utf-8")
    assert "循环体在 loop.py" in other          # 稳定条目照常入库
    assert "第 113 行" not in other
    assert "共 14 篇" not in other
    assert "尚未被 git 跟踪" not in other
    assert "新增 1 条" in report                # 易腐条目不算入库也不算「审查驳回」
    assert "3 条含易腐事实" in report
    assert "run_turn 在 loop.py 第 113 行" in report   # 原文报出供人工剥离


def test_perishable_gate_does_not_overreach(tmp_path):
    """闸门保守：只拦明确形态，不拦裸数字——「三层」「7–10 天」「最多 5 条」
    都是稳定事实，误杀等于把正常记忆挡在门外。"""
    entries = json.dumps(
        [
            {"category": "other", "content": "架构分三层：core / memory / orchestrator"},
            {"category": "other", "content": "气象预报的有效窗口约 7–10 天"},
            {"category": "other", "content": "每次固化最多产出 5 条"},
            {"category": "other", "content": "不跟踪用户的地理位置"},
        ],
        ensure_ascii=False,
    )
    script = [Message(role="assistant", content=entries)] * 2
    llm = ScriptedLLM(script)

    report = consolidate(_session(_dialogue()), llm, tmp_path / "learned")

    assert "新增 4 条" in report
    assert "易腐" not in report
    other = (tmp_path / "learned" / "other.md").read_text(encoding="utf-8")
    for text in ("架构分三层", "7–10 天", "最多产出 5 条", "不跟踪用户的地理位置"):
        assert text in other


# ---------- ADR 045 冗余的对照物：基础 prompt 进「已知记忆」 ----------


def test_base_prompt_enters_known_material(tmp_path):
    """此前「与 system prompt 重复」这类冗余在原理上查不出来——不是模型不守
    纪律，是它手上没那份材料。装配层传 DEFAULT_SYSTEM_PROMPT 补上对照物。"""
    learned = tmp_path / "learned"
    learned.mkdir(parents=True)
    llm = ScriptedLLM([Message(role="assistant", content="[]")] * 2)

    consolidate(_session(_dialogue()), llm, learned,
                base_prompt="唯一标识串-XYZ：search_notes 搜知识库内容")

    extract_input = llm.calls[0][-1].content
    assert "基础人设与工具清单（每轮已注入，不要再记）" in extract_input
    assert "唯一标识串-XYZ" in extract_input


def test_no_base_prompt_keeps_prior_behaviour(tmp_path):
    """默认空 = 改动前行为逐字相同（16 处测试调用点与旧装配路径不受影响）。"""
    learned = tmp_path / "learned"
    learned.mkdir(parents=True)
    llm = ScriptedLLM([Message(role="assistant", content="[]")] * 2)

    consolidate(_session(_dialogue()), llm, learned)

    extract_input = llm.calls[0][-1].content
    assert "基础人设与工具清单" not in extract_input
    assert "（暂无）" in extract_input          # 无 learned 条目、无 user.md、无 base_prompt
