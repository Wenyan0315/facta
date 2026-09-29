# 044 记忆层 MCP 化（A 径：架构可插拔，不独立仓库）

> 状态：**已批准（2026-09-25）**，同日按 roadmap 8.6 对外分档（内部归档）**收窄为只读**（原三工具 → 两个只读工具，写路径挂 tombstone 信号）｜日期：2026-09-25
> 授权出处：roadmap 8.4（内部归档）「可插拔 ≠ 产品化」裁定走 A 不走 B + 8.3 终极检验 + 刺 #9 解（MCP 化提前到 P1，原 P2-3）+ 8.6 对外分档（B-lite 撒饵）
> 前置已清：S8a 边界①收口（`paths.py` 全族锚 `WORKSPACE_ROOT`、stdio 子进程传 `cwd`）——外部 harness 从任意目录拉起记忆服务器不再漂

## 背景与动机

现状：记忆层是**内部直读**的——`build_default_agent` 构建时把 `data/learned/` 三桶 + 用户记忆拼成 system prompt 快照（agent.py:119-204），写入走 `consolidate`（LLM 萃取→审查→硬校验→按 scope 分流）。外部世界拿不到这份记忆：换个 harness（OpenCode / Claude Code / Trae）就是失忆。

8.3 终极检验问题：「如果明天 OpenCode 原生支持可插拔记忆层，my_project1 还剩什么？」合格答案 =「记忆数据和耦合逻辑当晚就能挂上去」。**这个答案要成立，必须有一个开放接口存在**——这就是 A 径的全部理由，不是为了「支持 MCP」这个特性本身。

8.4 已裁定不走 B（独立仓库/对外产品）：三个触发信号一个未兑现。本案只做 A——**记忆层多一个出口，内部实现与数据格式不动**。

## 选项

- **A. 记忆原语包成 MCP stdio 服务器**（`servers/memory_server.py`，复用 `learned.py` / `consolidate._harden`）：一个文件的成本；出口即接口，任何 harness 可挂
- **B. 直接开放 HTTP API**（把 `/api/learned` 那套当成对外接口）：已有实现，但那是面板的私有 API（形状随 UI 变），且要求 agent 服务常驻——外部 harness 挂载 = 依赖一个跑着的 8000 端口，比 stdio 脆
- **C. 只写导出脚本**（learned → 某标准格式，一次性搬走）：最便宜，但只是数据可携带、不是接口可携带；8.3 的答案要的是「挂上去继续用」，不是一次性 dump
- **D. 等第二个用户出现再做**：省当下成本，代价是 8.3 永远没有答案，且届时是「边设计接口边应付外部需求」——比现在按内部形状收口更贵

## 拍板（草案）

**A。B/C/D 都不做**（B 与 A 不互斥但无消费者；C 挂触发信号「真有 harness 要批量导入」；D 是 8.4 已否过的顺序）。

### 1. 暴露哪些原语（**两个只读**，写路径挂信号）

| 工具 | 语义 | 复用的内部原语 |
|---|---|---|
| `memory_recall` | 读长时记忆：按 category/scope 过滤 + 字面 query 过滤，返回条目 + 日期 + 是否已验证 | `learned.read_learned` / `paths.user_memory_path` |
| `memory_list_categories` | 报可用 category/scope 白名单（外部 harness 不用猜） | `consolidate.CATEGORIES` / `SCOPES` |

**写路径 `memory_add` 本案不实现**（原草案有，2026-09-25 按 8.6 收窄删掉）。理由：记忆层写的是**用户资产**——notes 服务器写沙箱写坏了无所谓，记忆写坏了是用户多年积累没了。而现在删除语义只有物理删除（`learned.delete_line`），**没有 tombstone**：外部 harness 写错一条，撤不回、查不到是谁写的。只读版的最坏后果是「读不到/读错，数据无损」，SLA 风险低一个量级；写版的最坏后果是「污染用户资产且不可逆」。

- **触发信号**：P2-1 tombstone（FR 四态状态字段）落地 → 写路径才开，且必须同时带 provenance（来源 harness / 时间戳，P0-8 配套项已有此设计）
- **不是「写路径不重要」**：它恰恰是「治理型个人记忆」定位最有力的展示位（接口自带「什么不许存」）——正因为它有力，不能在没有撤销机制时交出去

**有意不暴露**（与写路径同理，均为范围控制）：

- `consolidate`（固化）——需要 LLM，暴露它就得把路由器/密钥/模型选择拖进服务器进程；固化是内部会话生命周期的事（`settle_session`），外部 harness 没有「一段对话结束」这个钩子
- 会话仓库（`sessions/`）与历史检索——体积大、含完整对话隐私，且形状是内部底片（append-only 消息 + 摘要游标），不是记忆接口
- `compressor` / `plan` / `title` ——会话内部机制，与「可携带的记忆资产」无关
- todos——不是记忆护城河的组成部分；有需求再说（防范围蔓延）

### 2. 只读阶段的治理点：读也走单份真值 + 挂载即授权

写路径撤掉后，本节从「写入受治理」转为两条读侧纪律：

- **读走 `learned.read_learned`，不在服务器里自己 `open()`**——行格式解析、`LEARNED_LOCK`、user_memory 拼接口径都只有一份（同 043「导航逻辑单一真值源」纪律）。绕过它 = 对外接口读到的与内部读到的形状不一致，那天就是漂移的开始
- **挂载即授权，不做二次确认旋钮**：只读不降低隐私暴露面（读本身就是暴露 user scope 记忆给外部进程）。裁定是**用户在自己 harness 里写下这条配置 = 同意**，因此 `memory_recall` 两个 scope 都返回，不加 `allow_user_scope` 这类开关（零消费者的死旋钮，032「文件数>10 才加配置」同款教训）。隐私口径沿用 034/032：红线是**用户级信息不进 repo**，不是「不许自己的 agent 读」

`_harden`（长度 200 / 敏感凭证正则 / scope 白名单）与 `_append`（持锁追加）在本案**不被调用**，降级为「写路径开闸时的前置要求」：tombstone 信号兑现那天，写 handler 必须直接调这两个函数而不是复制逻辑——「治理型个人记忆」定位的第一块实体（接口自带「什么不许存」）到那时才立起来。

### 3. 破例：服务器 import agent 源码（不自包含）

`notes_server.py` 的先例是零依赖自包含。本案**故意打破**：行格式解析与 user_memory 拼接口径（`read_learned`）、白名单（`CATEGORIES`/`SCOPES`）、路径锚（`paths`）必须单份——复制进服务器就是两个真值源，漂移的那天等于对外接口读到的记忆与内部读到的不是一份（043「导航逻辑单一真值源」、042「围栏单一真值」同款纪律）。写路径开闸后，这条纪律还多管一层：`_harden` 不可绕过。

代价：外部 harness 挂载时必须用仓库 venv 的解释器 + 工作目录可达仓库根。两点都已被现成机制吸收——`mcp_config` 的 `{python}` 占位符填 `sys.executable`，`cwd` 锚 `WORKSPACE_ROOT`；对外部 harness 则在其配置文件里写绝对路径解释器（README 一句话的事，但本案不写对外文档，见「不做」）。

### 4. 内部 agent 不消费这个服务器

内部注入路径（system prompt 快照）保持直读不变——自己挂自己会白付一个子进程 + 100ms 探活，且快照语义（构建时一次性）与工具调用语义（每轮按需）本就不同。**这个服务器是纯对外出口。**

风险是「不被自己用的路径会烂」（刺 #8）；缓解 = 测试钉住三个不变量（见验收），不靠日常使用保鲜。

### 5. 格式兼容 pi-mem / pond：口径是「不改」

roadmap:46 记录的唯一格式事实 = 「纯 Markdown/JSONL、零向量库」。现状已同构：learned 是 Markdown 行格式 `- [YYYY-MM-DD] {[已验证] }内容`，无版本号无迁移逻辑。**裁定：本案不改格式、不引入向量库/SQLite、不加 schema 版本号**——兼容的方式是「本来就读得懂」，不是「写转换器」。触发信号 = 真有第二个 harness 需要批量导入/导出时再加导出脚本（C 径）。

## 判定标准（验收用什么说话）

1. **协议自举**：临时把 memory 服务器写进 `mcp_servers.json`，agent 自己挂载后 `mcp__memory_recall` / `mcp__memory_list_categories` 可用（验协议正确性，验完撤掉——不进默认清单，避免每轮启动多拉一个子进程）
2. **写路径不存在**：`tools/list` 只返回上述两个工具；`tools/call` 打 `memory_add`（或任何未知名）返回 `isError=true` 且进程不崩，`data/learned/` 与 `~/.personal-agent/user.md` 字节数不变——这条是收窄为只读的**可验证证据**，不是一句口头承诺
3. **单份真值（读侧）**：往 learned 桶文件手工塞一行 `- [YYYY-MM-DD] 测试条目`，MCP `memory_recall` 与内部 `read_learned`、记忆面板 `/api/learned` 读到同一行（三个入口一份数据）
4. **三道门**：ruff / mypy / pytest 不回退（基线 510 passed, 2 skipped）

外部 harness 实机挂载（OpenCode / Claude Code 任一）= 用户侧验收，不在本案自动化范围内。

## 反方（预写）

1. **没有第二个用户，A 也是提前建设**（刺 #1 摊薄的温和版）——成立一半。反驳：成本是一个 ~150 行服务器文件 + 复用现成原语，而 8.3 的答案必须靠一个存在的接口才成立；且 roadmap 已把 MCP 化排在 P1（刺 #9 解），不是本案临时提级。若实现中发现要动内部记忆层形状才能暴露，**立刻停手回来改草案**——A 的边界是「加出口」，不是「重构内核」
2. **打破 notes_server 自包含先例**——是。但自包含的价值在「配置可移植」，本案的价值在「治理不可绕过」，后者是差异化定位的实体；两者冲突时选治理。代价（挂载要指定解释器）已被 `{python}` / `cwd` 机制吸收
3. **纯出口不被自己用 → 会烂**（刺 #8）——真实风险。缓解只有测试（判定标准 1-3），不做「内部也改走 MCP」的自举表演；接受「这是一条需要偶尔擦的枪」
4. **误读风险：MCP 化 ≠ 把记忆系统换成外部服务**——内部直读路径、固化时机、落盘格式全不动；本案只新增 `servers/memory_server.py` 与 `mcp_servers.json` 的一条可选示例
5. **两个工具是不是太少**——`memory_list_categories` 严格说可以并进 `memory_recall` 的返回（少一个工具）；保留它的理由是 recall 的 category/scope 过滤参数需要合法值白名单，靠报错试探太贵，且写路径开闸那天它已是现成的。若实现时发现 recall 返回里带上白名单更顺，就地合并，不必回炉
6. **只读版是不是等于没做（8.3 要的是「挂上去继续用」）**——最有力的反方，成立一半。缺口是真的：外部 harness 用不上「反哺记忆」，只能消费。反驳分两层：① 8.3 的合格答案是「记忆**数据**和耦合逻辑当晚就能挂上去」，只读已让数据可携带、接口存在，且 `memory_recall` 是外部 harness 唯一真正每轮都要的动作（注入记忆），写回本就发生在会话结束后——而那正是内部 `settle_session` 的钩子位置，外部 harness 没有这个钩子（见「有意不暴露」的 consolidate 条）；② 按 8.6，B-lite 阶段的作用是**撒饵观测信号②**（真有第二个用户来问「能不能挂到我的 harness」），只读足以产生观测机会：真有人来问「能不能写」，那是信号②的更强形态，比我们现在替他们决定「应该有写路径」更可信

## 不做（防范围蔓延）

- 不独立仓库、不发布、不写对外文档/README、不承诺 API 稳定、不修外部 issue（全是 B，挂 8.4 三信号）
- **不暴露写路径 `memory_add`**（挂 tombstone 信号，见第 1 节）；也不暴露删除/改行（`update_line` / `delete_line` 是面板的编辑权，不给外部进程）
- 不做 tombstone / FR 四态 / recall x-ray / 异步 reflect（P2-1 另案；本案只搬运现有原语）
- 不改 learned 行格式、不加 schema 版本号、不写导入导出脚本
- 不加 HTTP/streamable 传输（本地 stdio 够；HttpMcpClient 那套留给远程服务器）
- 不动内部记忆注入路径与固化时机
- 不暴露 todos / sessions / plan / compressor

## 实现清单（批准后执行）

1. `servers/memory_server.py`：照 `notes_server.py` 的 JSON-RPC 骨架（`initialize` / `tools/list` / `tools/call`，protocolVersion `2024-11-05`，业务错误 `isError=true` 不崩进程），只读 handler 调 `learned.read_learned` / `paths.user_memory_path` / `consolidate.CATEGORIES`·`SCOPES` ✅
2. `mcp_servers.json`：加 memory 一条，`enabled: false`——只挂名不拉（`mcp_config.ServerSpec.enabled` 缺省为 `True`，`assemble_servers` 对 false 打一条 info 跳过；不显式写 false 就会每轮多拉一个子进程）✅
3. `tests/test_memory_server.py`：钉判定标准 1-3（协议往返 / 写路径不存在 / 读侧单份真值）✅ 8 passed（另含 registry 挂载与仓库清单两条）
4. 回写：architecture.md（ADR 索引加行 + 版本号 v0.75 + **memory 层行**注记「记忆层已有对外只读出口」——清单原写「93-94 行 MCP 段」，实际改落 memory 层行：memory_server 住 `servers/`，在运行链路 ASCII 图之外，改图要重画框线对齐，不值）、032 裁定三追加删除语义约束注记 + 待确认项①升级、roadmap 8.4 复评段标「A 已落地」+ 8.6 表格与顺序句标完成 ✅
