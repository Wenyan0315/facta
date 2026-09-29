# Facta · 个人 AI Agent

[![CI](https://github.com/Wenyan0315/facta/actions/workflows/ci.yml/badge.svg)](https://github.com/Wenyan0315/facta/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**从零自研的个人 AI Agent：执行主轴 + 长期记忆 + 工具/MCP 通用外延。无 Agent 框架，分层全部手写，接口与实现分离、逐层可替换。**

**A personal AI agent built from scratch — execution-first, with durable memory, extensible via built-in tools and MCP. No agent framework: every layer is hand-written, interface/implementation separated, and individually replaceable.**

![系统架构图 / System Architecture](docs/assets/architecture.png)

> 完整架构说明（中文）：[docs/architecture.md](docs/architecture.md)｜71 份决策记录见 [docs/decisions/](docs/decisions/)。
> Full architecture docs are in Chinese; 71 decision records live in [docs/decisions/](docs/decisions/).

---

## 特性 / Features

- **ReAct 主循环**：感知 → 决策 ⇄ 工具调用 → 观察；模型掌握决策权，程序掌握执行权。
- **九族内置工具**：time / history / notes / files / terminal / web / todo / plan+spawn / graph；另有 stdio + HTTP 双传输的 MCP 客户端。
- **Plan-then-act 人审掌舵**：复杂任务先出计划等人批准，步骤派发自动回写，终态收官校验。
- **场景路由**：轮首一针按 direct / single_tool / complex 决定工具菜单形状（可插拔，缺席走原生路径）。
- **多层记忆**：会话内消息、滚动摘要（底片不动）、跨会话多会话、`data/learned` 三桶固化管线（萃取 → 枚举审查 → 硬校验）、用户级记忆外置。
- **RAG 知识层**：BGE-M3 向量嵌入 + ChromaDB 增量向量库 + 知识图谱（抽取 / 增量同步 / 查询原语），检索带出处。
- **LLM 网关**：OpenAI 兼容接口；记账、重试超时、双档缓存、熔断四件套 + FallbackLLM 降级链。
- **崩溃恢复**：工具边界落盘账本，进程被 kill -9 后重启自动 heal，已记录的结果原样恢复；结果未知的副作用会提示模型先核验再继续。
- **安全收口**：L0/L1 命令白名单 + L2 确认缝、外部命令 macOS seatbelt 沙箱（其他平台当前回退为普通命令执行——README 不再笼统宣称跨平台沙箱）、记忆写入围栏、审计 append-only。
- **CLI + Web 双壳**：FastAPI + SSE 流式（仅绑 127.0.0.1），Preact 前端四个面板（对话 / 任务计划 / 记忆 / 知识图谱）。
- **离线评估**：冻结题库 + git archive 副本剥离 + 注入金丝雀 + LLM-as-judge，CI 中与单元测试同跑。

---

## 快速开始 / Quick Start

需要 Python 3.11+。 / Requires Python 3.11+.

```bash
git clone https://github.com/Wenyan0315/facta.git
cd facta

# 安装：dev=测试与工具链，rag=向量库，web=Web 壳（按需组合）
# Install: dev=testing/tooling, rag=vector store, web=web server (combine as needed)
pip install -e ".[dev,rag,web]"

# 配置密钥（.env 已被 gitignore） / Configure API keys
cp .env.example .env
# 编辑 .env：填入一家 LLM 的 API_KEY 就能跑（教学模式只要 FACTA_PROVIDER=mock）
```

任意 OpenAI 兼容供应商均可通过 `{前缀}_API_KEY` / `{前缀}_BASE_URL` / `{前缀}_MODEL` 三个环境变量接入。不装 `[rag]` 时自动退化为词袋嵌入，零外部服务也能跑。

### 支持的供应商 / Providers

| `FACTA_PROVIDER` | 配置前缀 | 默认 base_url | 默认对话模型 | embedding 可用 |
|---|---|---|---|---|
| `mock` / `echo` / `repeat` | （无 key，教学） | — | — | 词袋 |
| `deepseek`（默认）/ `deepseek-flash` | `DEEPSEEK_` | api.deepseek.com | deepseek-chat / deepseek-flash | — |
| `siliconflow` | `SILICONFLOW_` | api.siliconflow.cn/v1 | DeepSeek-V3 | `siliconflow`（默认）/ `openai` |
| `openai` | `OPENAI_` | api.openai.com/v1 | gpt-4o-mini | `openai`（设 `FACTA_EMBED_PROVIDER=openai`）/ `siliconflow` |
| `qwen` | `DASHSCOPE_` | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus | `siliconflow` / `openai` |
| `zhipu` | `ZHIPU_` | open.bigmodel.cn/api/paas/v4 | glm-4-flash | `siliconflow` / `openai` |
| `moonshot` | `MOONSHOT_` | api.moonshot.cn/v1 | moonshot-v1-8k | `siliconflow` / `openai` |

不在表里的中转商不必改代码：用 `{PREFIX}_BASE_URL` + `{PREFIX}_MODEL` 直接覆盖默认。embedding 通过 `FACTA_EMBED_PROVIDER`（默认 `siliconflow`）切换；缺 key 或未装 `[rag]` 时自动降级词袋嵌入，对话不报错（只是检索质量变差）。详见 [ADR 070](docs/decisions/070-multi-provider-compatibility.md)。

### CLI

```bash
python -m facta          # 真模型（默认 deepseek-flash）
python -m facta mock     # 假模型：不花钱、不联网，适合体验流程
```

### Web

```bash
python -m facta.server   # http://127.0.0.1:8000（只绑 loopback，默认 deepseek）
```

> **默认模型差异**（ADR 069 P2-8）：CLI 默认 `deepseek-flash`（便宜主力），Web 默认 `deepseek`（chat 档）。任一处都可用 `.env` 里的 `FACTA_PROVIDER=<name>` 覆盖。详见 [支持的供应商](#支持的供应商--providers)。

### 测试 / Tests

```bash
pytest                  # 700+ 用例
ruff check && mypy src/facta
```

---

## 项目结构 / Layout

```
src/facta/
├── orchestrator/   # 编排层：run_turn 主循环、Agent 对象、依赖装配、崩溃账本
├── tools/          # 工具层：ToolRegistry + 九族工具 + MCP 客户端 + 沙箱
├── core/           # 地基层：LLM 接口、网关四件套、场景路由、evalkit
├── knowledge/      # 知识层：RAG 检索、向量库、知识图谱
├── memory/         # 记忆层：会话、压缩、固化三桶、计划板
└── server/         # Web 壳（FastAPI + SSE）与前端静态资源
evals/              # 离线评估：冻结题库、注入题库、judge
servers/            # MCP 演示服务器
tests/              # pytest 测试
docs/               # 架构文档、产品文档、70 份 ADR
data/               # 语料与运行时数据（notes/learned.example 入库，其余 gitignore）
```

装配遵循「唯一真值源」：CLI 与 Web 都经 [orchestrator/assemble.py](src/facta/orchestrator/assemble.py) 依赖注入，条件装配（无 key 不挂路由、缺向量库不注册 RAG）。

---

## 文档导航 / Documentation

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 系统架构（含分层图、71 份 ADR 索引、在册触发信号） |
| [docs/decisions/](docs/decisions/) | 架构决策记录（ADR 001–071，含否决档案） |
| [docs/manual-test-cases.md](docs/manual-test-cases.md) | 人工测试用例集 |

## 许可证 / License

[MIT](LICENSE)
