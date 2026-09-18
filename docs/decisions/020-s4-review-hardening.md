# 决策记录 · S4 外部评审修复轮（2026-09-18）

> S4b 收官后引入外部专家评审（38 条，docs/reviewq/qwen.md），按分类法消化。返回 [architecture.md](../architecture.md)

- **评审分类法（本轮总纲）**：38 条建议分三桶——**6 条硬伤**（与既有承诺/安全底座直接冲突，即修）／**12 条已知边界**（已有触发信号在案，评审只是加速，不新开工）／**3 条方向分歧**（产品定位层面，记入 architecture.md 待讨论，临期拍板）。分类先于修复：不做「评审说什么就改什么」，每条先对照既有决策（014 定位、019 白名单粒度、veto-archive 前端框架化条件）再定性质。

- **R1 结构化日志分层**：内核库一律 `logging.getLogger(__name__)`（assemble/gateway/compressor/mcp_config 等 40+ 处 print→logger），入口层配 basicConfig——CLI `format="%(message)s"`（用户界面感不变）、Web 带 asctime/levelname/name（服务端可观测）；CLI 的交互 print 与 demo() 的演示 print 保留（那是界面不是日志）。测试侧 capsys 断言同步改 caplog。

- **R2 RunResult 三态枚举**：`run_turn` 返回 `tuple[RunResult, Message | None]`——COMPLETED/CANCELLED/FAILED 互斥终态，Web 终态判定不再靠「有没有 error 事件 + cancel_requested」反推 None 的含义。调用面 7 处（cli/app/3 测试文件）全同步。

- **R3 降级显式化（诚实降级）**：mock 兜底候选改 `MockLLM(degraded=True)`，回复自带「⚠️ 真模型暂时不可用，这是降级回复」前缀——降级不装正常；FallbackLLM 切候选时 logger.warning 声明保持。与 007 降级链「优雅兜底」承诺对齐：优雅≠沉默。

- **R4 白名单参数级校验（评审 #17）**：`_DANGEROUS_ARGS` 按命令名查危险参数集（find 的 -exec/-execdir/-ok/-okdir/-delete、sort 的 -o/--output），命中即弹确认——只读命令名+危险参数 ≠ 只读。裁定细节：评审同时点名的 grep -x 不收（整行匹配纯只读，误报确认正是白名单要防的疲劳源）；长选项等号形式（`--output=file`）用 split("=", 1) 兼容。

- **R5 RAG 引用溯源（SearchHit）**：溯源数据本就在库里（upsert 的 metadata={"source", "hash"}），缺的只是 query 透出。接口形状裁定：dataclass `SearchHit(chunk, score, metadata)` + `source` property（替代裸 tuple 加一位——GatewayConfig 位置漂移教训在案，命名访问防复发）。全链路：vector_store.query（双实现，Chroma include 补 metadatas）→ knowledge_base.search → notes.py 三处消费（search_notes 输出「出处：file.md」、查重闸门能报「撞了哪篇」——原「溯源缺口 M8 补」注释兑现、search_and_summarize context 带出处）→ evals 两处同步。双实现溯源一致性入测试（InMemory 与 Chroma 的 source 列表相等）。范围裁定：UI 点击跳转不做（需前端事件协议，超本轮），来源进 tool 返回文本后模型可引用、UI 显示 tool 输出时自然可见。

- **R6 mypy + ruff 进 CI**：宽松起步不 strict（先抓真 bug：漏 import/签名不匹配/None 滥用），严格度随成熟度拧紧。**首轮战果即证明价值：mypy 抓到 rename 端点 `load_session` 漏 import 的真 bug**（体验轮新增路径，测试只测了 active 分支，归档会话改名运行时必炸 NameError——「类型检查从纸面变真保障」的活案例）。mypy 19 错清零（含 LLM.name 基类属性补齐、RobustLLM 死类属性删除、mcp_config 畸形配置 url=None 防御、闭包 None 窄化三式：守卫后绑局部/注册守卫 assert/TYPE_CHECKING 导入破循环）；ruff 64 错清零（显式钉死规则集防版本漂移；项目级 ignore 逐条带理由——BLE001 宽捕获是降级链设计、DTZ 系 naive 本地时间是会话时间戳契约、PLC0415 延迟导入是可选依赖惯例等；B904 错误链/B905 zip strict/E741 等真问题即修）。CI 顺序：ruff → mypy → pytest（fail fast），安装组扩为 [dev,rag,web]（mypy 要看全 import 面）。

- **R7 方向分歧 3 条入待讨论**：产品定位（三场景并列 vs 知识伴侣楔子，对 014 的正面挑战）、S5 投法（skill 系统 vs Agent 对象抽象+plan-then-act）、前端框架化是否提前（veto-archive 触发条件可能被评审的体验药方集体触发）。不裁而记——方向性决策归产品层，临期拍板。

- 验收：ruff ✓ mypy ✓（44 文件零问题）pytest 295 passed +2 skipped（R4 +9 用例：白名单回归 4 + 参数级绕过 5；R5 溯源断言并入双实现契约测试）；demo() 入口人工跑一次（bow 短查询相似度天花板低于及格线致空结果为既有行为，非本轮引入）。
