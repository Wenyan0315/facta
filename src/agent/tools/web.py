"""联网工具：web_search + fetch_web（场景「个人助理」的核心缺口，2026-09-16）。

两次真实使用实测失败（「查天气」「股市分析」皆因无实时数据）催生的开门能力。
设计对齐 014 决策：联网属「开门」类，S3 完整栅栏之前随行最低防护。

架构：搜索 Provider 接口 + 实现类（与 LLM/Embedder 供应商化同构，第四次）。
v1 只落 Tavily（免费额度/agent 生态标准/返回已清洗摘要——查天气类一步到位，
省一次 fetch）。双 Provider 路由（中文博查/英文 Tavily）的位置已留好：
触发信号 = Tavily 中文实测不达标时加 BochaProvider + 语言路由（配置表一行）。

安全栅栏（fetch_web 的 URL 是模型/用户给的，不可信输入进网络层之前必须过闸）：
  ① scheme 白名单（http/https，拒 file:// 等）
  ② 私网地址拒绝（DNS 解析后逐 IP 检查——只查 hostname 字符串挡不住
     「evil.com 解析到 127.0.0.1」的绕过；DNS rebinding TOCTOU 是已知
     边界，个人工具 v1 接受，S3 完整栅栏再议）
  ③ 只读 GET
  ④ 超时（搜索 10s / 抓取 15s）+ 响应大小上限 2MB + 正文截 8000 字
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Protocol
from urllib.parse import urlparse

import httpx

from agent.tools.context import ToolContext
from agent.tools.registry import Tool, ToolRegistry

SEARCH_TIMEOUT = 10.0
FETCH_TIMEOUT = 15.0
MAX_BYTES = 2 * 1024 * 1024     # 响应大小上限：防内存炸弹
MAX_TEXT_CHARS = 8000           # 正文截断：防灌爆上下文（与 read_note 同量级纪律）

TAVILY_API_URL = "https://api.tavily.com/search"

_SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词。用于实时信息（天气/新闻/股价/近期事件）或个人知识库没有的内容；中文提问用中文搜索通常效果更好。",
        }
    },
    "required": ["query"],
}
_FETCH_PARAMS = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "要读取全文的网页 URL（http/https）。当 web_search 的摘要不够、需要深入某个结果时使用。",
        }
    },
    "required": ["url"],
}


class WebSearchClient(Protocol):
    """搜索 Provider 协议：一次查询 → 结构化结果列表。

    返回项约定：{"title": str, "url": str, "content": str}——content 是
    已清洗的摘要（Tavily 原生提供；博查届时在实现类里做同样清洗）。
    """

    def search(self, query: str) -> list[dict]:
        ...


class TavilySearch:
    """Tavily 实现：httpx 手写 REST（不引官方 SDK——POST/JSON 两行的 API
    不值得一个依赖，与手写 MCP 客户端同哲学）。key 从环境变量来，绝不进代码。
    """

    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def search(self, query: str) -> list[dict]:
        resp = httpx.post(
            TAVILY_API_URL,
            headers={"Authorization": f"Bearer {self._key}"},
            json={"query": query, "max_results": 5, "search_depth": "basic"},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
            for r in resp.json().get("results", [])
        ]


BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"


def _parse_bocha(payload: dict) -> list[dict]:
    """博查响应归一化：data.webPages.value[] → 协议约定的三项。

    纯函数单测的原料——各家响应形状不同（Tavily 平铺 results、博查嵌套
    webPages），归一化收在实现类里，协议消费方（工具本体）零感知。
    summary（长摘要）优先、snippet 兜底；datePublished 附进 content 尾部
    （时效信息对天气/新闻类查询是关键证据）。
    """
    items = payload.get("data", {}).get("webPages", {}).get("value", [])
    out = []
    for r in items:
        content = (r.get("summary") or r.get("snippet") or "").strip()
        date = r.get("datePublished", "")
        if date:
            content = f"{content}（发布：{date[:10]}）" if content else f"发布：{date[:10]}"
        out.append({"title": r.get("name", ""), "url": r.get("url", ""), "content": content})
    return out


class BochaSearch:
    """博查实现（2026-09-16 先行）：中文搜索质量好、国内直连。

    summary=True 要长摘要（博查特色：比 snippet 详细，对 LLM 更友好）；
    freshness=noLimit 不限时间——时间过滤的决策权留给模型（它知道用户
    问的是「今天天气」还是「历史事件」），触发信号=模型常带时间词查询
    却拿不到新结果时，再把 freshness 暴露成工具参数。
    """

    name = "bocha"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def search(self, query: str) -> list[dict]:
        resp = httpx.post(
            BOCHA_API_URL,
            headers={"Authorization": f"Bearer {self._key}"},
            json={"query": query, "count": 5, "summary": True, "freshness": "noLimit"},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_bocha(resp.json())


def get_web_search() -> "WebSearchClient | None":
    """搜索 client 工厂（组装层唯一真值源的 web 版）。

    优先级：BOCHA_API_KEY > TAVILY_API_KEY > None（不上菜单）。
    双路由（中文博查/英文 Tavily，按查询语言分发）是协议的一个包装实现，
    触发信号 = 两家 key 齐且实测出语言偏好差异时再加，工厂签名不变。
    """
    bocha_key = os.environ.get("BOCHA_API_KEY", "")
    if bocha_key:
        return BochaSearch(bocha_key)
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if tavily_key:
        return TavilySearch(tavily_key)
    return None


def _assert_public_http_url(url: str) -> None:
    """栅栏①②：scheme 白名单 + 私网地址拒绝（解析后逐 IP 检查）。

    坏 URL 以 ValueError 抛出——工具错误经 registry 变错误字符串回给模型，
    模型可自我纠正（换 URL），不炸会话。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"只支持 http/https，拒绝：{parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")

    try:
        ips = {ipaddress.ip_address(host)}
    except ValueError:   # 是域名：解析全部 A/AAAA 记录再逐个查
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ValueError(f"域名解析失败：{host}") from exc
        ips = {ipaddress.ip_address(info[4][0]) for info in infos}

    for ip in ips:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"拒绝访问内网/保留地址：{host}（解析到 {ip}）")


def _web_search(client: WebSearchClient, query: str) -> str:
    """web_search 工具本体：client 由闭包注入（依赖注入老姿势）。"""
    results = client.search(query)
    if not results:
        return "搜索无结果，可换关键词重试"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        content = r.get("content", "").strip()
        if len(content) > 500:
            content = content[:500] + "…"
        lines.append(f"{i}. {title}\n   {r.get('url', '')}\n   {content}")
    return "\n".join(lines)


def _fetch_web(url: str) -> str:
    """fetch_web 工具本体：GET → html2text → 截断。"""
    _assert_public_http_url(url)

    # 大小上限：流式读，超限即停（不把 2MB+ 的响应整个搬进内存再截）
    chunks: list[bytes] = []
    total = 0
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as http:
        with http.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BYTES:
                    break
    html = b"".join(chunks).decode("utf-8", errors="replace")

    try:
        import html2text   # 延迟导入：缺依赖时报清晰错误而非 import 期炸整包
    except ImportError as exc:
        raise ImportError("fetch_web 需要 html2text：pip install -e '.[rag]'") from exc
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True   # 图片 URL 对 LLM 是噪声
    converter.body_width = 0         # 不硬换行（markdown 源码交给模型）
    text = converter.handle(html).strip()

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n\n〔正文超长，已截断〕"
    return text or "（页面无有效正文）"


def register_web_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    """条件注册：ctx.web 缺席（无 key 的 mock 路径）→ 两工具不上菜单。

    与 kb=None 不注册 search_notes 同一语义：假模型路径不背联网依赖。
    """
    if ctx.web is None:
        return

    registry.register(Tool(
        name="web_search",
        description="联网搜索：获取实时信息（天气、新闻、股价、近期事件）或个人知识库没有的内容。返回若干条结果的标题、链接与摘要。",
        parameters=_SEARCH_PARAMS,
        func=lambda query: _web_search(ctx.web, query),
    ))
    registry.register(Tool(
        name="fetch_web",
        description="读取网页全文（转成 Markdown）。当 web_search 的摘要不够、需要某个链接的完整内容时使用。",
        parameters=_FETCH_PARAMS,
        func=_fetch_web,
    ))
