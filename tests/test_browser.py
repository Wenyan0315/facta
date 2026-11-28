"""浏览器冒烟（ADR 094 R08 / 118）：关键链路在真浏览器里过一遍。

- 聊天页加载，环境徽章可见（093 的 /api/status → sidebar 链路）；
- fork 按钮点击后会话清单 +1（093 把 085 的 fork 接进界面）；
- memory 面板加载出分组标题（fw 构建产物真被伺服）；
- 任务面板委派 → 待确认卡 → 批准 → done（118 验收钉子①，另起带技能的服务）。

playwright 未装或 chromium 缺席时整模块 skip——本地不挡三门；
CI 显式 `playwright install --with-deps chromium`，全部恒跑。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright 未装：浏览器冒烟跳过")

from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# 子进程引导脚本：最小 AppContext（与 test_app._make_ctx 同款最小面，
# 第二次出现，不抽共享模块）——冒烟只过 HTTP 壳，不需要 assemble 的重副作用。
_BOOT = """
import sys
from pathlib import Path

import uvicorn

from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext, ensure_persona
from facta.server.app import create_app
from facta.tools.registry import ToolRegistry

root = Path(sys.argv[1])


def build_agent(session, repo_root=None):
    agent = Agent(name="browser-smoke", system_prompt="测试人设", registry=ToolRegistry())
    ensure_persona(session, agent)
    return agent


ctx = AppContext(
    provider="mock",
    ledger=None,
    embedder=None,
    llm=ScriptedLLM([Message(role="assistant", content="你好！")]),
    internal_llm=ScriptedLLM([]),
    kb=None,
    store=SessionStore(root / "sessions"),
    build_agent=build_agent,
    todos=TodoStore(root / "todos.json"),
)
uvicorn.run(create_app(ctx), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """起一台真服务器（子进程 uvicorn，数据根指 tmp）——冒烟要过真 HTTP 与真静态文件。"""
    root = tmp_path_factory.mktemp("browser")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _BOOT, str(root), str(port)],
        env={
            **os.environ,
            "FACTA_DATA_DIR": str(root),
            "FACTA_USER_MEMORY": str(root / "user.md"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):   # 最多等 ~10s
        if proc.poll() is not None:
            raise RuntimeError(f"服务器子进程提前退出：\n{proc.stdout.read() if proc.stdout else ''}")
        try:
            if urllib.request.urlopen(f"{base}/api/status", timeout=1).status == 200:
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("服务器子进程 10s 未就绪")
    try:
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium 未安装（playwright install chromium）：{e}")
        yield b
        b.close()


def test_chat_page_shows_env_badge(server, browser):
    page = browser.new_page()
    page.goto(server)
    page.wait_for_function(
        "document.querySelector('#env-badge').textContent.includes('沙箱')"
    )
    assert "确认" in page.inner_text("#env-badge")
    page.close()


def test_fork_button_adds_session_copy(server, browser):
    urllib.request.urlopen(urllib.request.Request(f"{server}/api/sessions", method="POST"))
    page = browser.new_page()
    page.goto(server)
    page.wait_for_selector("#session-list li button[title^='fork']", state="attached")
    before = len(page.query_selector_all("#session-list li"))
    page.hover("#session-list li")   # 行内操作按钮 hover 才显示（style.css 有意设计）
    page.click("#session-list li button[title^='fork']")
    page.wait_for_function(
        f"document.querySelectorAll('#session-list li').length === {before + 1}"
    )
    page.close()


def test_memory_page_lists_groups(server, browser):
    page = browser.new_page()
    page.goto(f"{server}/memory")
    page.wait_for_selector("h3.memory-group-title")
    assert "决定与理由" in page.inner_text("body")
    page.close()


def test_mcp_page_loads_manifest(server, browser):
    """106 第五入口：MCP 面板加载出清单信息（断言常驻文案，不依赖清单内容）。"""
    page = browser.new_page()
    page.goto(f"{server}/mcp")
    page.wait_for_function("document.querySelector('#mcp-root').textContent.includes('清单文件')")
    page.close()


def test_ide_context_chip_appears_and_can_be_dropped(server, browser):
    """ADR 107：壳投递 → focus 拉取 → chip 可见 → 点「×」后服务端信箱也空。

    拉取时机是 focus 而非轮询（全站零轮询口径），所以必须真点一下输入框。
    """
    urllib.request.urlopen(urllib.request.Request(
        f"{server}/api/ide/context",
        data=json.dumps({"path": "demo.py", "selection": "a = 1\nb = 2"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    ))
    page = browser.new_page()
    page.goto(server)
    page.click("#input")
    page.wait_for_function("!document.querySelector('#ide-ctx').classList.contains('hidden')")
    chip = page.inner_text("#ide-ctx")
    assert "demo.py" in chip and "选中 2 行" in chip

    page.click("#ide-ctx button")
    page.wait_for_function("document.querySelector('#ide-ctx').classList.contains('hidden')")
    # UI 先清、DELETE 后发（chip 一消失这一轮就不认领了）⇒ 轮询到服务端真空
    for _ in range(50):   # 最多 5s
        if json.loads(urllib.request.urlopen(f"{server}/api/ide/context").read()) is None:
            break
        time.sleep(0.1)
    else:
        pytest.fail("点「×」后服务端信箱未清空")
    page.close()


# ---------- ADR 118：任务面板委派全链路（另起一台带技能表的服务器） ----------
#
# 与上面三条不共用 fixture：ScriptedLLM 的脚本序列是一次性的，混用会互相吃光；
# 而且委派链路要 write 档技能 + 真写工具，最小 ctx 面不同。
_BOOT_TASKS = """
import json
import sys
from pathlib import Path

import uvicorn

from facta.core.audit import AuditLog
from facta.core.llm import ScriptedLLM
from facta.core.types import Message
from facta.memory.store import SessionStore
from facta.memory.todos import TodoStore
from facta.orchestrator.agent import Agent
from facta.orchestrator.assemble import AppContext
from facta.server.app import create_app
from facta.skills import (
    AUTH_WRITE,
    SKILL_TOOLS,
    SKILL_WRITE_TOOLS,
    SkillSpec,
    build_skill_agent,
    build_skill_registry,
)
from facta.tools.context import ToolContext
from facta.tools.registry import ToolRegistry

root = Path(sys.argv[1])

_spec = SkillSpec(
    name="demo", system_prompt="浏览器冒烟技能", authorization=AUTH_WRITE,
    tools=frozenset(SKILL_TOOLS | SKILL_WRITE_TOOLS), max_tool_rounds=5, skip_settle=True,
)
_mother = ToolContext(notes_dir=root / "notes", workspace_root=root)
_audit = AuditLog(root / "audit")


def build_skill(session, skill, repo_root, authorization):
    # 复用生产两件（与 assemble._skill_agent_factory 同式），只把 spec 钉成 demo
    registry = build_skill_registry(_mother, _audit, repo_root, authorization or _spec.authorization)
    return build_skill_agent(_spec, registry, session)


ctx = AppContext(
    provider="mock",           # 非生产 provider ⇒ 不触发 093 批准弹窗（面板链路才走得下去）
    ledger=None,
    embedder=None,
    llm=ScriptedLLM([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "write_file",
             "arguments": json.dumps({"path": "hello.txt", "content": "hi"})},
        ]),
        Message(role="assistant", content="写完了"),
    ]),
    internal_llm=ScriptedLLM([]),
    kb=None,
    store=SessionStore(root / "sessions"),
    build_agent=lambda s, repo_root=None: Agent(
        name="browser-smoke", system_prompt="测试人设", registry=ToolRegistry(),
    ),
    todos=TodoStore(root / "todos.json"),
    skills={_spec.name: _spec},
    build_skill_agent=build_skill,
)
uvicorn.run(create_app(ctx), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
"""


@pytest.fixture(scope="module")
def task_server(tmp_path_factory):
    """带 write 档技能的服务 + 一个目标仓目录（委派的写落这里，不碰真仓）。

    P0 绑定校验波及（B1）：repo_root 现在必须是 Git 仓——fixture 地基补
    git init + 首提交（语义不弱化：委派目标仓仍是同一个自定义目录）。
    """
    root = tmp_path_factory.mktemp("browser-tasks")
    (root / "repo").mkdir()
    (root / "repo" / "base.txt").write_text("seed\n", encoding="utf-8")   # 空目录无从首提交
    subprocess.run(["git", "init", "-q"], cwd=root / "repo", check=True)
    subprocess.run(["git", "add", "-A"], cwd=root / "repo", check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root / "repo", check=True,
    )
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _BOOT_TASKS, str(root), str(port)],
        env={
            **os.environ,
            "FACTA_DATA_DIR": str(root),
            "FACTA_USER_MEMORY": str(root / "user.md"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):   # 最多等 ~10s
        if proc.poll() is not None:
            raise RuntimeError(f"服务器子进程提前退出：\n{proc.stdout.read() if proc.stdout else ''}")
        try:
            if urllib.request.urlopen(f"{base}/api/skills", timeout=1).status == 200:
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("任务服务器子进程 10s 未就绪")
    try:
        yield base, root / "repo"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _wait_task_status(page, status: str, tries: int = 10) -> None:
    """等到任务卡转到某状态（最多 tries × 4s）。

    M5 承诺件 b 起面板是 SSE 推送（状态翻转即时可见）；这里每 4s 没等到就
    reload 兜底一次（防断流/丢事件）。注意别「reload 完立刻查 DOM」：数据
    还在飞，查到的永远是「加载中」，会活锁（本测试踩过）。
    """
    sel = f'[data-testid="task-card"][data-status="{status}"]'
    for _ in range(tries):
        try:
            page.wait_for_selector(sel, timeout=4000)
        except PlaywrightTimeout:
            page.reload()
        else:
            return
    pytest.fail(f"任务卡未在 {tries * 4}s 内转到 {status}")


def test_delegate_task_confirm_approve_done(task_server, browser):
    """ADR 118 验收钉子①：委派 → 待确认卡 → 批准 → done，全链路在真浏览器里走完。

    覆盖：§2 委派表单（goal + 技能下拉 + 自定义 repo_root）、§3 面板内嵌确认卡
    （write-auth kind 文案 + 批准键）、以及「任务生命周期在 UI 上可点」本身
    （此前委派只能 curl）。
    """
    base, repo = task_server
    page = browser.new_page()
    page.goto(f"{base}/tasks")
    page.wait_for_selector('[data-testid="new-task-form"]')

    # 表单：goal + 选技能 + 自定义目标仓（不写进 Facta 自己的仓）
    page.fill('[data-testid="new-task-form"] textarea.tp-goal', "写一个 hello.txt")
    page.locator('label:has-text("技能") select').select_option("demo")
    page.locator('label:has-text("项目") select').select_option("__custom__")
    page.fill('input[placeholder="/absolute/path/to/repo"]', str(repo))
    page.click('[data-testid="new-task-form"] button[type="submit"]')

    # write 档首写停 ⇒ 派生态 waiting-approval + 红徽章（117 观察 3/4 的呈现）
    _wait_task_status(page, "waiting-approval")
    assert page.query_selector('[data-testid="pending-badge"]') is not None

    # 展开才订阅 per-Run SSE（订阅数 = 展开数），断线重放语义把挂起的确认弹回来
    page.click('[data-testid="task-card"] .task-head')
    page.wait_for_selector('[data-testid="confirm-card"]')
    note = page.locator('[data-testid="confirm-card"] .tp-kind-note')
    assert note.get_attribute("data-kind") == "write-auth"
    assert "本任务写授权" in note.inner_text()

    page.click('[data-testid="confirm-approve"]')
    _wait_task_status(page, "done")
    assert (repo / "hello.txt").exists()   # 批准真放行了写，不只是状态翻牌
    page.close()
