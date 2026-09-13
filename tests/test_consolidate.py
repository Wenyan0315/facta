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
from pathlib import Path

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
            content='[{"category": "constraints", "content": "项目路径统一放 paths.py 管理"}]',
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
    items = [{"category": "constraints", "content": f"约束条目 {i}"} for i in range(8)]
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