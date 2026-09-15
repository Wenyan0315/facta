# 决策记录 · M6.1–M6.3 · 记忆层（持久化/压缩/温层检索）

> 迁自 docs/architecture.md 决策记录（2026-09-15 v0.37 拆分，内容原样保留）；append-only——修正与补充以新记录追加，不改写旧记录。返回 [architecture.md](../architecture.md)

- **记忆持久化（2026-09-06 M6.1）**：memory/store.py（asdict→JSON 存，Message(**d) 读回，往返无损已验证）。窄 except 原则：只捕 FileNotFoundError（「文件不存在=新会话」是正常场景，返回 [] 静默降级）；文件损坏(JSONDecodeError)必须大声崩——静默吞掉会让历史蒸发而无感知。接线架构（控制反转再应用）：**落盘策略归组装层**——__main__ 启动时 load 注入、quit 退出时 save；run_chat 只管内存（messages 进参 + return 归还，不碰文件 IO），换存储介质主循环零改动。关键细节：条件种人设用 `if not messages` 而非 `is None`——load 首跑返回 []，空列表也要种 system prompt，否则第一次运行的 agent 无人设。已知 tradeoff（简单优先，接受不处理）：①system prompt 入盘，改 SYSTEM_PROMPT 后旧会话仍用旧人设 ②Ctrl+C 不保存（input() 直接抛异常）③全量记忆零筛选——「记住暗号」不是智能行为是全量录像，token 随轮数线性增长，正是 M6.2 摘要压缩的动机。session.json 是运行时数据（非资产），进 .gitignore——与 data/notes（语料资产，进库）的判定同「数据与代码分离」条款。

- **摘要压缩（2026-09-06 M6.2）**：三件套架构——底片（messages，append-only，落盘）/ 投影（payload，每轮 llm.generate 前由 build_payload 现切，纯函数）/ 缓存（summary+summarized_upto 覆盖进度，触发式重算平时复用）。agent_loop 工具轮双写（底片+投影同步 append），最终回答只入底片。**覆盖不变量（验收翻车换来的铁律）**：每条消息必须可见——要么已被摘要覆盖、要么原文在 payload；第一版有死区（窗口外未达触发阈值的积压两头不沾），暗号恰好落进去 → 模型看不见原文 → 当场编了个假暗号（幻觉标准机制：模型看不见时不会说看不见，会编）。修复一行：`start=min(window_start, summarized_upto)`；副作用是未触发摘要前 payload=全量直发。**窗口边界规则**：左边界必须落在 user 消息上——裸切会把 role=tool 消息与它的 tool_calls 爹切分家，孤儿 tool 消息被 API 400 拒收（mock 不校验，本地全绿真模型间歇炸）。**触发策略**：早触发（窗口6+余量6，消息条数为粗代理，50K 贴文只算1条的局限已知），拒绝「快满才压」（自指挤压+悬崖风险）。滚动摘要=旧摘要+新积压喂给内部 LLM（tools=None 防递归，search_and_summarize 同款）；摘要器 prompt 点名必须保留暗号/数字/承诺等关键事实。连带修复：①Ctrl+C/Ctrl+D 捕获后 trim_incomplete_round（掐掉孤儿工具轮）再保存，M6.1 tradeoff② 就此关闭 ②SYSTEM_PROMPT 增加记忆自我认知（历史自动保存、摘要等同亲历记忆），修掉「我技术上做不到持久记忆」的过时自画像 ③摘要完成即打印（可观测性，保真度当场可审）。验收教训：合成测试验证了「边界合法」，手工验收验证了「不变量成立」——不变量必须显式写下来才能被测到。

- **架构五问裁定（2026-09-06 M6.2 后评审）**：①用户取消——Ctrl+C 保存已随 M6.2 落地；完整取消（流式中打断）是 streaming 的副产品；长工具的协作式取消留阶段二 ②意图识别——无需建设：M5 意图守卫的生死已证明 LLM+工具即意图识别器；显式意图模块只在多模型路由/guardrails 场景复活（M7.5/阶段二）③状态管理——文件即状态是本规模正解（无状态计算+状态外置=重启安全）；状态机框架等阶段二长任务 checkpointing ④streaming——排期 M6.4；难点是 tool_calls 分片重组；接口用 generate() 可选参数演进 ⑤数据库——JSON 现在正确；SQLite 是 M6.3 的自然台阶（触发信号：跨会话搜索要遍历 N 个文件、防写坏）；MySQL/PG=多用户服务端、Redis=热数据层非归档层，选型跟着访问模式走。

- **M9 重定义（2026-09-06 架构讨论）**：能力 vs 触发之分——MCP/skill 提供「被调用才执行」的能力，但死进程不会自己醒，定时触发只能外置：归系统 cron/launchd（唤醒 agent，不 daemon 化、不自建调度器）。M9 的学习原子重排：headless 任务模式（run_chat 之外的第二入口，无人对话跑完即退，阶段二 coding agent 前身）＞ arXiv 外部 API 接入（M7.5 容错四件套练习场）＞ 推送通道。杀手锏：知识库语义过滤——新论文与笔记库算相似度、过阈值才推（RAG 反向应用：语料当过滤器给信息流打分，复用 BGE-M3）；去重状态与过滤逻辑是 agent 侧资产，外部工具替不了。arXiv 手写接入优先（from-scratch），MCP 替换留作届时选项。

- **search_history 与三温度层（2026-09-07 M6.3a）**：温层开门——底片一直在内存（append-only），模型看不见只因投影切掉了。search_history 闭包注入 history 列表（第三个闭包依赖；__main__ 装载顺序：history 必须先于 register_builtin）。**关键词子串匹配而非语义检索**：历史每轮生长，语义检索要每轮重 embedding（贵且慢）；智能活（提炼关键词）给模型，蛮力活（扫列表）给工具。角色过滤只搜 user/assistant（system=人设、tool=可重生的检索产物，非"对话原话"）；返回带 #编号+历史总数分母（位置核验材料）。已知局限（五轮验收逼出）：①词组查询按整短语子串匹配，模型爱组词组（搜"PHP 工具"漏掉只含"PHP"的 #1）→ 待分词匹配；②"第一句话"类问题是**位置查询**（ORDER BY id LIMIT 1），关键词是**内容查询**（LIKE），方向相反，模型被迫三跳间接推理（摘要线索→猜词→检索→取最小编号），最后一跳位置核验常失败 → read_history 位置读取工具排 M6.3b（与知识库 list/read/search 三件套对称）。**read_history 已于次日落地并验收通过（一跳直达，start=1 即第一句原话）**：与 search_history 正交互补（位置查询 OFFSET/LIMIT vs 内容查询 LIKE，"知道什么缺什么"决定走哪条），编号体系跨工具一致是硬契约（同一数据的多个视图必须共享坐标系）；组合拳=search 定位（命中 #N）→ read 取景（start=N-1, count=3 看上下文）；tool 结果截 300 字防灌爆（user/assistant 逐字全量——逐字引用是使命）。

- **Skill**：本质是 prompt 模板 + 资源包，后续做 `skills/` 目录按需加载，不提前设计

- **多智能体**：阶段二做（Orchestrator 编排 + 子 agent 实例化组合），依赖 M5 扎实后才做

- **校验 guardrails**：不单独分层，横切在 core 循环和工具层——M4 结构化输出校验+重试；M5 工具参数校验+自我纠错（2026-09-12 补全景：校验三维分工=语法层 json.loads / 结构层 execute 按 JSON Schema 最小子集校验 required+基础类型（此前 schema 只用于生成菜单、执行时不 enforcement，记忆库硬约束「工具必须 JSON Schema 参数校验」只兑现一半）/ 语义层工具函数自身抛异常——坏参数在任一层都以错误字符串回给模型，自纠反馈环三层无差别）

- **评估 evals**：独立 `evals/` 目录不进运行链路；检索用 precision@k/recall@k/MRR；M4 后加 LLM-as-judge

- **会话状态持久化（2026-09-08，修复 P0-1）**：M6.1 只落盘底片 messages，M6.2 的 summary/summarized_upto 是 run_chat 局部变量，重启即清零——滚动摘要退化成「启动首轮一次性全量大压缩」（档案越长越接近悬崖式压缩，且暗号跨压缩存活不可复现）。修法：抽 `Session` dataclass（messages + summary + summarized_upto）整体落盘，store 出 `save_session/load_session`（version 预留演进 + 旧列表格式自动迁移 + 游标钳到 [1,len] 防越界）；run_chat 改为注入 Session 原地变异、归还 Session——落盘策略仍归 __main__（控制反转不打折）。连带收口 __main__ 接线（此前半段还是旧 load_messages/save_messages，直接 NameError）。
