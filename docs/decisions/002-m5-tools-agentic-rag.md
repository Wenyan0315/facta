# 决策记录 · M5/M5.5 · 工具调用与 Agentic RAG

> 迁自 docs/architecture.md 决策记录（2026-09-15 v0.37 拆分，内容原样保留）；append-only——修正与补充以新记录追加，不改写旧记录。返回 [architecture.md](../architecture.md)

- **工具调用闭环（2026-09-05 M5）**：模型只决策不执行——返回 tool_calls（name+arguments JSON），程序执行后以 role="tool" 消息回填再问。核心组件：tools/registry.py（Tool四要素：name/description/parameters给模型，func给程序；ToolRegistry：schemas()生成菜单、execute()执行且错误也返回字符串→模型可自我纠正）、tools/builtin.py（get_current_time/list_notes）、agent_loop 工具循环（for...else 检测不收敛，_MAX_TOOL_ROUNDS=5 保险丝）。入史策略：工具轮完整入史（tool消息必须与tool_calls配对，否则API报400；且模型记住工具结果有长期价值）；纯聊天轮保持历史干净（RAG资料不滞留）。接口演进两手法：Message只加带默认值的可选字段、generate只加带默认值的可选参数——老代码零改动。

- **数据与代码分离（2026-09-05 Q2）**：笔记从 `SAMPLE_NOTES`（焊在代码里）迁到 `data/notes/*.md`（每条一个文件，中文名语义化），`knowledge/loader.py::load_notes()` 负责加载（sorted 保序 + 显式 utf-8 + 快速失败）。__main__ 与 evals 共用同一 loader → 评估与线上永远同一份语料。判定标准：会被 import 的是代码进 src/，只被读取的是数据进根目录。当前全量加载（9 条 <10KB，"内存不是瓶颈时简单就是性能"）；M7 库变大再长出索引+按需读取。加笔记=丢 md 文件，零改码。

- **工具写入与知识库治理（2026-09-05 M5 尾声）**：write_note 工具（agent 第一次能改文件系统）带双重防线——①安全栅栏：路径穿越(resolve+is_relative_to)/.md后缀/禁子目录/覆盖保护；②查重闸门：写入前 kb.search(content, min_score=0.85)，高度重复拒绝并提示先读后合并（知识库治理第1层=写入时把关；第2层维护工具、第3层元数据血缘留 M6/M8）。依赖注入新姿势：kb 通过闭包注入 write_note（register_builtin(registry, kb)）——工具层开始依赖知识层。溯源缺口：KB 只存文本块不记"块来自哪个文件"，查重只能返回相似片段而非文件名，M8 图谱补元数据。自我进化闭环已端到端验证：agent 读旧笔记→综合→write_note 新笔记→重启后可检索。已知边界：0.85 只拦"几乎照抄"级重复，"同主题不同措辞"的语义重复需 LLM-as-judge（M6 候选）。

- **Agentic RAG（2026-09-06 M5.5）**：kb.search 包成 search_notes 工具，检索权从程序移交模型——主循环删掉自动检索段（121→87 行，复杂度塌缩：代码更少能力更强），run_chat 不再依赖 kb，知识层完全经工具层介入对话。连带修订：①三级信息政策从"每轮拼进消息"升级进 SYSTEM_PROMPT（提示词即政策，一次设定全程生效）②M5 意图守卫（len>=3 粗筛）随之废除——短输入误中由模型自主决策天然解决，粗筛本就是 Agentic RAG 落地前的临时占位 ③schema description 承担新职责：教模型何时该查、查询词要提炼、指代性话语要改写。search_and_summarize 复合工具（检索+内部再调 LLM 摘要）= Sub-agent 原型；内部调用绝不传 tools，防"工具调工具"无限递归。遗留：write_note 查重 0.85 只拦照抄级，语义查重留 M6 LLM-as-judge。

- **MCP**：M5 工具层做成 ToolRegistry（内置 Python 工具 + MCP 客户端动态发现），MCP 顺延待排期（M5.5 已被 Agentic RAG 占用）
