# ADR 118：M4 宿主与发布——任务面板 + VS Code 壳增量 + v2.0.0 发布叙事

- 状态：**已裁定**（2026-11-19，用户四项拍板：vsix 版本对齐 2.0.0 / 不建
  CHANGELOG（Release notes 为变更历史真值源）/ 项目设置并入 `/tasks` 折叠区块 /
  扩展进 CI 只跑 `tsc --noEmit`——四项均取本案推荐案，照草案开工）｜日期：2026-11-19
- 上游：[114](114-facta-2-roadmap.md)（2.0 路线图：M4 期定义、验收钉子、五跨越自检要求、
      「切换入口进项目设置」裁定）、[115](115-m1-controllable-long-tasks.md)（M1：任务面板
      明裁归 M4；steering/保险丝/账单三端点为面板供数）、[116](116-m2-bounding-and-skills.md)
      （M2：项目设置 Web 页挂 M4；`/api/projects` 三端点已有；Brief 双轨对照遗留）、
      [117](117-m3-multitask-and-authorization.md)（M3：前端面板与队列可视化明裁归 M4；
      验收观察 3「确认卡未明示写授权文案」、观察 4「主 agent 任务 authorization 易误读」
      挂 M4）、[104](104-n04-vscode-shell-minimal.md)（壳最小版：单跳 iframe、「不重写 UI /
      不造第二个前端真值源」红线、扩展不进 CI 的触发信号、不发 marketplace）、
      [107](107-ide-context-inbox.md)（IDE 上下文信箱：壳内发起→网页侧消费的首例先例）、
      [023](023-frontend-preact-pilot.md)/[094](094-frozen-eval-web-shell.md)/
      [097](097-facta-frontend-architecture.md)（FW 站＝前端唯一真值源、产物进 git + CI
      一致性闸）、[093](093-openai-logo-compliance.md)（`/api/status` 徽章字段语义）、
      [110](110-product-validation-and-engineering-showcase.md)（V03/V04 排 M4 后）
- 调整关系：只落 114 的 **M4 期**（2.0 收官期）。V03 外部探索、V04 工程作品展示另案
  （编号顺延）；发布后若方向变化回 114 修订。115/116/117 挂 M4 的五件事（任务面板、
  项目设置页、确认卡授权文案、authorization 字段展示、队列可视化）由本案一并裁决。

## 背景

现状盘点（读码核对，行号以 2026-11-19 main=`6fc473f` 为准）：

| 能力 | 现状 | 缺口 |
|---|---|---|
| 任务面板（Web） | `/tasks` FW 页存在（app.py:1611 FileResponse），但数据源是 **`GET /api/runs`**（frontend/src/tasks/api.js:3，20s 轮询 App.jsx:20）＝「Run 列表 + 计划下钻」；全 `frontend/` 目录 grep `/api/tasks`、`skill`、`authorization`、`authorized_write` **零命中** | M1–M3 三期的任务生命周期（委派/队列/取消/resume/steering/账单/授权档）在 UI 上**无处可点**：委派任务只能 curl。114 定位「把任务委派给它」缺交互入口 |
| 确认面 | 聊天页有确认卡（static/index.html:45-52 模态 + app.js:120-147 `showConfirm`/`decideConfirm`，SSE 监听 app.js:209），只显示**工具名 + 参数**；FW 站七个入口页**无确认面** | 技能任务在 waiting-approval 时，用户在任务视图看不到「要批什么」，必须知道去聊天页找那条 Run。117 §3 要求确认消息明示「本任务写授权（批准后本任务后续写操作免停）」——`Run.request_confirm(tool, arguments)` 无 kind/message 形参（run_store.py:185、emit 在 :202），文案无承载 |
| VS Code 壳 | `vscode/` 入库 4 文件（package.json 48 行 / tsconfig 14 行 / extension.ts **161 行** / package-lock），扩展版本 0.2.0；两条命令 `facta.open`（单跳 iframe 面板，extension.ts:98-126）+ `facta.attachContext`（IDE 信箱，:127-155）；三项配置 serverUrl/pythonPath/dataDir；无 menus/keybindings/views/StatusBar；**不进 CI**（ci.yml 只有 frontend job，:28-39）；不发 marketplace，本地 `vsce package` 装 | 壳对任务面**零触达**：无「打开任务面板」入口、无运行态可观测（跑没跑、几个在排队、有没有等批准，全要人肉切窗口看网页）。104 挂的触发信号「扩展演进超出最小版（任务面板/诊断接入）」本期兑现，进不进 CI 需裁 |
| 项目设置页 | `GET/POST/PUT /api/projects` 三端点已落地（app.py:1635-1664，116 §2 就地模式 + PUT 切换），有测试（test_projects.py） | 无页面。114 明裁「切换入口进项目设置，不做全局 env」——**切换入口在 UI 上不存在**，双模式实际只能 curl 切 |
| 技能列表 | 技能表运行时装配（skills.py），名字只在 `mode` 校验 422 的报错串里出现 | 无 `GET /api/skills`：委派表单要给用户选技能，没有数据源 |
| 版本号 | 双写：pyproject.toml:7 `version = "0.9.0"` + `__main__.py:19` `VERSION = "0.9.0"`，靠行内注释「保持一致」的人肉纪律；`/api/status`（app.py:1063-1075）不含 version；**git tag 零个、GitHub Release 零个**，pyproject Changelog URL 指向 commits 页 | v2.0.0 要动两处 + vsix 第三处（0.2.0），三者关系未裁；「跑的是哪个版本」在 UI/端点上不可观测；无发布动作先例 |
| 对外材料 | README：「四个面板」（:20）、「Preact 五入口」（:53）、「115 份决策记录」（:13/:156）、「700+ 用例」（:135）；实际＝FW **七**入口、ADR **118** 份、测试 **1002** passed。2.0 的五跨越（任务委派/多任务/技能数据化/授权档/项目记忆归属）在 README **一字未提** | 数字全面滞后 + 2.0 叙事缺席。114 待验收第 2 条要求发布时对照五跨越逐条自检、未兑现的如实标注不宣称 |
| Brief 双轨对照 | 116 待验收唯一未验项，117 明裁「随 M4 一并做」 | 开工时「未做」→ **2026-11-22 已做并收口**：手工 Brief 与生成 Brief 同时点独立写、四维度对照结论通过（见 m4 验收报告 §4）；账目不一致（116 曾注「117 评审时兑现」）一并核销，见报告 O3 |

**架构红线（104/097 复用，本期最容易破的一条）**：壳**不重写 UI、不造第二个前端真值源**；
FW 站是前端唯一真值源，产物进 git + CI 一致性闸不变。M4 的新 UI 一律做在 FW 站，
扩展只增「宿主原生才能做的事」。

## 概念裁定

### 1 宿主分工：面板住 FW 站，壳只做宿主原生增量

**分界线（新裁，用于判定扩展侧任何新增是否越线）**：

| 类别 | 判定 | 本期实例 |
|---|---|---|
| **操作面**（委派/批准/steering/取消/设置） | **只在 Web 面板**，壳内不复刻 | 任务面板、确认卡、项目设置全在 FW 站 |
| **只读派生显示**（读同一端点、无写路径、不含独占信息） | 允许壳原生实现 | 状态栏任务计数（读 `GET /api/tasks` 聚合） |
| **宿主能力**（IDE 才拿得到的上下文/窗口/文件系统） | 壳实现，经端点交接给 Web | 既有 `facta.attachContext`（107 信箱）；本期新增「打开指定视图」 |

理由：097「不造第二个前端真值源」+ 104「单跳 iframe 直连 facta.server」。只读派生显示
不构成第二真值源（数据同源、无写路径、坏了不影响决策）；操作面复刻才是。

### 2 `/tasks` 升级：Task 一等视图（Run 降为下钻）

- **数据源换轨**：`frontend/src/tasks/api.js` 从 `fetchRuns()`（GET /api/runs）改为
  `fetchTasks()`（**GET /api/tasks**，端点已有 app.py:1201）；轮询沿用现状 20s
  （App.jsx:20 `REFRESH_MS`）。**不新建任务级 SSE**（负决策，见自我挑刺 2）。
- **任务卡**（数据全部来自 `to_dict` 现有字段，task.py:140-167，**后端零改动**）：
  状态徽章（queued/running/waiting-approval/waiting-steer/blocked/done/failed/cancelled）、
  goal、skill、continuations/baseline（保险丝余量）、blocked_reason、账单
  （`billing.llm_calls`/`llm_cost`）、`run_ids` 计数、created/updated。
- **操作按钮**（全部复用既有端点）：cancel（POST /api/tasks/{id}/cancel :1222）、
  resume（:1242）、steer（POST /api/runs/{run_id}/steer :1272，`run_id = run_ids[-1]`，
  仅 running/waiting-steer 可点）、查看账单（GET /api/tasks/{id}/billing :1209）。
- **委派表单**（页首，M4 的核心增量）：`goal`（textarea，必填）+ `skill`（下拉，
  数据源＝**新增 `GET /api/skills`**）+ 项目（下拉 `GET /api/projects` + 「自定义路径」
  输入框 → `repo_root`）+ `authorization`（展示技能声明档位；只允许收严为 readonly，
  放宽选项禁用并注「改档位请改技能文件」——117 §3 覆盖规则的前端呈现）→
  `POST /api/tasks`（:1157）。422/400 报错串原样显示（后端已给中文理由，前端不重写校验）。
- **Run 下钻**：展开任务卡 → 列 `run_ids` 的 Run 摘要（GET /api/runs 现状字段）→
  点单条 Run 进既有 `RunDetail.jsx`（计划面板 + `plan.*` SSE，:65-90）**原样复用不改**。
- **authorization 展示收口（117 观察 4）**：`skill == ""` 的任务卡**不显示** authorization
  徽章，改显示「主 agent（全工具池）」；API 字段语义不动（零回归）。

### 3 确认面收口：`confirm.request` 带 kind，两处 UI 一份语义

- **后端最小改动**：`Run.request_confirm(tool, arguments, kind="")` 增第三形参
  （run_store.py:185），emit 载荷加 `"kind": kind`（:202）；app.py:468-475 的 on_confirm
  包装在真挂起那一支传 `WRITE_AUTH_GUARD`（skills.py:61 现成常量，值 `"write-auth"`）。
  普通确认（run_command 动态停 / make_plan 人审）不传 → kind 空串 → **前端零变化**。
  反方登记：也可让前端按「工具名 + 任务 authorized_write」推断档位——否（聊天页无任务
  上下文，且 run_command 常规确认同形会误判）。
- **聊天页确认卡**（static/app.js:120-147）：`kind == "write-auth"` 时多渲染一行
  「本任务写授权：批准后本任务后续写操作免停」，其余不动。
- **任务面板内嵌确认卡**（新）：任务状态为 waiting-approval 且展开时，对
  `run_ids[-1]` 订阅既有 `GET /api/runs/{id}/events`（SSE 断线重放语义已保证
  「request 无对应 resolved 则重新弹窗」，run_store.py:189 注释），渲染 tool/arguments/kind
  文案 + approve/reject 两键 → `POST /api/runs/{id}/confirm`（:1019）。
  未展开时只显示红色徽章「待确认」（数据来自 117 已加的 Task 视图派生字段
  `confirm.request`），不订阅——**订阅数 = 展开的待确认任务数**，不常驻。

### 4 VS Code 壳增量（三项，全部落在「宿主原生」格内）

1. **`facta.openTasks` 命令**：`shellHtml(url)` 加一个 `path` 形参（缺省 `"/"`，
   extension.ts:68-96），新命令传 `"/tasks"`；面板标题 `Facta · 任务`。
   `facta.open` 行为不变（零回归）。**不做** TreeView 任务列表（＝操作面复刻，越线）。
2. **状态栏只读计数**：`$(sync) Facta 2⟳ 1⏸`（running 数 + waiting-approval 数，
   数据源 `GET /api/tasks` 聚合）；30s 一跳、失败静默（不打扰编码）、点击＝执行
   `facta.openTasks`；activate 时启、dispose 时清 interval。反方登记：常驻轮询是新增
   开销——缓解：30s + 单次 fetch 超时 1.5s（沿用 `healthy()` 口径 :32-41）+ 面板打开时
   不额外加密。触发信号＝实测轮询影响续航/服务负载。
3. **委派材料预填**（**纯前端侧，扩展零改动**）：委派表单加载时读
   `GET /api/ide/context`（:934-951），非 null 则在表单里提示「IDE 当前上下文：
   {path}（{lines} 行）」并作为可勾选项拼进 goal（107「发送时刻取走」语义不变——
   任务创建走 `POST /api/tasks`，不经 run 通道，故**只读不取走**，用 DELETE :953 显式清）。

**扩展进 CI（104 触发信号兑现判定）**：本期扩展从 161 行涨到约 200+ 行、命令 2→3、
新增常驻轮询——判定「演进超出最小版」**成立**，ci.yml 增 job `vscode`：
`npm ci && npx tsc -p . --noEmit`（node 20，复用 frontend job 的 setup-node 版本口径）。
**不做**：`vsce package` 进 CI、扩展 e2e（`@vscode/test-electron` 要下 VS Code 二进制，
太重）、vsce 进 devDependencies（外部提供，现状不变）。`.vsix` 仍本地打包、仍 gitignore。

### 5 项目设置：并入 `/tasks` 页的「项目」区块（不新建第八入口）

- 数据源 `GET /api/projects`；每条目显示 repo_root / 名称 / learned_mode（central|in-repo）
  / 是否已登记；操作＝**记忆模式切换**（`PUT /api/projects/{name}` :1653，114「切换入口
  进项目设置」的兑现）+ 就地模式切换时的确认弹窗（往工作仓写文件须显式同意，114 红线）。
- 与委派表单同页复用同一份项目数据（一次 fetch 两处用）。`/memory` 页加一行链接指向
  该区块（记忆归属语义相关，动线补足）。
- 反方登记：项目设置与任务面板职责混页——触发信号＝项目条目字段增长（>5 字段）或
  出现项目级独立操作（如项目级记忆浏览）时拆独立 `/projects` 入口。

### 6 版本与发布：v2.0.0 单点化 + 首个 GitHub Release

- **版本号搬家（消除双写债）**：`src/facta/__init__.py` 放 `__version__ = "2.0.0"`
  （`__init__` 保持零重导入，避免 attr 读取拖依赖）；pyproject 改
  `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "facta.__version__"}`
  （删掉 :7 的硬编码与「保持一致」注释）；`__main__.py:19` 的 `VERSION` 改为
  `from facta import __version__ as VERSION`（CLI 输出与既有引用零变化）。
  真值源从「两处人肉同步」变「一处」。
- **`/api/status` 增 `version` 字段**（app.py:1063-1075，093 徽章语义扩展）：
  「跑的是哪个版本」在端点/前端/壳三处可观测；前端页脚显示。
- **vsix 版本对齐 2.0.0**（0.2.0 → 2.0.0）：单一版本叙事，免除「哪个壳配哪个服务端」的
  支持负担。反方登记：扩展 API 面小、独立演进更诚实——若后续壳与服务端发布节奏分叉，
  再拆独立版本（触发信号＝壳单独发版一次）。
- **发布动作**：`git tag v2.0.0`（附注 tag，body 写五跨越自检结论一行）+ **首个 GitHub
  Release**：body ＝ ①五跨越自检表（逐条标「兑现 / 部分兑现 / 未兑现」，114 待验收第 2 条）
  ②已知边界（摘 architecture.md 未兑现项，不粉饰）③快速开始三段（Web / VS Code 壳 / CLI）
  ④`.vsix` 作为 Release 附件（本地 `vsce package` 产出后手工上传，不进 CI）。
- **不新建 `CHANGELOG.md`**：pyproject 的 Changelog URL 从 commits 页改指 releases 页；
  变更历史真值源＝ADR + Release notes（懒惰阶梯：新文件不如既有载体）。反方登记：开源
  惯例有 CHANGELOG——触发信号＝外部用户要求离线可读的变更历史。
- **不上 marketplace**（104 原样）：触发信号＝外部用户要求一键安装。

### 7 对外材料刷新（发布叙事）

- **README**（双语段落维持现状风格）：数字校正（面板四→七 + 任务面板职责改写、入口五→七、
  ADR 115→118+、用例 700+→1000+）；新增「Facta 2.0」特性段（任务委派 / 多任务队列与
  并发上限 / 技能数据化 / 授权档位 / 项目记忆归属双模式，逐条一句话 + 指向对应 ADR）；
  快速开始补「委派第一个任务」；VS Code 壳段补任务面板与状态栏；已知边界表按 M1–M4
  挂账刷新；**不宣称未兑现项**（114 红线）。
- **architecture.md**：版本 v0.133 → v0.134；决策索引加 118 行；里程碑表补 M4 收官行；
  「各层状态一览」宿主行刷新（壳＝面板 + 状态栏 + 信箱）；已知问题表逐条核对清理
  （111 三条 / 112 两条 / 113 两条 / 115 五条 / 116 / 117 观察项）；待讨论第 359 行
  「VS Code 壳 vs 独立 Web」按本案 §1 分界线收口。
- **不做**：发布文章 / 技术博客（归 110 的 V04 工程作品展示）；截图与演示视频
  （触发信号＝外部用户反馈 README 不足以理解交互）。

### 8 Brief 双轨对照（116 遗留验收，不改码）

- 做法：选一个真实评审对象，**同一时点**独立写两份 Brief——手工一份（不看生成结果）、
  `brief` 技能生成一份（`POST /api/runs` mode=brief，M2 现状路径）；写完后按四维度对照：
  引用真实性（是否指向真实存在的决定/文件/行）、相关性（是否答用户的问题）、
  过时判定（是否误引已被取代的结论）、长度与耗时。
- 产出：对照记录写进 [m4-acceptance-report.md](../../m4-acceptance-report.md)（仓库根，
  与 m1–m3 报告同处），回写 ADR 113 待验收（勾选）+ ADR 116
  待验收（该项关闭）。**不改码**；若对照暴露生成质量缺陷，登记为触发信号（首例「生成
  Brief 误导决策」再动生成轨），不在本期修。

## 落点清单（供开工）

| 文件 | 改动 |
|---|---|
| `src/facta/server/run_store.py` | `request_confirm(tool, arguments, kind="")` + emit 载荷加 kind（:185/:202） |
| `src/facta/server/app.py` | on_confirm 包装传 `WRITE_AUTH_GUARD`（:468-475）；**新增 `GET /api/skills`**（技能名 + authorization + budget + settle，数据源 skills registry）；`/api/status` 加 `version`（:1063-1075） |
| `src/facta/__init__.py` | **新增** `__version__ = "2.0.0"`（零重导入） |
| `src/facta/__main__.py` | `VERSION` 改 import 自 `facta.__version__`（:19） |
| `pyproject.toml` | `dynamic = ["version"]` + attr 取值（删 :7 硬编码）；Changelog URL 改指 releases |
| `frontend/src/tasks/api.js` | `fetchRuns` → `fetchTasks`/`fetchTaskBilling`/`createTask`/`cancelTask`/`resumeTask`/`steerRun`/`confirmRun`/`fetchSkills`/`fetchProjects`/`putProject`/`fetchIdeContext`（全部薄封装，错误如实上抛，现状风格） |
| `frontend/src/tasks/App.jsx` | Run 列表 → Task 列表 + 委派表单 + 项目区块 + Run 下钻；确认卡内嵌（SSE 按展开订阅） |
| `frontend/src/tasks/TaskCard.jsx` | **新增**：任务卡（状态/技能/档位/保险丝/账单/操作按钮/待确认徽章） |
| `frontend/src/tasks/ConfirmCard.jsx` | **新增**：确认卡（tool/arguments/kind 文案 + approve/reject），FW 站唯一确认面 |
| `frontend/src/tasks/NewTaskForm.jsx` | **新增**：委派表单（goal/skill/项目/authorization 只收严） |
| `frontend/src/tasks/ProjectsPanel.jsx` | **新增**：项目设置区块（列表 + 记忆模式切换 + 就地模式确认弹窗） |
| `frontend/src/tasks/RunDetail.jsx` | **不改**（原样复用） |
| `frontend/src/memory/App.jsx` | 加一行链接指向 `/tasks` 项目区块 |
| `src/facta/server/static/app.js` | 确认卡 kind 文案分支（:120-147）；页脚版本（读 `/api/status`） |
| `src/facta/server/static/fw/**` | `npm run build` 产物（CI 一致性闸强制入库） |
| `vscode/src/extension.ts` | `shellHtml(url, path)`；`facta.openTasks` 命令；状态栏计数（30s 轮询 + dispose 清理） |
| `vscode/package.json` | version 2.0.0；commands 加 `facta.openTasks`（category Facta） |
| `.github/workflows/ci.yml` | 新增 `vscode` job（`npm ci && npx tsc -p . --noEmit`） |
| `README.md` / `docs/architecture.md` | 见 §7 |
| `tests/test_skills_api.py`（新）/ `test_app.py` / `test_tasks.py` | `GET /api/skills` 契约、`/api/status` version、confirm.request 带 kind（普通确认 kind 空串）、`request_confirm` 第三形参向后兼容 |
| `tests/test_browser.py` | **新增 Playwright 用例**：委派表单 → 任务卡出现 → 待确认徽章 → 批准 → 状态转 done（沿用 094 真浏览器范式，**不引 vitest**） |

**既有路径不回归**：聊天 Run 全行为、`/api/runs` 响应字段快照（115）、`RunDetail` 计划
面板、IDE 信箱 chip（107）、MCP/笔记/图谱/记忆/评审/Brief 六页、`/api/tasks*` 六端点
契约（M1/M3 测试快照）、`facta.open` 与 `facta.attachContext` 两命令、CLI `VERSION` 输出。

## 边界与负决策

- **不做**：任务级 SSE 端点（20s 轮询 + 按需 per-Run SSE 够用）；队列甘特/时间轴可视化
  （列表 + 徽章）；任务优先级与拖拽排序（FIFO，117 不变）；扩展内 TreeView / 操作面
  （§1 越线）；扩展 e2e 与 marketplace 发布；`CHANGELOG.md`；发布文章与演示视频（V04）；
  前端测试框架引入（vitest/jest——复用 test_browser.py 的 Playwright 范式）；把 static
  聊天页迁进 FW 站（双栈现状保留，登记为技术债：确认卡两处渲染，语义一份、端点唯一，
  触发信号＝第三处需要确认面）；移动端/多语言 UI。
- **不改**：run_turn 一轮语义、S4b 确认缝本体（只加 kind 形参）、S6a 围栏、审计、
  checkpoint、117 队列调度与授权档语义、116 技能装配、Task/Run 状态机与 `to_dict` 契约。
- **不宣称**：五跨越自检里未兑现或部分兑现的条目，README 与 Release notes 如实标注
  （114 待验收第 2 条）；「生产就绪」「多用户」「云端」等词一律不出现（114 负决策）。

## 自我挑刺

| # | 质疑 | 处置 |
|---|---|---|
| 1 | 面板内嵌确认卡 + 聊天页确认卡＝两份 UI，真值源分裂？ | 决策端点唯一（`POST /api/runs/{id}/confirm`）、kind 语义唯一（后端一处发）；UI 两份是双栈现状的既有债，登记触发信号＝第三处确认面 |
| 2 | 20s 轮询下「待确认」最长 20s 才可见，是否违背「壳内完成全生命周期」？ | 徽章延迟可接受（人不是 20s 内必批）；展开即订阅 per-Run SSE，批准路径实时。触发信号＝真实使用中出现「因没看见确认而任务空转到熔断」（117 观察 2 的熔断成本 ¥0.30/单是既有前例） |
| 3 | 状态栏 30s 轮询常驻，是否越「不造第二真值源」线？ | 只读派生（同一端点聚合、无写路径、坏了不影响决策）＝§1 明许类别；开销上限＝每 30s 一次本地 GET。触发信号＝实测影响续航/负载 |
| 4 | `dynamic version` 改法会不会破坏 `pip install -e .` 或 mypy？ | 三门 + 本地 editable 重装实测；`__init__.py` 保持零导入避免 attr 解析拖重依赖；测试钉「`facta.__version__` == `/api/status` version == vsix version」 |
| 5 | 委派表单 repo_root 手填路径错字 → 任务跑在错误围栏里？ | 项目下拉为主、自定义路径为辅；`POST /api/tasks` 现状**不校验 repo_root 是否存在**（app.py:1171）——本期**不加后端校验**（改契约风险 > 收益），前端对自定义路径做「目录不存在」提示（`GET /api/projects` 已登记项优先）。触发信号＝首例路径错字导致越界读写 |
| 6 | 项目设置并入 `/tasks` 是否让页面过载（委派 + 任务列表 + 项目 + Run 下钻 + 确认卡）？ | 项目区块折叠（默认收起）；页面仍是单页单职责（任务视角）。触发信号＝项目字段 >5 或出现项目级独立操作，拆 `/projects` |
| 7 | vsix 跳到 2.0.0 与扩展自身演进节奏不符？ | 单一版本叙事优先（用户视角「一套 Facta」）；反方已登记，触发信号＝壳需单独发版 |
| 8 | v2.0.0 名副其实吗（五跨越逐条核）？ | 自检表进 Release notes：交互形态（委派 + 后台多任务）✓、控制模型（steering + 分档授权）✓、知识模型（四层归属 + 双模式）✓、能力定义（data skills，review/brief 迁移）✓、宿主（壳＝面板 + 状态栏 + 信箱）**本期才兑现**；未兑现项（如 Brief 生成质量对照、hooks 单开案）如实标注 |
| 9 | CI 加 vscode job 值不值（扩展只 ~200 行）？ | 值：现状 161 行**从未被任何自动检查看过**（tsc 都不跑），本期新增常驻轮询与命令；job 成本 ≈ `npm ci`（4 依赖）+ tsc，远小于 frontend job |

## 待验收

- [x] 本 ADR 过开工前评审（评审对象含 §1 分界线的越线判定、§3 kind 形参向后兼容、
      §6 版本号搬家的三处一致性口径）。→ 2026-11-19 用户拍板四项（见状态行），
      其余照草案通过；九条自我挑刺全部采纳处置，无退回条目。
- [x] **Brief 双轨对照**（116 遗留，§8）：同时点独立写、四维度对照记录进
      [m4 验收报告](../../m4-acceptance-report.md)，回写 ADR 113/116 待验收。
      → 2026-11-22 兑现（run `ce974e1e` 生成轨 vs 手工轨，四维度对照结论通过，
      全文见 m4 报告 §4）；对照副产物＝O1 定性发现（「write_file repo_root 不生效」
      系 117 §4 明裁行为非 bug）；113/116/117 账目同 PR 收口（O3）。
- [x] 三门全绿（ruff / mypy / pytest）+ **CI 四 job 全绿**（agent、test×2、frontend
      产物一致性闸）+ **新增 vscode job 绿**。→ PR #70 五 job 全绿；本地全量
      1009 passed / 7 skipped。
- [x] **钉子①：壳内完成任务全生命周期操作**——VS Code 真机：`facta.openTasks` 打开
      面板 → 选技能 + 项目委派一个 write 档任务 → 看到 queued/running → 待确认徽章 →
      面板内批准写授权（确认卡显示授权文案）→ 后续写免停 → 任务转 done → 面板查任务
      账单 → 另一任务 steering 打断一次成功 → cancel/resume 各一次。全程不切浏览器窗口
      （状态栏计数可见运行态）。→ 2026-11-22 验收：全生命周期 API 侧逐环取证
      （确认 kind 双路/计划人审/steering/保险丝/resume×2/cancel/账单，m4 报告 §3.1–3.3），
      壳内 `facta.openTasks` 真机开面板成功（§3.4）；面板内点击微链路因 macOS
      屏幕录制权限未取证（webview 跨源不进 AX 树、截图通道被挡），功能面由
      Playwright 全链路 + API 侧覆盖，如实标注（报告 §3.4/O4）。
- [x] **钉子②：README / 对外材料刷新**——数字与实际一致（面板数/入口数/ADR 数/用例数）、
      2.0 五跨越特性段齐全、五跨越自检表进 Release notes 且未兑现项如实标注、
      已知边界表刷新。→ 2026-11-22 材料逐项核对过（m4 报告 §6）；五跨越自检表
      已成稿（m4 报告 §5），进 Release notes 随「发布」项（p11）执行。
- [x] Playwright：委派表单 → 任务卡 → 待确认徽章 → 批准 → done 全链路一条用例过
      （test_browser.py 范式，CI 恒跑）。→ `test`×2 job 绿。
- [x] 版本单点：`facta.__version__` == pyproject 安装后版本 == `/api/status` version
      == vsix version == `2.0.0`；CLI `VERSION` 输出不回归。→ 四处实测一致
      （`test_version_single_source_of_truth` 钉，m4 报告 §2 ⑥）。
- [x] 确认 kind 链路：write 档任务首写 → `confirm.request` 事件含 `kind="write-auth"`；
      run_command 常规确认与 make_plan 人审 → `kind=""`（前端零变化，M3 行为不回归）。
      → 双路真机取证（m4 报告 §3.1/3.2）。
- [x] 零回归抽查：聊天页确认卡/IDE 信箱 chip/`facta.open`/`facta.attachContext`/
      RunDetail 计划面板/`/api/tasks*` 六端点契约。→ 聊天页 `/` 200、`/api/tasks*`
      六端点真机全覆盖、`runs/sync` `CHAT_OK`；信箱 chip 与 RunDetail 计划面板由
      Playwright + 前端产物闸覆盖（m4 报告 §2 ⑧）。
- [ ] 发布：附注 tag `v2.0.0` + 首个 GitHub Release（自检表 + 已知边界 + 快速开始 +
      `.vsix` 附件）；pyproject Changelog URL 指向 releases 页。→ 素材已齐
      （vsix 已打包实装，自检表 §5），p11 执行（按约定执行前须用户确认）。
- [x] 2.0 收官自检（114 待验收第 2 条）：对照五跨越逐条核，未兑现的在 Release notes
      与 architecture.md 如实标注；V03 外部探索启动前回看 114 定位是否仍成立。
      → 自检表已核（m4 报告 §5：四跨越达成、宿主跨越本期兑现，hooks 等如实标注
      未兑现）；architecture.md 已同步标注；Release notes 段落随 p11；V03 前回看
      114 留作后续触发项。

## 开工前评审记录

- **评审对象**：本 ADR 草案 v1（2026-11-19，PR #69）。
- **评审方法**：草案自查九条质疑（见「自我挑刺」）+ 向用户提交四处需拍板的分歧点
  （vsix 版本策略 / CHANGELOG 载体 / 项目设置住址 / 扩展 CI 档位），逐项给推荐案与反方。
- **评审结论**：2026-11-19 用户四项**全部取推荐案**——①vsix 对齐 `2.0.0`（单一版本
  叙事）；②不新建 `CHANGELOG.md`，变更历史真值源＝ADR + Release notes，pyproject
  Changelog URL 改指 releases 页；③项目设置并入 `/tasks` 折叠区块，不新建第八 FW 入口
  （触发信号＝项目字段 >5 或出现项目级独立操作时拆 `/projects`）；④扩展进 CI 只跑
  `npm ci && npx tsc -p . --noEmit`，不打包、不 e2e、vsce 不进 devDependencies。
  其余章节照草案通过，无退回修订。状态行已回写「已裁定」，实施另开 PR。
