"""联网工具验收：web_search + fetch_web（2026-09-16，场景「个人助理」核心缺口）。

不变量：
- 条件注册：ctx.web=None → 两工具不上菜单（mock 路径不背联网依赖）
- 栅栏：非 http(s) scheme / 私网地址 / DNS 解析到内网 → ValueError（模型可自纠）
- 截断：摘要 500 字 / 正文 8000 字，防灌爆上下文
"""

import pytest

from agent.tools.context import ToolContext
from agent.tools.registry import ToolRegistry
from agent.tools.web import (
    _fetch_web,
    _parse_bocha,
    _web_search,
    get_web_search,
    register_web_tools,
)


class _FakeSearch:
    """假搜索 client：测试注入，不真联网。"""

    def __init__(self, results=None):
        self.results = results or []
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.results


def _ctx(tmp_path, web=None) -> ToolContext:
    return ToolContext(notes_dir=tmp_path, web=web)


def test_web_tools_not_registered_without_client(tmp_path):
    registry = ToolRegistry()
    register_web_tools(registry, _ctx(tmp_path))
    assert "web_search" not in registry.names()
    assert "fetch_web" not in registry.names()


def test_web_tools_registered_with_client(tmp_path):
    registry = ToolRegistry()
    register_web_tools(registry, _ctx(tmp_path, _FakeSearch()))
    assert "web_search" in registry.names()
    assert "fetch_web" in registry.names()
    # schema 有 query/url 必填项（结构层校验的原料）
    schemas = {s["function"]["name"]: s for s in registry.schemas()}
    assert "query" in schemas["web_search"]["function"]["parameters"]["required"]
    assert "url" in schemas["fetch_web"]["function"]["parameters"]["required"]


def test_web_search_formats_results(tmp_path):
    client = _FakeSearch([
        {"title": "上海天气", "url": "https://weather.example/shanghai",
         "content": "晴，23 度，东风 3 级。"},
        {"title": "天气预报网", "url": "https://weather.example",
         "content": "x" * 600},   # 超长摘要 → 截断
    ])
    out = _web_search(client, "上海今天天气")
    assert "上海天气" in out and "23 度" in out
    assert client.queries == ["上海今天天气"]
    assert "x" * 501 not in out          # 摘要截到 500+…，不含 501 连续段
    assert out.count("weather.example") == 2


def test_web_search_empty_results_hint(tmp_path):
    out = _web_search(_FakeSearch([]), "不存在的东西")
    assert "无结果" in out


# ---------- 博查 Provider（先行实现） ----------

def test_parse_bocha_normalizes_response():
    # data.webPages.value[] → 协议三项；summary 优先 snippet 兜底；日期附尾
    payload = {
        "code": 200,
        "data": {
            "webPages": {
                "value": [
                    {"name": "上海天气", "url": "https://w.example/sh",
                     "summary": "晴 23 度", "snippet": "短摘要", "datePublished": "2026-09-16T08:00:00+08:00"},
                    {"name": "无摘要条目", "url": "https://w.example/2",
                     "snippet": "只剩 snippet"},
                ]
            }
        },
    }
    out = _parse_bocha(payload)
    assert out[0] == {"title": "上海天气", "url": "https://w.example/sh",
                      "content": "晴 23 度（发布：2026-09-16）"}
    assert out[1]["content"] == "只剩 snippet"   # summary 缺失 → snippet 兜底


def test_parse_bocha_empty_payload():
    assert _parse_bocha({}) == []
    assert _parse_bocha({"data": {}}) == []


def test_get_web_search_priority(monkeypatch):
    # 优先级：BOCHA > TAVILY > None（不上菜单）
    from agent.tools.web import BochaSearch, TavilySearch

    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert get_web_search() is None

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert isinstance(get_web_search(), TavilySearch)

    monkeypatch.setenv("BOCHA_API_KEY", "sk-x")
    assert isinstance(get_web_search(), BochaSearch)   # bocha 抢先


# ---------- 栅栏（fetch_web 的 URL 是不可信输入） ----------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",           # 非 http(s) scheme
    "ftp://example.com/file",
    "http://127.0.0.1:8000/api/runs",     # 环回
    "http://localhost:6379/",              # localhost 域名
    "http://192.168.1.1/admin",            # 私网段
    "http://10.0.0.1/",                    # 私网段
    "http://169.254.169.254/latest/meta-data/",  # 云元数据端点（链路本地）
])
def test_fetch_web_rejects_dangerous_urls(url):
    with pytest.raises(ValueError):
        _fetch_web(url)


def test_fetch_web_rejects_domain_resolving_to_private(monkeypatch):
    # 域名字符串无害、但解析到内网 IP → 一样拒绝（只查 hostname 挡不住的绕过）
    class _FakeInfo:
        def __init__(self, addr): self._addr = addr

        def __getitem__(self, i): return self._addr

    monkeypatch.setattr(
        "agent.tools.web.socket.getaddrinfo",
        lambda host, port: [(0, 0, 0, "", ("10.66.66.66", 0))],
    )
    with pytest.raises(ValueError, match="内网"):
        _fetch_web("https://looks-normal.example.com/")

    # 对照：解析到公网 IP 放行到 HTTP 层（用不存在的 TLD 让请求失败，
    # 证明过了栅栏——错误不再是 ValueError）
    monkeypatch.setattr(
        "agent.tools.web.socket.getaddrinfo",
        lambda host, port: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    with pytest.raises(Exception) as exc_info:
        _fetch_web("https://looks-normal.example.com/")
    assert not isinstance(exc_info.value, ValueError)


def test_fetch_web_truncates_long_text(monkeypatch):
    # 假 HTTP 层：返回超长正文 → 截到 8000 + 标记
    import agent.tools.web as web

    class _FakeResp:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def raise_for_status(self): pass

        def iter_bytes(self, n): yield b"<p>" + b"word " * 20000 + b"</p>"

    class _FakeHTTP:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def stream(self, method, url): return _FakeResp()

    class _FakeClient:
        def __init__(self, **kw): self._http = _FakeHTTP()

        def __enter__(self): return self._http

        def __exit__(self, *a): return False

    monkeypatch.setattr(web.httpx, "Client", _FakeClient)
    out = _fetch_web("https://example.com/long")
    # 界碑（S3）包裹后总长 = 8000 正文 + 界碑声明 + 截断标记，上限放宽到 +300
    assert len(out) <= web.MAX_TEXT_CHARS + 300
    assert "已截断" in out
    assert out.startswith("〔以下为外部网络内容") and out.rstrip().endswith("〔外部网络内容结束〕")
