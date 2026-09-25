"""044 记忆层 MCP 只读服务器验收：钉住三条判定标准（离线、子进程真实往返）。

不变量：
- 只读是**可验证的**：tools/list 只有两个工具，打 memory_add 拿 isError 且盘上无变化
- 读侧单份真值：MCP 读到的行与内部 `read_learned` 逐行一致（同一份解析）
- 白名单单份真值：memory_list_categories 报的 category/scope 就是 consolidate 的常量
- 清单里 memory 条目「挂名不拉」（enabled=false），且脚本路径相对仓库根可解析
"""

import json
import sys
from pathlib import Path

import pytest

from agent.memory.consolidate import CATEGORIES, SCOPES
from agent.memory.learned import read_learned
from agent.paths import WORKSPACE_ROOT
from agent.tools.mcp_client import McpCallError, McpClient, register_mcp_tools
from agent.tools.mcp_config import load_server_specs
from agent.tools.registry import ToolRegistry

SERVER = Path(__file__).resolve().parents[1] / "servers" / "memory_server.py"

DECISION_LINE = "- [2026-09-25] 记忆层对外只读，写路径挂 tombstone 信号"
USER_LINE = "- [2026-09-20] [已验证] 用户偏好：回答要短"


@pytest.fixture
def memory_dirs(tmp_path):
    """造两个作用域的落盘物：项目级三桶之一 + 仓库外用户记忆。"""
    learned = tmp_path / "learned"
    learned.mkdir()
    (learned / "decisions.md").write_text(DECISION_LINE + "\n", encoding="utf-8")
    user = tmp_path / "user.md"
    user.write_text(USER_LINE + "\n", encoding="utf-8")
    return learned, user


@pytest.fixture
def client(memory_dirs):
    learned, user = memory_dirs
    c = McpClient(
        [sys.executable, str(SERVER)],
        env={
            "MCP_LEARNED_DIR": str(learned),        # 服务器侧的测试注入点
            "CORTEX_USER_MEMORY": str(user),        # paths.user_memory_path() 的现成覆写点
        },
    )
    yield c
    c.close()


def test_tools_list_is_exactly_two_readonly(client):
    """判定标准 2 的前半：写路径不在菜单上。"""
    names = {t["name"] for t in client.list_tools()}
    assert names == {"memory_recall", "memory_list_categories"}
    assert all("description" in t and "inputSchema" in t for t in client.list_tools())


def test_write_path_does_not_exist(client, memory_dirs):
    """判定标准 2 的后半：打 memory_add 拿 isError，且两个作用域的文件字节不变。"""
    learned, user = memory_dirs
    before = (learned / "decisions.md").read_bytes(), user.read_bytes()
    with pytest.raises(McpCallError, match="未知工具"):
        client.call_tool("memory_add", {"content": "外部写入", "category": "decisions"})
    assert (learned / "decisions.md").read_bytes() == before[0]
    assert user.read_bytes() == before[1]
    assert list(learned.iterdir()) == [learned / "decisions.md"]   # 没被顺手建新桶


def test_recall_returns_both_scopes_and_matches_internal_reader(client, memory_dirs):
    """判定标准 3：MCP 读到的与内部 read_learned 读到的是同一份。"""
    text = client.call_tool("memory_recall", {})
    assert "记忆层对外只读" in text
    assert "[已验证] 用户偏好：回答要短" in text   # verified 前缀原样带出，不另造字段

    learned, user = memory_dirs
    internal = [e.content for e in read_learned(learned / "decisions.md")]
    internal += [e.content for e in read_learned(user)]
    for content in internal:
        assert content in text


def test_recall_filters_by_scope_category_and_query(client):
    assert "用户偏好" not in client.call_tool("memory_recall", {"scope": "project"})
    assert "记忆层对外只读" not in client.call_tool("memory_recall", {"scope": "user"})
    assert "记忆层对外只读" in client.call_tool(
        "memory_recall", {"category": "decisions"}
    )
    # 空桶跳过（constraints/other 没建文件）+ query 子串过滤
    assert client.call_tool("memory_recall", {"category": "constraints"}) == "（无匹配的记忆条目）"
    assert client.call_tool("memory_recall", {"query": "TOMBSTONE"}) != "（无匹配的记忆条目）"
    assert client.call_tool("memory_recall", {"query": "不存在的词"}) == "（无匹配的记忆条目）"


def test_recall_rejects_illegal_scope(client):
    """白名单外的参数值是业务错误（isError），不是静默返回全部。"""
    with pytest.raises(McpCallError, match="scope 非法"):
        client.call_tool("memory_recall", {"scope": "galaxy"})
    with pytest.raises(McpCallError, match="category 非法"):
        client.call_tool("memory_recall", {"category": "secrets"})


def test_list_categories_reports_internal_whitelist(client):
    payload = json.loads(client.call_tool("memory_list_categories", {}))
    assert payload["categories"] == list(CATEGORIES)
    assert payload["scopes"] == list(SCOPES)
    assert "未暴露" in payload["write"]   # 只读口径自述，外部 harness 不用猜


def test_registry_mounts_prefixed_readonly_tools(client):
    """判定标准 1：agent 自己挂载后能点菜（「内部不消费」≠「内部挂不上」）。"""
    registry = ToolRegistry()
    register_mcp_tools(registry, client)
    assert "mcp__memory_recall" in registry.names()
    assert "mcp__memory_add" not in registry.names()
    assert "decisions" in registry.execute("mcp__memory_list_categories", "{}")
    # 业务错误经注册表转错误字符串（模型自我纠正的反馈环），不抛到主循环
    bad = registry.execute("mcp__memory_recall", json.dumps({"scope": "galaxy"}))
    assert bad.startswith("错误")


def test_repo_manifest_lists_memory_server_as_disabled():
    """清单里的 memory 条目：挂名不拉（不给每轮启动添一个子进程），脚本路径可达。"""
    specs = {s.name: s for s in load_server_specs(WORKSPACE_ROOT / "mcp_servers.json")}
    assert "memory" in specs
    assert specs["memory"].enabled is False
    assert specs["memory"].prefix == "memory__"
    script = specs["memory"].command[-1]
    assert (WORKSPACE_ROOT / script).is_file()   # 相对路径锚仓库根（S8a 边界①）
