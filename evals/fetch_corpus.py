"""评估语料抓取（混合检索评估的前置）：经项目自己的 MCP 客户端从 Context7 拉文档。

落 evals/corpus/{lib}.md —— 评估专用语料，绝不进 data/notes（线上与评估共语料
是现状契约；2026-09-19 混合检索评估的边界裁定）。主题避开 data/notes 的 AI
概念领地（Preact/FastAPI/SQLite），防语义撞车。

幂等：文件已存在则跳过——语料版本化进 git，变更需显式 --force 重抓。

运行（项目根目录）：
    .venv/bin/python -m evals.fetch_corpus [--force]
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent.tools.mcp_http import HttpMcpClient

CORPUS_DIR = Path(__file__).parent / "corpus"
CTX7_URL = "https://mcp.context7.com/mcp"

# (库 ID, 文件名, 主题列表)：主题即 query-docs 的 query——每次查一个概念，
# 返回约 2-3k 字 markdown 片段；多主题拼接成该库语料（每库 ~1.5 万字）
LIBRARIES = [
    ("/preactjs/preact-www", "preact.md", [
        "useState and hooks", "signals", "virtual dom",
        "components and props", "diffing algorithm", "react compatibility",
    ]),
    ("/websites/fastapi_tiangolo", "fastapi.md", [
        "dependency injection", "pydantic models", "middleware",
        "background tasks", "async await", "automatic api docs",
    ]),
    ("/websites/sqlite_docs", "sqlite.md", [
        "transactions", "indexes", "wal mode",
        "data types", "json functions", "triggers",
    ]),
]


def fetch_library(client: HttpMcpClient, lib_id: str, filename: str, topics: list[str]) -> None:
    out = CORPUS_DIR / filename
    if out.exists() and "--force" not in sys.argv:
        print(f"跳过（已存在，重抓用 --force）：{out}")
        return
    parts = [f"# {filename.removesuffix('.md')} 文档（Context7 抓取，评估语料）\n"]
    for topic in topics:
        text = client.call_tool("query-docs", {"libraryId": lib_id, "query": topic})
        parts.append(f"\n\n## Topic: {topic}\n\n{text}")
        print(f"  {filename} ← {topic}（{len(text)} 字）")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    print(f"落盘 {out}（共 {sum(len(p) for p in parts)} 字）")


def main() -> None:
    client = HttpMcpClient(CTX7_URL, timeout=60)
    try:
        for lib_id, filename, topics in LIBRARIES:
            fetch_library(client, lib_id, filename, topics)
    finally:
        client.close()   # 与 assemble_servers 同纪律：不留孤儿连接


if __name__ == "__main__":
    main()
