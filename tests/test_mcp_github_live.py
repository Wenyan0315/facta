"""MCP 真三方验收·GitHub 官方服务器（live network + gh 凭证，默认跳过）。

官方远程 GitHub MCP 服务器对全体 GitHub 用户开放（无需 Copilot 订阅）：
    https://api.githubcopilot.com/mcp/ + Authorization: Bearer <token>
凭证用 `gh auth token` 现取（gh CLI 已登录即可）——不入代码、不入仓库、
不入环境变量文件；本测试跑完即弃。

手动开跑：PYTEST_LIVE_NETWORK=1 .venv/bin/python -m pytest tests/test_mcp_github_live.py -q
"""

import os
import shutil
import subprocess

import pytest

from facta.tools.mcp_http import HttpMcpClient

pytestmark = pytest.mark.skipif(
    os.environ.get("PYTEST_LIVE_NETWORK") != "1",
    reason="真网络验收需 PYTEST_LIVE_NETWORK=1",
)

URL = "https://api.githubcopilot.com/mcp/"


def _gh_token() -> str:
    if shutil.which("gh") is None:
        pytest.skip("未安装 gh CLI")
    result = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, timeout=30,
        check=False,   # 退出码自行判断（未登录 → skip）
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("gh 未登录或取不到 token")
    return result.stdout.strip()


def test_official_github_mcp_acceptance():
    """手写 HTTP 客户端连官方 GitHub MCP：发现 + 只读调用双验收。"""
    token = _gh_token()
    client = HttpMcpClient(
        URL, timeout=60, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        names = {t["name"] for t in client.list_tools()}
        assert len(names) >= 40   # 官方服务器 44 工具量级：清单真的交出来了
        # 用实测见过的工具名（远程版命名与本地版不同——评测断言只信观察）
        assert {"get_me", "create_pull_request", "add_issue_comment"} <= names

        me = client.call_tool("get_me", {})
        assert "login" in me
    finally:
        client.close()
