"""评估测试集：每个条目 = (问题, 期望命中的笔记列表, 形态标签)。

标注规则：
- expected 里写「该问题理应被检索到的笔记」——用笔记中一段独特文字做"指纹"，
  只要这段文字出现在检索结果里，就算命中。
- 知识库里没有答案的问题，expected 为空列表——用来检验"不该硬凑结果"。
- 标注与语料必须同步：加新笔记后要复查本文件——「AI研发的学习路径」曾因
  Agent仿制笔记入库而标注过期（LLM-as-judge 评测当场抓出，2026-09-12）。

形态分层（2026-09-19 混合检索评估）——出题时的设计意图，用于 miss 归因：
- paraphrase  概念改写型：query 与文档用词有实质差异，考语义近似（向量强项）
- exact       精确词型：query 含文档中的专有名词/参数/命令，考字面命中（grep 强项）
- absent      无答案型：库里没有，检验闸门不硬凑
语料两源：data/notes（线上笔记）+ evals/corpus（Context7 抓取的技术文档，
指纹已按抓取原文核对）。
"""

CASES: list[tuple[str, list[str], str]] = [
    # ---- 线上笔记（data/notes）：概念改写 + 无答案 ----
    ("RAG 是什么", ["RAG 是检索增强生成"], "paraphrase"),
    ("什么是检索增强生成", ["RAG 是检索增强生成"], "paraphrase"),
    ("embedding 是干嘛的", ["Embedding 是把文字转换成向量"], "paraphrase"),
    ("余弦相似度是什么", ["余弦相似度用来衡量"], "paraphrase"),
    ("有哪些向量数据库", ["向量数据库专门用来存储"], "paraphrase"),
    ("什么是 Agent", ["Agent 是能自主调用工具"], "paraphrase"),
    ("PHP 是什么语言", ["PHP 是一种脚本语言", "PHP是最好的语言"], "paraphrase"),
    ("Python 能做什么", ["Python 是一种通用的编程语言"], "paraphrase"),
    ("Agent是什么", ["Agent 是能自主调用工具"], "paraphrase"),
    ("Agent能做什么", ["能自主调用工具、规划任务来完成目标"], "paraphrase"),
    ("AI研发的学习路径是什么", ["三条学习路径"], "paraphrase"),
    ("中国的首都是哪", [], "absent"),
    ("怎么做饭好吃", [], "absent"),
    # ---- Preact 文档（evals/corpus/preact.md）----
    ("hooks 为什么能在组件多次渲染间记住数据", ["remember information across"], "paraphrase"),
    ("用虚拟 DOM 写界面有什么好处", ["compose user interfaces"], "paraphrase"),
    ("preact 的 hydrate 函数有什么用", ["skip most diffing when loading pre-rendered HTML"], "exact"),
    ("computed 创建的 signal 是只读的吗", ["Creates a read-only signal"], "exact"),
    ("hooks 能写在条件分支或循环里吗", ["cannot be conditional or inside loops"], "exact"),
    # ---- FastAPI 文档（evals/corpus/fastapi.md）----
    ("FastAPI 的依赖注入适合用来做什么", ["sharing logic, managing database connections"], "paraphrase"),
    ("中间件在请求流程中什么时候执行", ["processes every request before it reaches a path operation"], "paraphrase"),
    ("Depends 的依赖结果会被缓存复用吗", ["cached and re-used"], "exact"),
    ("FastAPI 交互式文档默认在哪个路径", ["available at `/docs`"], "exact"),
    ("后台任务在什么时机执行", ["after the response is returned to the client"], "exact"),
    # ---- SQLite 文档（evals/corpus/sqlite.md）----
    ("SQLite 的类型系统为什么灵活", ["data type is associated with the individual value rather than the column"], "paraphrase"),
    ("WAL 模式对并发读写有什么好处", ["allowing readers and writers to operate simultaneously"], "paraphrase"),
    ("怎么开启 SQLite 的 WAL 模式", ["PRAGMA journal_mode = WAL"], "exact"),
    ("json_each 和 json_tree 有什么区别", ["recursive traversal"], "exact"),
    ("STRICT 表是做什么的", ["enforce rigid type checking"], "exact"),
    # ---- 无答案（技术主题但语料未覆盖）----
    ("React 的 fiber 架构是怎么工作的", [], "absent"),
    ("PostgreSQL 和 MySQL 该怎么选", [], "absent"),
]
