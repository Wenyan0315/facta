# ADR 116：M2 界定与技能——skill 数据化（Brief 首个）+ 项目记忆归属（双模式）

- 状态：**已裁定**（2026-11-14，用户定稿照修订稿开工，含顺手件 3/4 语义裁定）｜日期：2026-11-12
- 上游：[114](114-facta-2-roadmap.md)（2.0 路线图，M2 期定义与三颗验收钉子）、
  [113](113-brief-generation-prototype.md)（Brief＝M2 首个 data skill，人设/工具/预算/落点已裁定）、
  [111](111-v01-review-prototype.md)（mode/repo_root 接线、锚点注入、归属先例）、
  [112](112-review-tool-rounds-budget.md)（证据预算先例）、
  [115](115-m1-controllable-long-tasks.md)（M1 落地的 `Task.repo_root` 登记）、
  [090](090-data-root-unification.md)（数据根推导）、
  [052](052-memory-write-fence.md)/[053](053-memory-provenance.md)（写围栏与溯源）
- 调整关系：只落 114 的 **M2 期**；M3/M4 各自另案（编号顺延）。113 的实现载体
  即本案——skill 机制由本案裁决，Brief 按 113 的既定设计**填进该机制**，不重开
  113 的人设/工具/预算/落点裁定。

## 背景

现状盘点（读码核对）：

| 能力 | 现状 | 缺口 |
|---|---|---|
| 人设 | 硬编码两件：`DEFAULT_SYSTEM_PROMPT`（agent.py:35）+ `REVIEW_SYSTEM_PROMPT`（review.py:35） | 新人设＝改代码，违反 114 五跨越「skill 数据化」 |
| mode 分派 | `_agent_for_mode` 只认 chat/review 两分支（app.py:306-319）；`CreateRunRequest.mode` 是 `Literal["chat","review"]`（app.py:200） | 每加一个技能改枚举+分派两处 |
| Brief | 未实现（113 待实施，载体＝本案 skill 机制） | — |
| 项目记忆 | learned 三桶全局一份 `data/learned/`；chat 恒注入、review 仅 `repo_root==WORKSPACE_ROOT` 注入（assemble.py:345） | 无项目登记与归属隔离——第二个项目的任务会看到第一个项目的决定（钉子②未兑现） |
| 归属判定 | `Task.repo_root` 在 M1 只登记不产生围栏（115 §1） | 未接任何注入/固化逻辑 |
| 固化 | `settle_session` 写死 `LEARNED_DIR`（assemble.py:240） | 固化无处按项目分流 |
| 收尾分派 | worker 两处条件只认 review：标题就地补（app.py:453）、跳固化（app.py:471） | 第三个技能要么再改 worker、要么按名字硬编码 |

**架构红线（114 复用）**：不重写内核。演进只发生在两处——编排层把 mode 分派
从「枚举分支」换成「技能表查找」，数据层新增 `data/skills/*.md` 与项目登记表。

## 概念裁定

### 1 skill 数据化：`data/skills/*.md`

一个技能 = 一个 markdown 文件。**零翻译层**：模型看到的人设＝文件正文（agent.py
「注入格式＝落盘格式」同款纪律），人读可 diff。文件格式（分节、行解析）：

| 节 | 必填 | 语义 |
|---|---|---|
| H1 标题 | 是 | 技能名（= mode 值） |
| 正文（首个 `##` 之前） | 是 | 人设 prompt 原文 |
| `## tools` | 否 | 工具名清单；缺省＝技能 registry 全量 |
| `## budget` | 否 | 工具轮预算；缺省＝Agent 默认 5 |
| `## settle` | 否 | `skip`＝跳固化+就地标题（评审/Brief 类）；缺省＝正常固化 |

**校验不静默**（build_review_agent「不静默降级」先例）：未知节名报错（防拼写
漂移）；`tools` 声明的名字不在技能 registry → **装配报错**，不静默少工具
（拼错＝该工具静默不上菜单，review.py 头注记同款风险）。解析器住
`src/facta/skills.py`。

**技能 registry（M2 唯一形态：只读锚点注册表）**（评审问题 1 修订——评审核出
`register_builtin` 是 notes **五**件含 `write_note`、`register_file_tools` 是
**四**件含 `write_file`，缺省全量＝10 件带两个写工具）：技能 agent 的工具池 =
time 一件 + notes 只读四件（list/read/search/search_and_summarize）+ files 只读
三件（read_file/search_code/list_dir，锚点按 repo_root replace，111 同款），
**恰 8 件＝REVIEW_TOOLS 同集**；`write_note/write_file/terminal/spawn/plan/历史`
一律不注册——写与跑命令在结构上缺席（review「不改代码不跑命令」同一事实）；
因此 `allowed_tools=None` 的技能最多拿到这 8 件只读，结构上不可能越界。技能
需要写工具 = M3 授权档一起做（负决策）。技能一律**不注入用户记忆**（陪伴者
语境噪音，111 先例；触发信号＝某技能真需要用户偏好，再加节）。

**装配**：`assemble()` 启动时扫 `SKILLS_DIR` 建 `ctx.skills: dict[str, SkillSpec]`；
`AppContext` 尾部新增 `build_skill_agent: Callable[[Session, str, Path], Agent]`
（技能名 + repo_root 进，Agent 出），**显式固定 `router=None`**（评审问题 8：
「评审 run 不挂场景路由」是 111 等价点，随默认值漂移会丢，钉死在装配里并进
review 等价测试）。`build_review_agent` 字段随 review.py 一并删除，统一走
`build_skill_agent`（可选建议采纳：不留兼容字段）。review 的人设/工具集/预算
从 `review.py` 迁入 `data/skills/review.md`（learned 归属策略统一走 §3，不写在
技能文件里）。另采可选建议：H1 标题与文件名 stem 一致性校验（不一致装配报错，
防「用文件名当 mode」时漂移）。

**零代码钉子**：`CreateRunRequest.mode` 从 `Literal` 扩为 `str`：值为 `"chat"`
或任一已加载技能名，否则 422（缺省 chat ⇒ 既有调用方逐字节不变）。新增技能 =
新增一个 `data/skills/*.md`，`mode=<技能名>` 即用，**零 py 改动**——页面不是
技能的一部分，无页面时 curl API 即可跑（钉子①的验收口径）。`_agent_for_mode`
改为「chat → build_agent；否则 → build_skill_agent」，不再每技能一个分支。

### 2 Brief 交付（113 既定设计填进技能机制）

- `data/skills/brief.md`：人设 = 113 §2 六节契约 + 三条评审补强**原文迁入**；
  `tools` = 113 §3 的 `REVIEW_TOOLS` 同集（两技能各自声明同名清单——第二次
  出现不抽共享模块，113 §1 先例）；`budget` = 12（112）；`settle` = `skip`。
- 前端五落点照 113 §1：`frontend/brief.html` + `frontend/src/brief/`
  （照 review 复制改 `mode:"brief"`）+ `vite.config.mjs` 入口 + `app.py` 的
  `/brief` 路由（FileResponse）+ `static/fw/` 构建产物进 git（CI 前端闸照跑）。
- worker 两处条件**从按名字判断改为按属性判断**（app.py:453/471）：
  `mode == "review"` / `mode != "review"` → `agent.skip_settle`（`Agent`
  dataclass 六字段增一：`skip_settle: bool = False`；`build_skill_agent` 按
  技能文件 settle 节置位，chat 恒 False）。**评审问题 3（异常路径）**：`agent`
  构造在 try 内可能抛异常（技能名未加载等），finally 里读 `agent.skip_settle`
  会 NameError 放大成 sync 端点半挂——改为 **try 前按技能表预计算 `skip_settle`
  布尔值**，453/471 与 finally 只读该布尔；补测试「技能构造失败时 run 仍
  finish、sync 不挂死」。第三、第四个技能不再改 worker，也消除「漏改收尾处就把
  Brief 草稿固化进 learned」（113 评审意见 3）这类按名漏改。113 §4 装配三落点
  被本案 §1 吸收（技能不存在 → mode 422 在入口拦）。
- repo_root 可选且校验泛化到**所有技能 mode**（评审问题 7 修订：chat 维持 111
  的忽略语义，`test_chat_mode_ignores_repo_root` 不动；泛化含存在且为目录校验）；
  skill 未带 repo_root 时回落 `WORKSPACE_ROOT`。**评审问题 2 点名的第三处按名字
  判断**：`_review_repo`（首行 `if body.mode != "review": return None`）会把
  brief 带的 repo_root 丢成 None——改名 `_resolve_repo_root`，chat 恒 None、
  技能 mode 校验解析；补测试「brief + 外部已登记仓 → 文件锚点与 learned 都指向
  该项目」。

### 3 项目记忆归属（双模式）

- **项目登记表**（单一真值源）：`data/projects/projects.json`（运行时数据，
  **不进 git**，`.gitignore` 增 `data/projects/`，与 tasks/ 同口径）。条目：
  `name / repo_root / memory_mode("centralized"|"in-repo") / learned_path`
  （learned_path 登记时按模式推导落盘，读时以盘上为准）。登记即建目录。
  读写与迁移住 `src/facta/projects.py`（`ProjectsManifest`）。
- **目录**：集中式＝`data/projects/<name>/learned/{decisions,constraints,other}.md`
  ——`read_learned`/`consolidate` 全套复用（consolidate 已收 learned_dir 参数，
  路径参数化即可，三桶文件名不变，行格式与溯源 tag 全不变）；
  就地式＝`<repo_root>/.facta/learned/`（往工作仓写文件须显式同意，
  `.gitignore` 由用户定——114 已拍板）。
- **归属判定**（单一函数 `resolve_learned_dir(repo_root) -> Path | None`，按
  优先级）：①登记命中 → 该项目目录；②`repo_root == WORKSPACE_ROOT` → 本仓
  全局 `data/learned/`（现状零迁移）；③其余 → None。**评审问题 6（本仓双读）**
  裁定取 (a)：**禁止登记本仓**——`POST/PUT /api/projects` 带
  `repo_root == WORKSPACE_ROOT` → 400，本仓恒走②全局；chat 主 agent 的注入也
  统一走 `resolve(WORKSPACE_ROOT)`（本仓恒全局，「projects.json 是归属唯一真值源」
  口径合一，不存在 chat 读旧目录、task 读项目目录的分叉）。补测试「本仓登记被
  拒 + chat 与 task 读同一份」。消费两处：
  - **注入**：skill agent 与 task run 的 agent 构建时按 repo_root 取目录注入；
    None = 不注入（外部未登记仓不注入，111 归属先例的泛化）。
  - **固化**：`settle_session` 增 `learned_dir` 参数（缺省 `LEARNED_DIR`，
    既有调用方零改动）；`_run_worker` 按 repo_root 归属传参，None 回落
    `LEARNED_DIR`（＝现状：未登记仓的内容固化进本仓全局，明写不改，
    触发信号挂档：首次发现跨仓内容混进全局记忆再议「固化强制登记」）。
    **评审问题 4（078 整理漏换目录）**：`settle_session` 尾部
    `maintain_memory(LEARNED_DIR, …)` 是写死全局——consolidate 与
    maintain_memory **共用同一个 `learned_dir`**，项目目录 settle 后整理只动
    项目目录；补测试钉住。固化管线与「用户确认」红线（098）原样不动，只换
    落盘目录。
- **双模式切换**：`PUT /api/projects/{name}` 带 `memory_mode` + `migrate=true`
  ——迁移＝**复制**三桶到新址（045/064「记忆不可逆」保守：不删源），登记表
  改模式；源目录留档后不再被任何代码读取。就地式创建须
  `confirm_repo_write=true`（114「往工作仓写文件须显式同意」的兑现）。迁移与
  settle 并发写同目录的保护挂触发信号（M2 自用场景；信号＝首例迁移撞上运行中
  run，再议锁）。
- **入口**：M2 验收靠 API（`POST/GET /api/projects`、`PUT /api/projects/{name}`），
  **不新增项目设置页面**——钉子答的是「归属是否隔离」，页面归 M4 宿主层
  （115 前端节同款理由）。

### 4 顺手清四件（M1 报告 §6 待裁决，随 M2 开工清）

| # | 事项 | 本案处置 |
|---|---|---|
| 1 | 62 条发现动态验证 | 已由普通 subagent 补完（结论在 robustness_patrol.md），不入 M2 预算；修复排期不入本案（本案只动自身落点文件） |
| 2 | 12 条低中危登记项 | 登记回活清单，不安排修复，触发信号到再动 |
| 3 | skipped 算终态的语义缺口（115 §1 被批量滥用成假收官后门） | 修订回写 **115 补注四**：done 判定加机械约束——最终活跃计划中 `skipped` 数 ≥ 常量 `PLAN_SKIPPED_LIMIT`（默认 3）→ 不落 done，转 `waiting-steer`。**落点**（评审问题 5 补）：`task.py` 的 done 判定分支（计数口径＝`_verdict_plan` 的活跃/最后归档计划）；**解锁路径**：resume 后须由模型修订计划（skipped 改回 pending 或新建计划），再次判定过闸才落 done；测试三条（3 skipped → waiting-steer / 2 skipped → done / resume 修订后 → done） |
| 4 | spawn_step 无动态证据也自动标 done（spawn.py:342-347 已核） | 不改机制（083 自动回写语义保留）；只加一条纪律：spawn_step 描述与结论串要求「结论须含复核方法/证据，无证据须明说」（**落点：`spawn.py` 描述一行**）；证据机械强制不做（M3 授权档的事） |

> 第 3/4 条是语义裁定，随本案一并提交用户拍板；第 3 条拍板后按「ADR 修订」
> 回写 115 补注四，第 4 条落 spawn_step 描述一行。

## 落点清单（供开工）

| 文件 | 改动 |
|---|---|
| `src/facta/skills.py` | **新**：`SkillSpec` + `load_skill`（节解析+校验）+ `build_skill_registry`（只读锚点注册表）+ `build_skill_agent` |
| `data/skills/review.md` / `data/skills/brief.md` | **新**：review 迁移 + Brief（113 人设原文）；`data/skills/` 进 git |
| `src/facta/review.py` | 删（常量迁入技能文件，引用与测试重锚；`AppContext.build_review_agent` 字段一并删） |
| `src/facta/paths.py` | `SKILLS_DIR`、`PROJECTS_DIR` |
| `src/facta/projects.py` | **新**：`ProjectsManifest`（登记/迁移/`resolve_learned_dir`/原子写） |
| `src/facta/orchestrator/agent.py` | `Agent` 增 `skip_settle: bool = False` |
| `src/facta/orchestrator/assemble.py` | 载入 skills、绑 `build_skill_agent` 工厂（固定 router=None）；`settle_session` 增 `learned_dir`（consolidate 与 maintain_memory 同参）；归属判定接线 |
| `src/facta/orchestrator/task.py` | 顺手件 3：`PLAN_SKIPPED_LIMIT` + done 判定闸（含解锁路径） |
| `src/facta/tools/spawn.py` | 顺手件 4：spawn_step 描述纪律一行 |
| `src/facta/server/app.py` | mode 校验扩技能名、`_agent_for_mode` 改技能分派、`_review_repo` 改名 `_resolve_repo_root`、worker 两条件改预计算 `skip_settle`、repo_root 校验泛化（chat 忽略不变）、`/brief` 路由、项目端点（拒登记本仓） |
| `frontend/brief.html` + `frontend/src/brief/` + `vite.config.mjs` + `static/fw/` | 113 §1 前端五落点 |
| `.gitignore` | `data/projects/` |
| `tests/test_skills.py` / `tests/test_brief.py` / `tests/test_projects.py` | **新**；`tests/test_review.py` 重锚技能文件。评审补列的钉子测试：`_resolve_repo_root` 泛化、skip_settle 异常路径、maintain_memory 同目录、本仓登记被拒、router=None、skipped 闸三条 |

**既有路径不回归**：`/api/runs` 响应字段快照（115）、CLI、github_bot、评测
（mode 缺省 chat 逐字节不变）；frozen-eval 语料钉（data/notes 15 篇）不碰。

## 边界与负决策

- **不做**：Task 显式绑技能字段（M2 只做到 `mode=技能名` + Task.repo_root
  归属；「每任务绑一个技能」完整接线归 M3）；授权档（M3）；项目设置 Web 页（M4）；
  技能热加载（重启生效，MCP 面板同款）；技能间引用/模板库/版本（113 边界；
  清单重复优于引用机制）；自动投递（113/114）；技能使用写工具（M3）。
- **不改**：run_turn 内核与「一轮」语义；固化管线与 098 用户确认红线（只换
  落盘目录）；052 写围栏（就地式 `.facta/` 是显式同意的写，走既有记忆落盘入口）；
  M1 的 Task 状态机/保险丝/分账。
- **M2 不接文件围栏切换**：技能 agent 的文件锚点＝repo_root（111 锚点缝复用），
  但 Task 执行的文件围栏仍归 S6a/WORKSPACE_ROOT 现状——「任务的读写限在项目内」
  的围栏接线随 M3 任务绑项目一起做，M2 钉子只答记忆归属。

## 自我挑刺

| # | 质疑 | 处置 |
|---|---|---|
| 1 | 技能文件格式是不是又一个 DSL？ | 不是：5 个平铺分节、行解析、缺省收敛——AGENTS.md/learned 同款数据行；带 schema/继承/版本才是 DSL，负决策已挡。 |
| 2 | mode 从 Literal 扩成 str 是否丢类型安全？ | 入口 422 校验 + 测试钉住；比枚举省「每技能改两处」，且 mode 本来就是跨边界外部输入。 |
| 3 | projects.json 会不会漂移成第二真值源？ | 它是归属判定的唯一真值源（单文件原子写）；learned_path 落盘推导、读时以盘为准，漂移面已收窄。 |
| 4 | 集中式目录不进 git，项目记忆丢了怎么办？ | 与 tasks/audit 同口径（运行时数据）；「要进版本控制就切就地式」恰是 114 拍板双模式的理由。 |
| 5 | 未登记外部仓固化仍混进全局，钉子②是否没兑现？ | 钉子②只约束「第二个项目的任务」（已登记）；未登记混全局是现状，不恶化即不改（触发信号挂档）。 |

## 待验收

- [ ] 本 ADR 过开工前评审，意见逐条记录处置（见下节）。
- [ ] 三门全绿：ruff / mypy / pytest（新：test_skills/test_brief/test_projects；
      既有 suite 零回归、frozen-eval 语料钉不动）。
- [ ] 钉子①：`data/skills/` 加一个文件 → `mode=该技能名` 真机跑通（curl
      或既有页），全程零 py 改动。
- [ ] 钉子②：登记第二个项目 → 在该项目 repo_root 上跑 task/skill——
      注入只见该项目决定；固化落该项目目录（注入与固化双向隔离测试）。
- [ ] Brief 真机双轨例（113 验收第 2 条）：Brief 落地后的下一个真实任务
      （M3 草案开工前评审）——手工 Brief 与生成 Brief 同一时点独立写，对照
      引用真实性/相关性/过时判定/长度与耗时。
- [ ] review 迁移等价：评审 Agent 工具集/预算/跳固化/锚点行为与 111 验收
      逐项一致（test_review 重锚全绿 + 真机一例）。

## 开工前评审记录

- **评审对象**：本 ADR 草案 v1（2026-11-12 起草）；评审 Agent = deepseek-flash、
  12 轮预算、只读证据菜单（真机 dogfood，run 20261112-234246，终态 done）。
- **评审结论**：**需要修改后再开工**——核心方向与 114/115 一致，5 处影响交付的
  事实错误/落点遗漏 + 2 处口径二义；修订后可开工，不需重开设计。
- **处置表**（8 条问题全部采纳，修订已并入上文对应小节）：

| # | 问题 | 处置 |
|---|---|---|
| 1 | 技能 registry 实为 10 件含 write_note/write_file（草案误写 9 件只读） | 采纳 (a)：registry 收 8 件只读＝REVIEW_TOOLS 同集，写工具不注册（§1，抽验属实） |
| 2 | `_review_repo` 是第三处按名字判断，brief 的 repo_root 会被丢成 None | 采纳：改名 `_resolve_repo_root`，落点清单点名 + 钉子测试（§2） |
| 3 | skip_settle 属性化在 finally 有未初始化引用风险（agent 构造异常 → NameError → sync 半挂） | 采纳：try 前预计算布尔值，finally 只读布尔 + 异常路径测试（§2） |
| 4 | `maintain_memory(LEARNED_DIR)` 写死全局，078 整理漏换目录 | 采纳：consolidate 与 maintain_memory 共用 learned_dir + 测试（§3） |
| 5 | 顺手件 3/4 代码落点（task.py/spawn.py）不在清单；第 3 条缺解锁路径 | 采纳：清单补两行；计数口径与解锁路径写明；测试三条（§4） |
| 6 | 本仓登记绕过「现状零迁移」、chat 与 task 可能双读 | 采纳 (a)：禁止登记本仓（400），chat 注入统一走 resolve（§3） |
| 7 | 「校验泛化到所有 mode」与 chat 忽略语义（test_chat_mode_ignores_repo_root）冲突 | 采纳：收敛为「所有技能 mode，chat 维持 111 忽略」（§2） |
| 8 | router=None 没写进装配口径，review 等价缺一钉 | 采纳：装配固定 router=None + 进等价测试（§1） |

- **可选建议**（5 条，4 采纳 1 挂信号）：H1/文件名一致性校验✅；build_review_agent
  字段删不留兼容✅；新测试落点补列✅；115 补注四文档落点随第 3 条拍板后回写✅；
  migrate 与 settle 并发保护挂触发信号（§3）。
- **评审自报未核实缺口**（本轮工具预算耗尽，非仓库不存在）：consolidate/
  maintain_memory/read_learned 函数正文、ADR 113/114/111 全文、app.py 750 行后
  的 mode 使用点。前两者起草时已精读核过（113 全文、settle_session 调用点）；
  app.py mode 使用点由本案抽验补核（grep 全量：请求模型/分派/worker 两处/
  _review_repo/_task_worker 取 agent，无其余漏网）。
- **状态**：修订后待用户定稿（含顺手件第 3/4 条两处语义裁定），定稿后开工。