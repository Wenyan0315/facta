"""S4a 文件工具验收：workspace 围栏 / 四件行为 / diff 摘要（2026-09-17）。

不变量：
- 围栏：越界路径 / 敏感黑名单（.env/.git/会话/审计）→ ValueError → 模型可自纠
- read/search/list = L0；write_file = L1（S3 分级框架下）
- write_file 覆盖必返 diff（改了什么一眼可见）
"""

import pytest

from agent.tools.files import (
    _list_dir,
    _read_file,
    _search_code,
    _write_file,
    register_file_tools,
)
from agent.tools.registry import ToolRegistry

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
    out = _read_file("src/agent/paths.py", offset=17)   # 常量定义区
    assert "WORKSPACE_ROOT" in out
    assert "拒绝" not in out


# ---------- 四件行为 ----------

def test_read_file_paging(tmp_path, monkeypatch):
    # 分页窗口：超 limit 显示行号区间 + 续读提示
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)
    (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1, 51)), encoding="utf-8")

    out = _read_file("big.py", limit=10)
    assert "共 50 行" in out and "line1" in out and "line10" in out
    assert "line11" not in out
    assert "offset=11" in out               # 教模型怎么续读


def test_read_file_missing_and_binary(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)
    assert "不存在" in _read_file("nope.py")
    (tmp_path / "blob.bin").write_bytes(b"\x00\xff\xfe")
    assert "不是文本" in _read_file("blob.bin")


def test_search_code_finds_and_formats(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("def run_turn():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")

    out = _search_code("run_turn")
    assert "a.py:1: def run_turn():" in out
    assert "b.py" not in out                # 未命中文件不出现
    assert "没有命中" in _search_code("不存在的符号xyz")


def test_search_code_skips_sensitive_dirs(tmp_path, monkeypatch):
    # .git/.venv 等黑名单目录整树跳过（含命中也不返回）
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "cfg").write_text("SECRET_TOKEN here\n", encoding="utf-8")

    out = _search_code("SECRET_TOKEN")
    assert "没有命中" in out


def test_list_dir_one_level(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "in_pkg.py").write_text("x", encoding="utf-8")
    (tmp_path / "top.py").write_text("y", encoding="utf-8")

    out = _list_dir(".")
    assert "pkg/" in out and "top.py" in out
    assert "in_pkg.py" not in out           # 只列一层


def test_write_file_new_and_diff(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)

    # 新建
    out = _write_file("new.py", "print('v1')")
    assert "已新建" in out

    # 覆盖 → 必返 diff
    out = _write_file("new.py", "print('v2')")
    assert "已覆盖" in out
    assert "-print('v1')" in out and "+print('v2')" in out

    # 内容相同 → 不写
    assert "完全相同" in _write_file("new.py", "print('v2')")


def test_write_file_respects_fence(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.files.WORKSPACE_ROOT", tmp_path)
    with pytest.raises(ValueError, match="敏感"):
        _write_file(".env", "STOLEN=1")


# ---------- 注册与分级 ----------

def test_file_tools_registered_with_levels():
    registry = ToolRegistry()
    register_file_tools(registry)
    schemas = {s["function"]["name"] for s in registry.schemas()}
    assert {"read_file", "search_code", "list_dir", "write_file"} <= schemas


def test_builtin_split_menu_unchanged():
    # 拆分回归：kb/llm/history 全缺席时，register_builtin 聚合菜单 =
    # time 1 件 + notes 3 件（条件注册的两对缺席）= 4 件，与拆分前语义一致
    from pathlib import Path as P

    from agent.tools.builtin import register_builtin
    from agent.tools.context import ToolContext

    registry = ToolRegistry()
    register_builtin(registry, ToolContext(notes_dir=P("data/notes")))
    assert set(registry.names()) == {
        "get_current_time", "list_notes", "read_notes", "write_note",
    }
