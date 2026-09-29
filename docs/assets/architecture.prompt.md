# facta 架构图重绘说明

本图依据 2026-09-29 当前工作区的实际代码重绘。生成方式：imagegen 内置工具；旧 PNG 只作为深色视觉风格参考，不沿用旧模块关系。

核对入口：`src/facta/__main__.py`、`server/__main__.py`、`orchestrator/assemble.py`、`orchestrator/loop.py`、`tools/registry.py`、`tools/spawn.py`、`memory/`、`knowledge/`、`core/`、`servers/memory_server.py`。

生成提示词如下（事实与布局规范，非可编辑画布；旧 kanva 文件仅对应历史版本）：

```text
+Use case: infographic-diagram.
Create a completely new, publication-quality software architecture diagram for the real open-source project "facta", replacing an outdated architecture image. Deliver a sharp landscape PNG, preferably 2560×1920 or larger, approximately 4:3. It must look like a polished engineering diagram, not an illustration or marketing poster. The provided old image is STYLE REFERENCE ONLY; rebuild the structure and labels from the exact specification below. Do not copy its clutter, outdated text or arrows.

VISUAL STYLE:
Deep navy background (#0B1220), slightly lighter rectangular cards (#121E30), bright nearly-white Chinese text, muted but still legible secondary text. Amber main-flow arrows, cyan model connection, teal knowledge/memory accents. Modern sans-serif Chinese typography. Fine borders, restrained corner rounding, generous spacing. No glow, 3D, decorative icons, gradients, logos other than the text "facta". Every arrow must connect specific box boundaries and never run through text. High readability at README width; use short labels and avoid very small text.

TITLE at top left:
"facta · 系统架构"
Subtitle: "本地个人 AI Agent｜执行循环、长期记忆与工具扩展"
Top-right tiny legend: "实线：调用与返回   虚线：装配与上下文"
No version numbers, ADR counts, efficiency comparisons, test counts, "护城河", "极强", or promises of complete safety.

LAYOUT AND EXACT CONTENT:
Top band spanning full diagram, heading "01  交互入口":
Two adjacent sections:
"CLI" / "python -m facta" / "多轮对话 · 操作确认"
"Web · FastAPI + SSE" / "对话 / 任务计划 / 记忆 / 知识语料 / 知识图谱" / "会话准入 · 流式事件 · 取消与确认"
Use one Web section only. Main chat is vanilla JS; remaining panels Preact, but omit those implementation details to save space.

Next narrow band full width, heading:
"02  统一装配 · orchestrator/assemble.py"
Contents: "AppContext 共享资源 · 按会话构建 Agent 与工具注册表 · 可选能力按配置启用"
An amber downward arrow connects entry band to assembly band. Assembly sends an amber downward arrow to central execution card, labelled "构建 Agent". Short dashed branches from assembly go to memory and model cards, showing provisioning. Do not depict memory/knowledge as serial mandatory stages.

Middle row THREE cards:
LEFT card "记忆与会话 · memory/"
Text lines:
"Session：原文 · 滚动摘要 · PlanBoard"
"项目记忆 + 用户记忆 → 主 Agent 上下文"
"固化：萃取 → 枚举审查 → 硬校验"
"Web 每轮重读记忆快照"
Small storage line: "sessions/*.json · learned/*.md · ~/.facta/user.md"
This is a parallel support service, not a mandatory retrieval stage.

CENTER card, visually emphasized with amber border:
"03  执行核心 · orchestrator/"
"Agent：系统提示 · 工具范围 · 轮次预算"
"run_turn · ReAct"
A clear small closed loop of four labelled nodes:
"构建上下文" -> "模型决策" -> "执行工具" -> "回填结果"
Return arrow from "回填结果" back to "模型决策", not back to context construction.
Small lines below:
"计划审批 → 步骤执行 → 状态回写 → 收官"
"可选场景路由：调整首轮工具菜单"
"循环限制 · 协作式取消 · 事件回调"
The central card connects bidirectionally to the right model card, labelled "模型调用 / 回复".
The left memory card supplies context to the central card via a dashed arrow pointing LEFT-TO-RIGHT labelled "上下文". Do not imply an LLM automatically triggers every memory operation.

RIGHT card "模型与网关 · core/"
Text lines:
"LLM 接口 · OpenAI 兼容供应商"
"RobustLLM：重试 · 超时 · 精确缓存 · 熔断"
"FallbackLLM：模型降级"
"UsageLedger：调用用量记录"
"用户链 / 内部链"
Small footnote: "语义缓存默认关闭；场景路由可选"

Next row TWO cards:
LEFT wider card, below central execution:
"04  工具执行 · tools/"
"ToolRegistry：参数校验 · 计划范围 · 操作确认 · 审计"
Three lines:
"内置工具：time / history / notes / files / terminal / web / todo / plan / spawn / graph"
"子 Agent：复用 run_turn · 并行派发 · 可选 worktree 隔离"
"MCP 客户端：stdio / HTTP → 外部工具服务器"
Small safety note:
"文件访问围栏 · macOS seatbelt（其他平台无此后端）"
Draw TWO separated amber arrows between central execution card and this tool card: one down labelled "工具调用", one up labelled "结果回填". This visibly closes the real execution loop.

RIGHT card "知识检索 · knowledge/"
Text lines:
"笔记 → 切块 → Embedding → 向量检索"
"Chroma 持久化 · 内容指纹增量同步"
"缺依赖或密钥：词袋 + 内存库"
"知识图谱：抽取 · 同步 · 关系查询"
Small line: "检索带出处；模型按需调用"
Draw a horizontal bidirectional teal arrow from tools card to knowledge card, labelled "notes / graph". This is the access path: model decides tools, notes/graph tools use knowledge. DO NOT draw a mandatory knowledge -> memory -> tools chain.

BOTTOM two slim support cards, separated from the runtime diagram by whitespace and a thin divider, heading "运行支撑与验证":
Left: "持久化与恢复"
"会话 JSON · 记忆 Markdown · 向量库 · 图谱 JSON"
"工具边界检查点 / heal · 审计 JSONL"
"已记录结果回注；未知副作用需核验"
Right: "评估与外部接入"
"tests / evals：检索、回答、消融、冻结任务与恢复评估"
"evalkit：评估复用组件；计划收官也复用 judge 解析"
"memory_server.py：只读记忆 MCP，按需启用"
These are support summaries, NOT sequential runtime steps. No arrow from the primary runtime flow through the evaluation block.

VERY SMALL FINAL FOOTNOTE, still readable:
"图示当前实现。项目运行记忆不随示例发布；外部服务和可选能力依配置启用。"

ACCURACY CONSTRAINTS:
1. Only the facts and text specified above. Do not invent queues, Redis, SQL databases, event buses, autonomous schedulers, multi-user service, IM gateways, RAG rerankers, Docker isolation or production-readiness.
2. Chroma/semantic embeddings are optional, not required to start chatting. Do not hard-code BGE-M3 as the only embedding model.
3. Routing and semantic caching are optional; semantic cache disabled by default.
4. Durable recovery is not exactly-once execution. Keep the note about verifying unknown side effects.
5. Each requested label should appear once, with no repeated section, missing arrowheads, clipped text or pseudo-Chinese. Prioritize exact short labels over adding decoration.
```
