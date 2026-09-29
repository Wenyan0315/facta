"""Web 服务启动入口：python -m agent.server。

provider 从 AGENT_PROVIDER 环境变量取（Web 常驻场景用环境变量，而非 CLI 的 argv），
默认 deepseek。安全默认：只绑 127.0.0.1（OpenClaw 调研吸收，本机单人使用）。
"""

from __future__ import annotations

import os

import uvicorn

from agent.orchestrator.assemble import assemble
from agent.server.app import create_app


def main() -> None:
    provider = os.environ.get("AGENT_PROVIDER", "deepseek")
    ctx = assemble(provider)
    app = create_app(ctx)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
