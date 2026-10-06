"""S4a 文件工具验收：workspace 围栏 / 四件行为 / diff 摘要（2026-09-17）。

不变量：
- 围栏：越界路径 / 敏感黑名单（.env/.git/会话/审计）→ ValueError → 模型可自纠
- read/search/list = L0；write_file = L1（S3 分级框架下）
- write_file 覆盖必返 diff（改了什么一眼可见）
"""

import pytest

from facta.tools.files import (
    _list_dir,
    _read_file,
    _search_code,
    _write_file,
    read_file_succeeded,
    register_file_tools,
    write_file_succeeded,
)
from facta.tools.registry import ToolRegistry

# ---------- workspace 围栏 ----------

@pytest.mark.parametrize("path", [
    "../outside.txt",                       # 上跳逃逸
    "/etc/passwd",                          # 绝对路径跳脱
    "src/../../../../etc/hosts",            # 嵌套上跳
])
def test_fence_rejects_escape_paths(path):
    with pytest.raises(ValueError, match="越界|拒绝"):
        _read_file(path)


@pytest.mark.parametrize("path", [
    ".env",                                 # 密钥（读都不行）
    ".env.local",
    "docs/../.env",
    ".git/config",                          # 版本库内部
    "data/memory/session.json",             # 会话隐私
    "data/audit/audit-20260917.jsonl",      # 审计（防篡改痕迹）
    "servers/sandbox/x.txt",                # MCP 沙箱区
])
def test_fence_rejects_sensitive_paths(path):
    with pytest.raises(ValueError, match="敏感"):
        _read_file(path)


def test_fence_allows_normal_project_files():
    out = _read_file("src/facta/paths.py", offset=17)   # 常量定义区
    assert "WORKSPACE_ROOT" in out
    assert "拒绝" not in out


# ---------- 四件行为 ----------

def test_read_file_paging(tmp_path):
    # 分页窗口：超 limit 显示行号区间 + 续读提示（S6a 起 root 显式注入，替代 monkeypatch）
    (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1, 51)), encoding="utf-8")

    out = _read_file("big.py", limit=10, root=tmp_path)
    assert "共 50 行" in out and "line1" in out and "line10" in out
    assert "line11" not in out
    assert "offset=11" in out               # 教模型怎么续读


def test_read_file_window_indicators(tmp_path):
    # 037 P1：窗口上下方余量显式指示（SWE-agent ACI 式）
    (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1, 51)), encoding="utf-8")

    out = _read_file("big.py", offset=21, limit=10, root=tmp_path)
    assert "显示第 21~30 行" in out
    assert "上方还有 20 行" in out and "下方还有 20 行" in out
    assert "line20" not in out and "line31" not in out

    full = _read_file("big.py", limit=100, root=tmp_path)
    assert "上方" not in full and "下方" not in full   # 全文件无窗口指示

    # 037 P2：空文件显式标记
    (tmp_path / "empty.py").write_text("", encoding="utf-8")
    assert "空文件" in _read_file("empty.py", root=tmp_path)

    # offset 越界显式报错而非静默回全量
    assert "超出文件范围" in _read_file("big.py", offset=99, root=tmp_path)


def test_read_file_missing_and_binary(tmp_path):
    assert "不存在" in _read_file("nope.py", root=tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\xff\xfe")
    assert "不是文本" in _read_file("blob.bin", root=tmp_path)


def test_search_code_finds_and_formats(tmp_path):
    (tmp_path / "a.py").write_text("def run_turn():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")

    # 037 P3：默认只回文件清单+命中计数，不回行内容
    out = _search_code("run_turn", root=tmp_path)
    assert "a.py（1 处）" in out
    assert "def run_turn" not in out
    assert "b.py" not in out                # 未命中文件不出现

    # 行级明细需显式二次展开
    lines_out = _search_code("run_turn", show_lines=True, root=tmp_path)
    assert "a.py:1: def run_turn():" in lines_out

    assert "没有命中" in _search_code("不存在的符号xyz", root=tmp_path)


def test_search_code_skips_sensitive_dirs(tmp_path):
    # .git/.venv 等黑名单目录整树跳过（含命中也不返回）
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "cfg").write_text("SECRET_TOKEN here\n", encoding="utf-8")

    out = _search_code("SECRET_TOKEN", root=tmp_path)
    assert "没有命中" in out


def test_list_dir_one_level(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "in_pkg.py").write_text("x", encoding="utf-8")
    (tmp_path / "top.py").write_text("y", encoding="utf-8")

    out = _list_dir(".", root=tmp_path)
    assert "pkg/" in out and "top.py" in out
    assert "in_pkg.py" not in out           # 只列一层


def test_write_file_new_and_diff(tmp_path):
    # 新建
    out = _write_file("new.py", "print('v1')", root=tmp_path)
    assert "已新建" in out

    # 覆盖 → 必返 diff
    out = _write_file("new.py", "print('v2')", root=tmp_path)
    assert "已覆盖" in out
    assert "-print('v1')" in out and "+print('v2')" in out

    # 内容相同 → 不写
    assert "完全相同" in _write_file("new.py", "print('v2')", root=tmp_path)


def test_write_file_respects_fence(tmp_path):
    with pytest.raises(ValueError, match="敏感"):
        _write_file(".env", "STOLEN=1", root=tmp_path)


def test_memory_write_fence_blocks_write_but_not_read(tmp_path):
    """052：记忆资产 write_file 拒写且未落盘；read_file 照常放行——围栏只作用于写。"""
    notes = tmp_path / "data" / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text("hi", encoding="utf-8")
    (tmp_path / "data" / "graph.json").write_text("{}", encoding="utf-8")

    for target in ("data/notes/x.md", "data/notes/a.md", "data/learned/f.md", "data/graph.json"):
        assert "拒绝直写" in _write_file(target, "poison", root=tmp_path), target
    assert not (notes / "x.md").exists()
    assert (notes / "a.md").read_text(encoding="utf-8") == "hi"
    assert (tmp_path / "data" / "graph.json").read_text(encoding="utf-8") == "{}"

    # 读语义不变（防过度封堵的回归守卫：notes 是 r1/i3/i4 的 verify 语料）
    assert "hi" in _read_file("data/notes/a.md", root=tmp_path)
    # 正对照：非记忆路径照常写
    assert "已新建" in _write_file("data/other.md", "ok", root=tmp_path)


# ---------- 注册与分级 ----------

def test_file_tools_registered_with_levels():
    from pathlib import Path

    from facta.tools.context import ToolContext

    registry = ToolRegistry()
    register_file_tools(registry, ToolContext(notes_dir=Path("data/notes")))
    schemas = {s["function"]["name"] for s in registry.schemas()}
    assert {"read_file", "search_code", "list_dir", "write_file"} <= schemas


def test_builtin_split_menu_unchanged():
    # 拆分回归：kb/llm/history 全缺席时，register_builtin 聚合菜单 =
    # time 1 件 + notes 3 件（条件注册的两对缺席）= 4 件，与拆分前语义一致
    from pathlib import Path as P

    from facta.tools.builtin import register_builtin
    from facta.tools.context import ToolContext

    registry = ToolRegistry()
    register_builtin(registry, ToolContext(notes_dir=P("data/notes")))
    assert set(registry.names()) == {
        "get_current_time", "list_notes", "read_notes", "write_note",
    }


# ---------- R04/091 成败判定对账：真实输出喂判定函数，文案漂移先红 ----------


def test_read_success_predicate_matches_real_outputs(tmp_path):
    """read_file_succeeded 认下 _read_file 全部成功变体、拒掉真实失败输出。"""
    (tmp_path / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    assert read_file_succeeded(_read_file("a.txt", root=tmp_path), "a.txt")
    # 空文件变体
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert read_file_succeeded(_read_file("empty.txt", root=tmp_path), "empty.txt")
    # offset 超范围变体（模型视角的「读完了」，仍算读过）
    assert read_file_succeeded(_read_file("a.txt", offset=99, root=tmp_path), "a.txt")
    # 失败：不存在 / 二进制拒绝
    assert not read_file_succeeded(_read_file("ghost.txt", root=tmp_path), "ghost.txt")
    (tmp_path / "bin.dat").write_bytes(b"\x00\xff\xfe")  # 非法 UTF-8 = 二进制
    assert not read_file_succeeded(_read_file("bin.dat", root=tmp_path), "bin.dat")


def test_write_success_predicate_matches_real_outputs(tmp_path):
    """write_file_succeeded 认下新建/覆盖（含 diff 注记后缀）、拒掉真实失败输出。"""
    assert write_file_succeeded(_write_file("b.txt", "第一版", root=tmp_path))
    assert write_file_succeeded(_write_file("b.txt", "第二版改了内容", root=tmp_path))
    # 失败：内容相同未写入 / 二进制拒绝覆盖
    assert not write_file_succeeded(_write_file("b.txt", "第二版改了内容", root=tmp_path))
    (tmp_path / "bin.dat").write_bytes(b"\x00\xff\xfe")  # 非法 UTF-8 = 二进制
    assert not write_file_succeeded(_write_file("bin.dat", "文本", root=tmp_path))
