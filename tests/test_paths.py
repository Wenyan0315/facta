"""ADR 090（R03）：数据目录配置统一——运行时路径推导收口 + 迁移源锚死 + 围栏同源。

不变量：
- 默认布局下推导输出与历史字面量逐字节一致（seatbelt profile / bwrap argv /
  files.py rel 比对零回归面）
- FACTA_DATA_DIR 覆盖 = 全新数据根：待办/审计/向量库等全部运行时路径落在
  临时根，默认 data/ 零引用（不往默认目录产生新数据）
- 迁移源 MEMORY_PATH 锚死默认旧位，不跟随 env（旧布局只存在于仓库内安装）
- 数据根迁出 WORKSPACE_ROOT 时围栏条目转绝对路径，seatbelt deny 跟到真位置
  （tmp 族数据根补上写白名单缺口）
"""
from __future__ import annotations

import importlib

import facta.paths as paths_mod
from facta.orchestrator import assemble
from facta.tools import files, sandbox


def _reload_path_consumers() -> None:
    """DATA_ROOT 是模块加载期结算：env 变更靠重载生效。必须把 paths 与全部
    围栏消费方一起重载——reload 产生新 tuple 对象，漏一个就会打破
    test_sandbox 的「同一对象」断言（它拿旧对象比新对象）。"""
    for mod in (paths_mod, files, sandbox, assemble):
        importlib.reload(mod)


def test_default_layout_byte_identical():
    """默认布局：推导输出 = 旧字面量（围栏/profile 零变化的硬钉）。"""
    assert paths_mod.DATA_ROOT == paths_mod.WORKSPACE_ROOT / "data"
    assert paths_mod.MEMORY_WRITE_FENCE == ("data/notes", "data/learned", "data/graph.json")
    assert paths_mod.BLACKLIST_DIRS == (
        "data/memory", "data/audit", "data/vector_db",
        "servers/sandbox", ".venv", "data/worktrees",
    )
    # 装配层三常量从 DATA_ROOT 推导（087 R03 的「统一从数据根推导」）
    assert assemble.TODOS_PATH == paths_mod.DATA_ROOT / "todos.json"
    assert assemble.AUDIT_DIR == paths_mod.DATA_ROOT / "audit"
    assert assemble.VECTOR_DB_DIR == paths_mod.DATA_ROOT / "vector_db"


def test_data_root_override_relocates_runtime_paths(monkeypatch, tmp_path):
    """env 覆盖后：运行时路径全落临时根、默认 data/ 零引用；迁移源锚死旧位；
    围栏条目转绝对路径并进 seatbelt deny。"""
    monkeypatch.setenv("FACTA_DATA_DIR", str(tmp_path))
    try:
        _reload_path_consumers()

        new_root = tmp_path.resolve()
        assert new_root == paths_mod.DATA_ROOT
        runtime = [
            paths_mod.NOTES_DIR, paths_mod.LEARNED_DIR, paths_mod.SESSIONS_DIR,
            paths_mod.CHECKPOINT_DIR, paths_mod.WORKTREES_DIR, paths_mod.GRAPH_PATH,
            assemble.TODOS_PATH, assemble.AUDIT_DIR, assemble.VECTOR_DB_DIR,
        ]
        assert all(p.is_relative_to(new_root) for p in runtime)
        # 「不往默认目录产生新数据」的推导级钉子：运行时路径与默认 data/ 无交集
        default_data = paths_mod.WORKSPACE_ROOT / "data"
        assert all(not p.is_relative_to(default_data) for p in runtime)
        # 迁移源锚死默认旧位（env 场景无旧数据可迁，migrate 幂等空转）
        assert default_data / "memory" / "session.json" == assemble.MEMORY_PATH

        # 根外数据 → 围栏条目转绝对路径，deny 跟到真位置
        assert str(new_root / "notes") in paths_mod.MEMORY_WRITE_FENCE
        assert str(new_root / "audit") in paths_mod.BLACKLIST_DIRS
        profile = sandbox.build_seatbelt_profile(paths_mod.WORKSPACE_ROOT)
        assert f'(deny file-write* (subpath "{new_root}/notes"))' in profile
        assert f'(deny file-write* (subpath "{new_root}/audit"))' in profile
    finally:
        monkeypatch.undo()
        _reload_path_consumers()
