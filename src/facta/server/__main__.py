"""Web 服务启动入口：python -m facta.server。

provider 从 FACTA_PROVIDER 环境变量取（Web 常驻场景用环境变量，而非 CLI 的
argv），默认 deepseek。安全默认：只绑 127.0.0.1（OpenClaw 调研吸收，本机单人使用）。

P2-8 评审修复：先 load_dotenv 再读 provider——原顺序下 `.env` 里的
FACTA_PROVIDER 要等 assemble 才被加载，对本入口的模型选择来得太晚，
只有 shell export 才生效（评审实测 .env 填 mock 仍选 deepseek）。
旧名 AGENT_PROVIDER 兼容读取（新名优先）。
"""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv

from facta.orchestrator.assemble import assemble
from facta.server.app import create_app


def main() -> None:
    load_dotenv()   # 先于 provider 读取：.env 里的 FACTA_PROVIDER 必须对本进程生效
    provider = os.environ.get("FACTA_PROVIDER") or os.environ.get("AGENT_PROVIDER", "deepseek")
    ctx = assemble(provider)
    app = create_app(ctx)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
