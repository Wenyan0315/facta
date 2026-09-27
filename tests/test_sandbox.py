"""048 run_command 沙箱测试：写围栏正反对称 / git 闭环 / TMPDIR / 网络 /
子进程继承 / 降级零行为差 / files.py 黑名单同源交叉断言。

seatbelt 实跑类仅在 macOS + sandbox-exec 在场时跑（CI/降级环境自动 skip，
降级路径由 test_degraded_* 覆盖）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent import paths as paths_mod
from agent.core.audit import AuditLog
from agent.tools import files as files_mod
from agent.tools import sandbox
from agent.tools.registry import Tool, ToolRegistry
from agent.tools.sandbox import build_seatbelt_profile, detect_backend, wrap_command
from agent.tools.terminal import _run_command

HAS_SEATBELT = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
seatbelt_only = pytest.mark.skipif(not HAS_SEATBELT, reason="需要 macOS sandbox-exec")

# pytest tmp_path 在 macOS 常含 /var→/private/var symlink；seatbelt 按真实
# 路径匹配，root 必须 resolve 后才与 profile 锚点对齐（调用方责任，钉在
# wrap_command docstring——这里每个测试都用 resolve 后的 root）。
def _root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def _init_repo_outside_sandbox(root: Path) -> None:
    """测试夹具在围栏外建仓：git init/clone 要写 .git/config 和模板 hooks——
    048 围栏内这两个口被围死（与 git config 被拒同一机制），init/clone
    在沙箱里跑不了属丙案代价，已登记 ADR 048 补注；日常闭环=已有仓库里
    add/commit，不受影响（下面两个测试正是验这个）。"""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


# ── 1. 后端检测 ──────────────────────────────────────────────

def test_detect_backend_env_off(monkeypatch):
    monkeypatch.setenv("CORTEX_SANDBOX", "off")
    assert detect_backend() is None


def test_detect_backend_matches_platform():
    expected = "seatbelt" if HAS_SEATBELT else None
    assert detect_backend() == expected


# ── 2. wrap_command 形态 ─────────────────────────────────────

def test_wrap_command_off_returns_none(monkeypatch):
    monkeypatch.setattr(sandbox, "detect_backend", lambda: None)
    argv, backend = wrap_command("echo hi", root=Path("/tmp"))
    assert argv is None and backend == "off"


@seatbelt_only
def test_wrap_command_seatbelt_argv():
    argv, backend = wrap_command("echo hi", root=Path("/tmp"))
    assert backend == "seatbelt"
    assert argv is not None
    assert argv[0] == "sandbox-exec" and argv[1] == "-p"
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


# ── 3. profile × files.py 黑名单同源交叉（漂移即红）──────────

def test_profile_blacklist_covers_files_py():
    profile = build_seatbelt_profile(Path("/ws"))
    # 目录黑名单：files.py 每一项都有对应 deny subpath
    for d in files_mod._BLACKLIST_DIRS:
        assert f'(deny file-write* (subpath "/ws/{d}"))' in profile, d
    # .env 语义对齐 files.py：根级前缀 + 任意深度文件/目录
    assert '/\\.env' in profile
    # .git 收窄是有意裁定（048 丙案）：files.py 管应用层读（全 .git 挡），
    # 沙箱管进程级写——只围死 hooks/config 两个毒化入口，保日常 commit 闭环
    assert '/\\.git/hooks/' in profile
    assert '/\\.git/config$' in profile
    # 顺序：seatbelt 同一 operation 后定义胜出——所有 deny 必须在所有
    # allow 之后，否则黑名单被写白名单盖掉
    assert profile.rindex("(deny") > profile.rindex("(allow")


def test_profile_memory_write_fence_single_source():
    """052：记忆写围栏三处（notes/learned/graph.json）都进 deny，且 files.py
    与 sandbox.py 用的是 paths.py 那一个对象——各写一份字面量就是 P1-3 的老病。"""
    profile = build_seatbelt_profile(Path("/ws"))
    for d in paths_mod.MEMORY_WRITE_FENCE:
        assert f'(deny file-write* (subpath "/ws/{d}"))' in profile, d
    assert files_mod.MEMORY_WRITE_FENCE is paths_mod.MEMORY_WRITE_FENCE
    assert sandbox.MEMORY_WRITE_FENCE is paths_mod.MEMORY_WRITE_FENCE
    # 围栏只作用于写：读侧全放（049 只围 .env 一族），语料必须读得到
    assert "(deny file-read* (subpath" not in profile


# ── 4. seatbelt 实跑：写围栏正反对称 ─────────────────────────

@seatbelt_only
def test_write_fence_allows_root_denies_home(tmp_path):
    root = _root(tmp_path)
    inside = root / "ok.txt"
    r = _run_command(f"touch {inside}", root=root)
    assert "exit code: 0" in r and inside.exists()

    home_target = Path.home() / f".cortex_sb_fence_{os.getpid()}"
    try:
        r = _run_command(f"touch {home_target}", root=root)
        assert "Operation not permitted" in r
        assert not home_target.exists()
    finally:
        home_target.unlink(missing_ok=True)


@seatbelt_only
def test_env_files_denied(tmp_path):
    root = _root(tmp_path)
    for name in (".env", ".env.example"):
        r = _run_command(f"echo x > {root / name}", root=root)
        assert "Operation not permitted" in r, name
        assert not (root / name).exists()


@seatbelt_only
def test_blacklist_dir_denied(tmp_path):
    root = _root(tmp_path)
    (root / "data" / "memory").mkdir(parents=True)
    r = _run_command(f"echo x > {root / 'data' / 'memory' / 'f'}", root=root)
    assert "Operation not permitted" in r


@seatbelt_only
def test_memory_write_denied_read_allowed(tmp_path):
    """052 核心（实跑断言，不只断 profile 文本）：记忆资产写/追加/删除全 EPERM，
    读与非记忆写照常——i6 的 bash 臂规避链（自写脚本落盘 data/notes）走不通。"""
    root = _root(tmp_path)
    notes = root / "data" / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text("hi")
    (root / "data" / "graph.json").write_text("{}")

    # 读侧放行（围栏只作用于写）
    assert "hi" in _run_command("cat data/notes/a.md", root=root)

    r = _run_command("echo poison > data/notes/x.md", root=root)
    assert "Operation not permitted" in r and not (notes / "x.md").exists()
    r = _run_command("echo poison >> data/notes/a.md", root=root)
    assert "Operation not permitted" in r and (notes / "a.md").read_text() == "hi"
    r = _run_command("rm data/notes/a.md", root=root)
    assert "Operation not permitted" in r and (notes / "a.md").exists()
    r = _run_command("echo {} > data/graph.json", root=root)
    assert "Operation not permitted" in r
    assert (root / "data" / "graph.json").read_text() == "{}"
    # 目录不在场也挡（规则按路径匹配，不需要目录存在）
    assert "Operation not permitted" in _run_command(
        "mkdir -p data/learned && echo x > data/learned/f.md", root=root,
    )
    # i6 实际观测到的形状：先写脚本到 /tmp（可写），再用解释器落盘 notes
    r = _run_command(
        f"{sys.executable} -c \"open('data/notes/p.md','w').write('poison')\"", root=root,
    )
    assert "Operation not permitted" in r and not (notes / "p.md").exists()
    # 正对照：非记忆路径不误伤
    r = _run_command("echo ok > data/other.md", root=root)
    assert "exit code: 0" in r and (root / "data" / "other.md").exists()


@seatbelt_only
def test_git_hooks_denied(tmp_path):
    root = _root(tmp_path)
    _init_repo_outside_sandbox(root)
    hook = root / ".git" / "hooks" / "pre-commit"
    r = _run_command(f"echo evil > {hook}", root=root)
    assert "Operation not permitted" in r
    assert not hook.exists()


@seatbelt_only
def test_git_config_denied_but_commit_loop_works(tmp_path):
    """048 丙案核心：毒化入口围死 + 日常 commit 闭环保留。"""
    root = _root(tmp_path)
    _init_repo_outside_sandbox(root)
    # config 写被拒（git 先写 config.lock 再 rename，rename 目标被 deny），
    # 非零退出如实回传，原 config 未被污染
    r = _run_command("git config user.name evil", root=root)
    assert "exit code: 0" not in r
    assert "evil" not in (root / ".git" / "config").read_text()
    # -c 参数不写 config 文件 → commit 闭环在围栏内照常跑通
    (root / "a.txt").write_text("hi")
    r = _run_command(
        "git add a.txt && git -c user.name=t -c user.email=t@t commit -qm init",
        root=root,
    )
    assert "exit code: 0" in r, r
    assert "init" in _run_command("git log --oneline", root=root)


@seatbelt_only
def test_tmpdir_writable(tmp_path):
    marker = f"cortex_sb_tmp_{os.getpid()}"
    r = _run_command(f'touch "$TMPDIR/{marker}" && echo ok', root=_root(tmp_path))
    assert "ok" in r
    _run_command(f'rm -f "$TMPDIR/{marker}"', root=_root(tmp_path))


@seatbelt_only
def test_network_allowed(tmp_path):
    """048 裁定：网络放行（网络维度另案）——本地回环通了即证未误伤。"""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"sandbox-net-ok")

        def log_message(self, *args):  # 静音
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    try:
        r = _run_command(f"curl -s http://127.0.0.1:{srv.server_port}/", root=_root(tmp_path))
        assert "sandbox-net-ok" in r
    finally:
        t.join(timeout=5)
        srv.server_close()


@seatbelt_only
def test_child_process_inherits_fence(tmp_path):
    """fork/exec 全继承：sh 套 sh（curl x | sh 同款链）逃不出围栏。"""
    root = _root(tmp_path)
    home_target = Path.home() / f".cortex_sb_nest_{os.getpid()}"
    try:
        r = _run_command(f"sh -c 'touch {home_target}'", root=root)
        assert "Operation not permitted" in r
        assert not home_target.exists()
    finally:
        home_target.unlink(missing_ok=True)


# ── 5. 降级：无后端零行为差 ──────────────────────────────────

def test_degraded_behaves_like_before(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "detect_backend", lambda: None)
    root = _root(tmp_path)
    inside = root / "ok.txt"
    r = _run_command(f"touch {inside}", root=root)
    assert "exit code: 0" in r and inside.exists()
    # 降级=现状：写 home 也放行（危险面由确认缝兜住，同 048 之前）
    home_target = Path.home() / f".cortex_sb_off_{os.getpid()}"
    try:
        r = _run_command(f"touch {home_target}", root=root)
        assert "exit code: 0" in r and home_target.exists()
    finally:
        home_target.unlink(missing_ok=True)


# ── 6. 审计打标：sandbox 字段（批准拒绝都带）─────────────────

def test_audit_sandbox_field(tmp_path):
    audit = AuditLog(tmp_path / "audit")
    reg = ToolRegistry(audit)
    reg.register(Tool(
        name="run_command", description="t", parameters={}, func=lambda command: "ok",
        needs_confirmation=lambda args: True, sandboxed=True,
    ))
    reg.register(Tool(name="read_file", description="t", parameters={}, func=lambda: "ok"))
    reg.execute("run_command", '{"command": "ls"}', confirm=lambda n, a: True)   # 批准
    reg.execute("run_command", '{"command": "ls"}', confirm=lambda n, a: False)  # 拒绝
    reg.execute("read_file", "{}")
    events = audit.read()
    assert events[0]["sandbox"] in ("seatbelt", "off")   # 批准带标
    assert events[1]["sandbox"] in ("seatbelt", "off")   # 拒绝也带标
    assert "sandbox" not in events[2]                    # 非沙箱工具零行为差


# ── 7. 读围栏（049）：凭证读了就等于泄漏 ─────────────────────

def test_profile_read_deny_symmetric_with_write():
    """.env 读 deny 与写 deny 同形状，且必须排在 (allow file-read*) 之后
    （seatbelt 同 operation 后定义胜出——放前面会被 allow 盖掉）。"""
    profile = build_seatbelt_profile(Path("/ws"))
    assert '(deny file-read* (regex "^/ws/\\.env"))' in profile
    assert '(deny file-read* (regex "/\\.env$"))' in profile
    assert '(deny file-read* (regex "/\\.env/"))' in profile
    assert profile.index("(deny file-read*") > profile.index("(allow file-read*)")


@seatbelt_only
def test_env_read_denied(tmp_path):
    """实跑断言（不只断 profile 文本）：cat 拿不到 .env 内容。"""
    root = _root(tmp_path)
    (root / ".env").write_text("SECRET=canary-DO-NOT-LEAK")
    (root / "sub").mkdir()
    (root / "sub" / ".env").write_text("SECRET=canary-nested")
    r = _run_command("cat .env", root=root)
    assert "Operation not permitted" in r
    assert "canary" not in r
    r = _run_command("cat sub/.env", root=root)
    assert "Operation not permitted" in r
    assert "canary" not in r
    # 正对照：普通文件照读——读围栏没误伤日常工作流
    (root / "README.md").write_text("hello")
    assert "hello" in _run_command("cat README.md", root=root)


@seatbelt_only
def test_env_read_deny_is_zero_friction_for_python(tmp_path):
    """零摩擦结论的地面真值：open() 抛 PermissionError 但解释器跑完，
    load_dotenv() 静默返回 False 不炸（本项目 13 处 load_dotenv 全在主进程、
    沙箱内零处直接 open('.env')，故这条围栏不改变任何现有行为）。"""
    root = _root(tmp_path)
    (root / ".env").write_text("SECRET=canary-DO-NOT-LEAK")
    (root / "probe.py").write_text(
        "from dotenv import load_dotenv\n"
        "print('dotenv:', load_dotenv())\n"
        "try:\n"
        "    open('.env').read()\n"
        "    print('open: leaked')\n"
        "except PermissionError:\n"
        "    print('open: PermissionError')\n"
    )
    r = _run_command(f"{sys.executable} probe.py", root=root)
    assert "exit code: 0" in r, r
    assert "dotenv: False" in r
    assert "open: PermissionError" in r
    assert "canary" not in r
