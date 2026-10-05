"""记忆面板验收（2026-09-19，025）：learned 读/改/删原语 + 三端点。

不变量：
- 行号定位协议：GET 返回的 line 原样传回 PUT/DELETE，操作后行号仍对
- 好行编辑保留日期前缀（时间戳归程序管）；坏行（手写行）原样替换可删
- 空行/文件尾换行在重写后原样保留（append-only 固化的兼容前提）
- category 白名单挡路径穿越，line 越界 404，空内容 400
- 053：人工编辑过的行必须与程序固化的行可辨（[手改]），且原 tags 不丢
"""

import pytest

from facta.memory.learned import (
    delete_line,
    delete_line_by_id,
    format_line,
    make_id,
    read_learned,
    render,
    set_statuses,
    sweep_tombstones,
    update_line,
    update_line_by_id,
    visible_text,
)


def _write(tmp_path, text):
    path = tmp_path / "constraints.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------- read_learned：解析 ----------

def test_read_parses_formatted_lines(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 第一条\n- [2026-09-14] 第二条\n")
    entries = read_learned(path)
    assert [(e.line, e.date, e.content) for e in entries] == [
        (0, "2026-09-13", "第一条"),
        (1, "2026-09-14", "第二条"),
    ]


def test_read_tolerates_bad_lines_and_skips_blank(tmp_path):
    # 手写坏行不炸：date=None、content=原行全文；空行不进列表（行号仍按文件计）
    path = _write(tmp_path, "- [2026-09-13] 好行\n\n手写的裸行\n")
    entries = read_learned(path)
    assert [(e.line, e.date) for e in entries] == [(0, "2026-09-13"), (2, None)]
    assert entries[1].content == "手写的裸行"


def test_read_missing_file_is_empty(tmp_path):
    assert read_learned(tmp_path / "nope.md") == []


# ---------- update / delete：行号定位 ----------

def test_update_keeps_date_prefix(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 原文\n")
    update_line(path, 0, "新正文")
    # 053：日期照旧保留，同时打上 [手改]——「人碰过」从此可辨
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] [手改] 新正文\n"


def test_update_bad_line_replaces_raw(tmp_path):
    path = _write(tmp_path, "手写裸行\n- [2026-09-13] 好行\n")
    update_line(path, 0, "改过的裸行")
    assert path.read_text(encoding="utf-8") == "改过的裸行\n- [2026-09-13] 好行\n"


def test_update_preserves_blank_lines_and_tail_newline(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 甲\n\n- [2026-09-14] 乙\n")
    update_line(path, 2, "乙改")
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] 甲\n\n- [2026-09-14] [手改] 乙改\n"


def test_line_out_of_range_raises(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 唯一\n")
    with pytest.raises(IndexError):
        update_line(path, 5, "x")
    with pytest.raises(IndexError):
        delete_line(path, 5)


def test_delete_line_leaves_tombstone(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 甲\n- [2026-09-14] 乙\n- [2026-09-15] 丙\n")
    delete_line(path, 1)
    raw = path.read_text(encoding="utf-8").splitlines()
    # 好行打 [已撤回:今天] 留 tombstone，正文与其余行都在
    assert raw[1].startswith("- [2026-09-14] [已撤回:")
    assert "乙" in raw[1]
    assert raw[0] == "- [2026-09-13] 甲"
    assert raw[2] == "- [2026-09-15] 丙"
    # 默认读侧过滤：只剩在用两行
    assert [(e.line, e.content) for e in read_learned(path)] == [(0, "甲"), (2, "丙")]
    # include_inactive 带出 tombstone，状态字段可解析
    all_lines = read_learned(path, include_inactive=True)
    assert [e.status for e in all_lines] == [None, "已撤回", None]
    assert all_lines[1].status_date is not None


def test_delete_bad_line_still_removes_physically(tmp_path):
    # 坏行（无日期无 tag）打不上 tombstone，仍物理删
    path = _write(tmp_path, "手写裸行\n- [2026-09-13] 好行\n")
    delete_line(path, 0)
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] 好行\n"


def test_set_statuses_roundtrip(tmp_path):
    path = _write(tmp_path, "- [2026-09-13] 甲\n- [2026-09-14] 乙\n")
    set_statuses(path, "2026-10-05", {1: "已过期"})
    entries = read_learned(path, include_inactive=True)
    assert entries[0].status is None and entries[0].content == "甲"
    assert entries[1].status == "已过期" and entries[1].status_date == "2026-10-05"
    assert entries[1].content == "乙"
    # 默认过滤后只剩在用一行
    assert [e.content for e in read_learned(path)] == ["甲"]


# ---------- ADR 078：墓碑物理回收（sleep-time 整理的执行原语） ----------


def test_sweep_tombstones_by_age(tmp_path):
    """老墓碑物理删（事件时间超 keep_days），新墓碑与在用行原样保留。"""
    from datetime import date

    path = _write(
        tmp_path,
        "- [2026-09-01] 在用甲\n"
        "- [2026-09-01] [已撤回:2026-09-02] 老墓碑\n"
        "- [2026-10-01] [被取代:2026-10-01] 新墓碑\n"
        "- [2026-09-22] 在用乙\n",
    )
    swept = sweep_tombstones(path, keep_days=7, today=date(2026, 10, 5))
    assert swept == 1
    assert [e.content for e in read_learned(path, include_inactive=True)] == [
        "在用甲", "新墓碑", "在用乙",
    ]


def test_sweep_tombstones_keeps_bad_date_and_missing_file(tmp_path):
    """事件时间解析失败的墓碑保守保留（坏日期不是删除理由）；缺席文件零动作。"""
    from datetime import date

    path = _write(tmp_path, "- [2026-09-01] [已撤回:哪天?] 坏日期墓碑\n- [2026-09-02] 在用\n")
    assert sweep_tombstones(path, keep_days=0, today=date(2026, 10, 5)) == 0
    assert len(read_learned(path, include_inactive=True)) == 2
    assert sweep_tombstones(tmp_path / "nope.md", keep_days=7) == 0


# ---------- API 端点 ----------

def _client(monkeypatch, tmp_path):
    """最小 AppContext + LEARNED_DIR 指向临时目录（与 test_app 同款隔离模式）。

    user 桶也一并隔离（FACTA_USER_MEMORY，041 的现成注入点）：面板端点把
    user 当第四个伪 category，路径走 paths.user_memory_path()——不隔离就会
    读真 home 的隐私文件，且断言「列出的条目集」会被开发机上的真实用户级
    记忆污染（2026-09-29 实爆：首条真实用户记忆落盘后本测试翻红）。
    """
    from fastapi.testclient import TestClient

    from facta.core.llm import ScriptedLLM
    from facta.core.types import Message
    from facta.memory.store import SessionStore
    from facta.orchestrator.agent import Agent
    from facta.orchestrator.assemble import AppContext
    from facta.server.app import create_app
    from facta.tools.registry import ToolRegistry

    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path)
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))

    ctx = AppContext(
        provider="mock",
        ledger=None,
        embedder=None,
        llm=ScriptedLLM([Message(role="assistant", content="ok")]),
        internal_llm=ScriptedLLM([]),
        kb=None,
        store=SessionStore(tmp_path / "sessions"),
        # 记忆面板端点不跑 worker，工厂只需满足类型（不会真被调用）
        build_agent=lambda session: Agent(
            name="test", system_prompt="测试人设", registry=ToolRegistry()
        ),
        todos=None,
    )
    return TestClient(create_app(ctx))


def test_api_roundtrip_edit_and_delete(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    (tmp_path / "constraints.md").write_text(
        "- [2026-09-13] 甲\n- [2026-09-14] 乙\n", encoding="utf-8"
    )

    listed = client.get("/api/learned").json()
    assert [(e["category"], e["line"], e["date"]) for e in listed] == [
        ("constraints", 0, "2026-09-13"),
        ("constraints", 1, "2026-09-14"),
    ]

    # 编辑保留日期
    assert client.put(
        "/api/learned/constraints/0", json={"content": "甲改"}
    ).status_code == 200
    # 删除按行号
    assert client.delete("/api/learned/constraints/1").status_code == 200

    after = client.get("/api/learned").json()
    assert len(after) == 1
    # 053：面板的 content = 可见 tag + 正文（前端零改动：改完原样 PUT 回来）
    assert after[0]["date"] == "2026-09-13" and after[0]["content"] == "[手改] 甲改"
    # 被删行后，剩余行号已变（0）——前端 refresh 后用新行号，协议自洽
    assert after[0]["line"] == 0


def test_api_guards(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    (tmp_path / "other.md").write_text("- [2026-09-13] 唯一\n", encoding="utf-8")

    # 路径穿越被挡（httpx 客户端侧即归一化 → 405；即便构造原始请求，
    # category 白名单也会拦）——断言只认「没成功写入」
    assert client.put("/api/learned/../etc/0", json={"content": "x"}).status_code >= 400
    assert client.put("/api/learned/other/9", json={"content": "x"}).status_code == 404
    assert client.put("/api/learned/other/0", json={"content": "  "}).status_code == 400
    assert client.delete("/api/learned/decisions/0").status_code == 404   # 类别文件不存在


# ---------- 041：用户级分栏 ----------

def test_learned_path_user_follows_env(monkeypatch, tmp_path):
    """user 的路径走 paths.user_memory_path()——与 agent 注入侧同源（FACTA_USER_MEMORY）。"""
    from facta.server.app import PANEL_CATEGORIES, _learned_path

    monkeypatch.setattr("facta.server.app.LEARNED_DIR", tmp_path)
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "user.md"))
    assert _learned_path("user") == tmp_path / "user.md"
    assert _learned_path("constraints") == tmp_path / "constraints.md"
    assert PANEL_CATEGORIES == ("decisions", "constraints", "other", "user")


def test_api_user_scope_roundtrip(monkeypatch, tmp_path):
    user_md = tmp_path / "user.md"
    user_md.write_text(
        "- [2026-09-20] 行程提早一周提醒\n- [2026-09-21] 回答用中文\n", encoding="utf-8"
    )
    project = tmp_path / "constraints.md"
    project.write_text("- [2026-09-13] 项目甲\n", encoding="utf-8")
    before = project.read_bytes()
    monkeypatch.setenv("FACTA_USER_MEMORY", str(user_md))
    client = _client(monkeypatch, tmp_path)

    listed = client.get("/api/learned").json()
    assert [(e["category"], e["line"], e["date"]) for e in listed if e["category"] == "user"] == [
        ("user", 0, "2026-09-20"),
        ("user", 1, "2026-09-21"),
    ]

    # 编辑保留日期前缀、删除按行号——与项目桶同一套协议（同一个 learned.py）
    assert client.put("/api/learned/user/0", json={"content": "行程提早两周提醒"}).status_code == 200
    assert client.delete("/api/learned/user/1").status_code == 200
    # ADR 074：删除留 tombstone，正文仍在但默认 GET 已隐藏
    raw = user_md.read_text(encoding="utf-8").splitlines()
    assert raw[0] == "- [2026-09-20] [手改] 行程提早两周提醒"
    assert raw[1].startswith("- [2026-09-21] [已撤回:")
    # 用户级操作不动项目桶（字节级）
    assert project.read_bytes() == before


def test_api_user_scope_missing_file_and_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTA_USER_MEMORY", str(tmp_path / "never-created.md"))
    client = _client(monkeypatch, tmp_path)

    # 新用户：固化管线还没写出 user.md → 空栏，不是错误
    assert client.get("/api/learned").json() == []
    assert client.put("/api/learned/user/0", json={"content": "x"}).status_code == 404
    assert client.delete("/api/learned/user/0").status_code == 404
    # 白名单外的类别仍 400（含看起来像目录名的），穿越防线没被 user 撑开
    assert client.put("/api/learned/preferences/0", json={"content": "x"}).status_code == 400
    assert client.delete("/api/learned/notes/0").status_code == 400


# ---------- 053：provenance（来源侧）----------

def test_legacy_lines_parse_without_tags(tmp_path):
    """零迁移：P0-7 之前的存量行（无 tag）照旧解析，渲染形状逐字节不变。"""
    path = _write(tmp_path, "- [2026-09-13] 老行\n")
    (e,) = read_learned(path)
    assert (e.date, e.content, e.tags) == ("2026-09-13", "老行", ())
    assert render(e) == "- [2026-09-13] 老行"


def test_origin_tag_lands_on_disk_but_not_in_prompt(tmp_path):
    """[固化:sid] 只落盘：解析得出来（给人排查），render 时被滤掉（不给模型看）。"""
    path = _write(tmp_path, format_line("2026-09-13", ["[固化:0001]"], "用 BGE-M3") + "\n")
    (e,) = read_learned(path)
    assert (e.tags, e.content) == (("[固化:0001]",), "用 BGE-M3")
    assert render(e) == "- [2026-09-13] 用 BGE-M3"
    assert visible_text(e) == "用 BGE-M3"     # 面板也看不见它，要看就去磁盘看原行


# ---------- ADR 071：稳定 id 落盘 + by-id 增改删 ----------


def test_make_id_is_deterministic():
    # 同一 (date, content, seq) → 同一 id；内容变 → id 变
    a = make_id("2026-09-29", "测试", 1)
    b = make_id("2026-09-29", "测试", 1)
    c = make_id("2026-09-29", "测试改", 1)
    d = make_id("2026-09-29", "测试", 2)
    assert a == b
    assert a != c       # content 变 → id 变
    assert a != d       # seq 变 → id 变（防御同秒同内容撞车）
    assert len(a) == 8  # 8 字节 hex


def test_id_round_trip(tmp_path):
    """id 写入落盘后 read_learned 能解析回来；老格式行 id=None 不破坏。"""
    new = format_line("2026-09-29", [], "新内容", id="abcd1234")
    legacy = format_line("2026-09-13", [], "老格式无 id")     # 不传 id → 不写注释
    path = _write(tmp_path, new + "\n" + legacy + "\n")

    e1, e2 = read_learned(path)
    assert e1.id == "abcd1234" and e1.content == "新内容"
    assert e2.id is None and e2.content == "老格式无 id"     # 向后兼容：老文件没 id


def test_update_by_id_preserves_id(tmp_path):
    # id 在 edit 时保留（避免 hash 重算导致外部链接失效）
    path = _write(tmp_path, format_line("2026-09-29", [], "原内容", id="abcd1234") + "\n")
    update_line_by_id(path, "abcd1234", "改后内容")
    e = read_learned(path)[0]
    assert e.id == "abcd1234"
    assert e.content == "改后内容"
    assert "abcd1234" in path.read_text()    # 尾注释仍存


def test_delete_by_id_works_and_misses_raise(tmp_path):
    p = format_line("2026-09-29", [], "保留", id="aaaabbbb")
    q = format_line("2026-09-29", [], "删除目标", id="ccccdddd")
    path = _write(tmp_path, p + "\n" + q + "\n")

    delete_line_by_id(path, "ccccdddd")
    lines = read_learned(path)
    assert len(lines) == 1
    assert lines[0].id == "aaaabbbb"

    # id 不存在 → KeyError（API 层转 404）
    with pytest.raises(KeyError):
        delete_line_by_id(path, "deadbeef")
    with pytest.raises(KeyError):
        update_line_by_id(path, "deadbeef", "应该不写")


def test_id_stable_across_concurrent_delete(tmp_path):
    """评审边界复现——两标签页拿同一 id 删不同行：各删各的，行号错位也不互相干扰。"""
    p = format_line("2026-09-29", [], "甲", id="aaaa1111")
    q = format_line("2026-09-29", [], "乙", id="bbbb2222")
    r = format_line("2026-09-29", [], "丙", id="cccc3333")
    path = _write(tmp_path, p + "\n" + q + "\n" + r + "\n")

    # 模拟：tab1 在 t0 读到三行 id={aaaa, bbbb, cccc}；tab2 删了 bbbb；tab1 按 id 删 cccc
    delete_line_by_id(path, "bbbb2222")    # tab2 的删除
    delete_line_by_id(path, "cccc3333")    # tab1 按 id 删除（不再是「按行号 2」——行号变了）

    lines = read_learned(path)
    assert [(e.id, e.content) for e in lines] == [("aaaa1111", "甲")]


def test_visible_tags_render_in_order(tmp_path):
    path = _write(tmp_path, format_line("2026-09-13", ["[已验证]", "[固化:s1]"], "正文") + "\n")
    (e,) = read_learned(path)
    assert render(e) == "- [2026-09-13] [已验证] 正文"


def test_update_keeps_origin_tag_and_visible_tags(tmp_path):
    """人工编辑：[手改] 强制在场，磁盘上的 [固化:sid] 补回（面板看不见它就改不着它）。"""
    path = _write(tmp_path, format_line("2026-09-13", ["[已验证]", "[固化:s1]"], "原文") + "\n")
    # 面板 GET 给的就是 visible_text，用户改完原样 PUT 回来（前端零改动的契约）
    update_line(path, 0, visible_text(read_learned(path)[0]).replace("原文", "改后"))
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] [已验证] [手改] [固化:s1] 改后\n"


def test_update_lets_user_drop_verified_tag(tmp_path):
    """可见 tag 以回传为准：用户能删掉 [已验证]——021「能改」的语义不因 provenance 收回。"""
    path = _write(tmp_path, format_line("2026-09-13", ["[已验证]"], "原文") + "\n")
    update_line(path, 0, "原文改")          # 回传不带 [已验证]
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] [手改] 原文改\n"


def test_update_does_not_duplicate_hand_edited_tag(tmp_path):
    """连改两次只留一个 [手改]（不去重就一轮多一个，注入 prompt 越滚越长）。"""
    path = _write(tmp_path, "- [2026-09-13] 原文\n")
    update_line(path, 0, "[手改] 一次")
    update_line(path, 0, "[手改] 二次")
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] [手改] 二次\n"


def test_bracket_outside_vocabulary_stays_in_content(tmp_path):
    """闭集词表：词表外的方括号是正文不是 tag——否则它从注入 prompt 里静默消失。"""
    path = _write(tmp_path, "- [2026-09-13] [TODO] 修一下\n")
    (e,) = read_learned(path)
    assert e.tags == () and e.content == "[TODO] 修一下"
    assert render(e) == "- [2026-09-13] [TODO] 修一下"
    # 编辑侧同口径：剥不出词表外的 tag，用户写的字一个不少
    update_line(path, 0, visible_text(e))
    assert path.read_text(encoding="utf-8") == "- [2026-09-13] [手改] [TODO] 修一下\n"


def test_tag_copied_into_content_self_heals(tmp_path):
    """自愈：_load_known 把原行喂给内部 LLM，模型可能把 tag 抄进正文 → 落盘长出
    重复 tag；下一轮读盘重新解析成 tag、visible_text 去重，注入 prompt 不膨胀。"""
    path = _write(tmp_path, "- [2026-09-13] [已验证] [已验证] 用 BGE-M3\n")
    (e,) = read_learned(path)
    assert e.content == "用 BGE-M3"
    assert render(e) == "- [2026-09-13] [已验证] 用 BGE-M3"

