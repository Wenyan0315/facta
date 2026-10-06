"""浏览器冒烟（ADR 094 R08）：三条关键链路在真浏览器里过一遍。

- 聊天页加载，环境徽章可见（093 的 /api/status → sidebar 链路）；
- fork 按钮点击后会话清单 +1（093 把 085 的 fork 接进界面）；
- memory 面板加载出分组标题（fw 构建产物真被伺服）。

playwright 未装或 chromium 缺席时整模块 skip——本地不挡三门；
CI 显式 `playwright install --with-deps chromium`，三条恒跑。
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright 未装：浏览器冒烟跳过")

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


def build_agent(session):
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
