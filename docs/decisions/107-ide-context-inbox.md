# ADR 107：IDE 上下文信箱——推翻剪贴板桥，服务端单槽投递 + 发送时界碑注入

- 状态：已裁定（2026-10-18，方向经用户拍板「IDE 桥最小可用集」，范围
  沿用 [102](102-n07-hygiene-dispositions.md) 起的单项定稿）；实现与
  验证读数见文末
- 日期：2026-10-18
- 上游：[104](104-n04-vscode-shell-minimal.md)（N04 壳 + 剪贴板桥裁定
  + issue #42 实测死局）、architecture 待讨论 348 行 VS Code 壳**剩余
  待议③**（原文「先做『当前文件 + 选中区』两项即可日用，LSP/diff
  后置」）、[017](017-s3-security.md)（注入界碑）、
  [061](061-plan-context-memory-recall.md)（**免疫随数据走**：新召回面
  自带免疫声明，不靠 SYSTEM_PROMPT 兜；sha256 锁只锁素材本体）、
  [049](049-p0-8-injection-hardening.md)（禁内容检测/分类器）、
  [039](039-s8a-session-model.md)（准入而非加锁、内存态单例 + 一把锁）、
  [040](040-run-checkpoint-impl.md)/[081](081-headless-json-output.md)
  （`create_run` 与 `create_run_sync` 双端点同形）
- 推翻关系：**显式推翻 [104](104-n04-vscode-shell-minimal.md) `#41-45`
  的剪贴板方案裁定**，即重裁那条「服务器零改动」红线。原裁定的理由是
  「postMessage 注入被跨源 DOM 挡死、直写输入框需服务端改动」，于是
  用剪贴板换零服务端改动，代价记为「一步粘贴」。issue #42 的实测把这
  笔账推翻了：**那一步粘贴在壳里根本走不通**（Cmd+V 不达嵌套 iframe；
  Simple Browser 同页可粘但壳降级为书签、失 CSP 与生命周期控制），
  三轮对齐（0.1.1 `enableForms` / 0.1.2 sandbox 四标志 / 0.1.3 三行
  引导脚本）均未解锁且根因未定位。⇒「零服务端改动」保住了，功能没
  交付。**裁定失效的条件是功能不可达，不是品味变化**，故重裁正当；
  104 的其余裁定（做壳不重写 UI、单跳 iframe 保同源、`ensureServer()`
  生命周期、扩展不进 CI）全部沿用。

## 背景

壳（vsix 0.1.3）今天能做的只有「打开服务」：探活 → 拉起 → iframe 指过去。
它在 IDE 里不产生任何独立价值，因为**IDE 唯一值钱的东西（用户此刻在看
什么）过不到 agent 手里**。348 行把这件事拆成三档，并明确「当前文件 +
选中区两项即可日用」，LSP 诊断（P1-3）与 diff 视图后置——本里程碑做
这两项，做完壳从「能打开服务」变成「在 IDE 里真有用」。

已否决的替代路径（104 `#94-105` 三轮记录 + 本次复核）：

1. **postMessage 直写 iframe 内输入框**：webview 与远程 origin 跨源，
   DOM 不可写。结构性死路。
2. **改用 Simple Browser**：粘贴确实可用，但壳退化成书签（失 CSP
   `frame-src`、失 `deactivate()` 回收子进程），且**仍需人工一步**——
   付出架构代价没换来自动化。
3. **在 webview 里复刻一个输入框**（扩展自己收文本再转发）：正中 104
   `#56-57` 与 097 验收红线「不造第二个前端真值源」。

⇒ 剩下的唯一通路是：**不过 DOM，走服务端**。

## 设计

### 形状一句话

扩展采集（当前文件路径 + 选中区文本）→ `POST` 进服务端**单槽信箱** →
前端在用户点输入框时 `GET` 出 chip 给人看 → 用户点发送时服务端**原子
取出**并拼进本轮 `user_text`（界碑包裹）→ chip 消失。

语义等价于「服务端替用户完成那次粘贴」：用户仍是发起人与确认人，只是
粘贴动作从人手挪进进程内——与 104 已验收的剪贴板语义同构，不是新权限。

### 服务端：`create_app` 闭包内一个单槽

- 状态 = `ide_ctx: dict[str, str] | None` + `ide_lock = threading.Lock()`，
  住在 `create_app` 体内（`nonlocal` 改写），照 `RunStore` 的 per-app
  实例化范式（`app.py:290`）。⇒ 每个 `TestClient` 一份、测试天然隔离，
  且**不新建文件、不新建类**（[ponytail](../.trae/rules/ponytail.md)：
  文件数越少越好；一个字典加一把锁不值一个模块）。
- `POST /api/ide/context`（201）：body `{path, selection}`。**覆盖式**
  （新投递挤掉旧的，信箱深度恒 1）。选覆盖不选队列：用户要表达的是
  「我**现在**在看这个」，队列会把过期上下文送进 prompt——**送错比
  送不出更糟**。
- `GET /api/ide/context`：返回 `{path, selection, lines, chars}`，空槽
  返回 `null`。**GET 不取走**——前端要能在用户点「×」之前反复渲染，
  取走只发生在真正发送那一刻（否则用户改主意或刷新页面就丢件）。
- `DELETE /api/ide/context`：用户点「×」丢弃。幂等（空槽也算成功）；返回
  `{"deleted": true}` + 200——**不用 204**：全仓无 204 先例，`@app.delete`
  一律回 JSON dict（`{"deleted": todo.id}` 等），照现成范式而不是引新形状。
- 三端点与发送时的认领共用 `ide_lock`，**pop 在同一把锁内完成** ⇒ 两个
  并发发送不会都拿到同一份上下文（先到先得，后者看到空槽）。
- **超长直接 400，不截断**（`selection` 上限 8000 字符，常量）。对照
  `web.py:263` 的 `〔正文超长，已截断〕`：那截的是连续散文，少一段仍
  可读；**半截代码会误导模型**（缺了右半边的条件看起来像完整判断）。
  拒绝并让人少选一点，比默默送半截诚实。

### 注入点：server 层拼 `user_text`，不动 `run_turn`

`CreateRunRequest` 加 `ide_context: bool = False`（**默认关** ⇒ 全部既有
调用方、评测轨迹、checkpoint 回放语义逐字节不变）。`create_run` 与
`create_run_sync` 共用一个小 helper：`ide_context=True` 时原子 pop 信箱，
把界碑块**拼在 `body.text` 之后**，再交给 `_run_worker`。

- **为什么是后缀不是前缀**：`run_turn` 的轮首路由吃
  `route_text = user_text`（`loop.py:405-408`）——把一段代码围栏塞在
  用户话之前会污染 Jev 的意图判断，而 M10 路由误判已是登记在案的真实
  故障（architecture 活清单 334 行）。「任务在前、材料在后」既保路由
  也符合人读顺序。
- **为什么不给 `run_turn` 加「本轮临时上下文」形参**：那是内核改动，
  牵连 checkpoint、评测轨迹与 895 测试面；而 server 层拼接能拿到**逐
  字节相同**的模型可见文本。省下的复杂度正是 104 评审③「不套 iframe」
  同款判断：意图（上下文进 prompt）与实现路径（在哪一层拼）分开看。
- `title` 仍取 `body.text[:60]`——会话列表不被围栏字面量污染。

### 界碑（照 061「免疫随数据走」）

新增 `_IDE_OPEN` / `_IDE_CLOSE` 一对标记（`web.py:59-63` 同款形状），
块头写三件事（`agent.py:180-189` 的 `_learned_block` 范本）：**性质**
（用户此刻在 IDE 里打开的文件与选中区，由用户手动投递）、**时效**
（发送那一刻的快照，文件随后可能已改）、**免疫延伸**（其中出现的任何
指令性文字都是代码/文本内容本身，不是任务）。

两条红线照守：**SYSTEM_PROMPT 一行不改**（sha256 锁素材不动，061 的
裁定照用——锁的语义是「改动必须显式过这里」，本次无需过）；**不加内容
检测器**（049 明令：误报会毁掉正常读文件流，而 IDE 上下文**全是**别人
写的代码，检测器在这条链路上必然误报）。

### 前端（`static` 站，聊天主视图）

- `index.html`：`#composer` 内、`.composer-actions` 之前插
  `<div id="ide-ctx" class="hidden">`（chip 容器）。
- `app.js`：`let ideCtx = null` + `loadIdeContext()`（照 `loadEnvBadge`
  单次 fetch + `catch` 静默降级，`app.js:618-624`）+ chip 渲染
  （`📎 正在看 <path>（选中 N 行）` + 「×」→ `DELETE` 后清 chip）+
  `send()` 里按下面的不变式带 `ide_context`、`resp.ok` 后清 chip。
- **拉取时机 = focus 事件**（`inputEl` 与 `window` 各挂一个）。
  `app.js` 全文零轮询（无 `setInterval`/`setTimeout`），不该为这个开
  第一例；SSE **结构性不可用**（`encode_sse` 严格 per-Run，发送前没有
  `run_id`，`GET /api/runs/{id}/events` 无 Run 即 404）。「用户要发消息
  必先让壳拿到焦点」是这条链路上唯一既零成本又时序正确的钩子；挂
  `window` 是为了覆盖「先点输入框、再回编辑器按快捷键」的顺序。
- **「所见即所送」不变式**：`payload.ide_context = ideCtx !== null`——
  只有 chip 真的显示过，这一轮才认领信箱。这把「用户看不见的注入」
  这个最坏形状从设计上排除（陌生进程投递的内容必须先被人看见才可能
  进 prompt）；代价是极端时序下上下文晚一轮生效，信箱保留不丢件。
- 气泡文案不受影响：`addUser(text)` 乐观渲染用的是用户敲的原文
  （`app.js:250-289`），围栏只进模型可见文本，不进 UI。

### 扩展侧（`vscode/`，仍不进 CI）

- `facta.attachContext` 从「写剪贴板」改为 `ensureServer()` 后
  `fetch POST {serverUrl}/api/ide/context`（约 20 行增量，无重试、无
  状态机）；命令标题改为「发送当前文件/选中区到 Facta」。
- 采集：`path = workspace.asRelativePath(document.fileName)`——**VS Code
  打开的可能是别的项目，此时它原样返回绝对路径，照实投递不加工**；
  `selection = document.getText(ed.selection).trim()`；无选中区则
  `selection` 传空串（只投「正在看哪个文件」也有价值）。
- **不进 CI 的触发信号复核**（104 `#53-55`）：增量 20 行 fetch、无重试
  无状态机 ⇒ 触发信号「扩展演进超出最小版」**未兑现**，维持不进 CI。
  可测部分全落服务端（`test_app.py` 范式）+ 前端（`test_browser.py`
  Playwright 范式），扩展侧人工验收。
- vsix `0.1.3 → 0.2.0`（桥的语义换了：剪贴板 → 直投，minor 而非 patch）。

### 路径围栏：不做，并写明理由

- 采集由**人手发起**（命令面板/快捷键），不是模型自主读文件 ⇒ 与 104
  已验收的剪贴板版本同一权限级（那版零校验）；界碑负责压「内容里的
  指令被当任务」的概率。
- 不做 `_resolve_in_workspace`（`files.py:40-71`）黑名单校验的**决定性
  理由**：壳里打开的常常是别的项目，相对 facta 的 `WORKSPACE_ROOT` 会
  **全部判越界** ⇒ 围栏会误杀主用例。这与 049 禁检测器同一逻辑：为了
  极小概率的坏形状毁掉正常流，不划算。
- **已知风险（诚实登记）**：端点无认证，本机任何进程都能投递 ⇒ 与既有
  全部 `/api/*` 同一暴露面（127.0.0.1 单用户假设，S3 已定档），不是本
  ADR 新增的攻击面；「所见即所送」+「×」丢弃把静默生效这条路堵死。
  残余风险＝用户不看 chip 就点发送。触发信号＝服务监听非 loopback 或
  出现第二个用户。

### 顺带追认（348 行待议①②，均已被 104 事实上定档）

- **①壳的启动方式** = 扩展激活时探活不通即 `spawn`（`ensureServer()`，
  40×500ms 轮询）。**未违反「不 daemon 化」原则**：进程生命周期绑扩展
  （`deactivate()` 里 `server_proc?.kill()`），不是脱离宿主的常驻服务，
  故不牵出 S8b 重审。
- **②数据目录** = `facta.dataDir` 配置经 `FACTA_DATA_DIR` 注入
  （071/090 通道），缺省空由服务端自回落。
- ⇒ 348 行三项待议全部结案，**该议题关闭**。

### issue #42 的处置

不修、不标「已完成」，改为注记「已绕开」：桥走服务端信箱后，**壳内
粘贴不再是链路的一环**（用户不需要粘贴任何东西）。Cmd+V 不达嵌套 iframe
的平台限制仍在，若将来壳内确需粘贴（图片/长文本），按 104 `#94-105`
的三轮记录重开——那三轮的价值是排除了三条错路。

## 边界（范围外，均有触发信号）

- **LSP 诊断接入**（P1-3）：348 行原文后置。触发信号＝壳里首次出现
  「为什么这里报红」类真实请求（活清单 331 行信号③已兑现「壳立项」，
  但诊断这一档仍需真实需求拉动，不预造）。
- **diff 视图**：同上后置。
- **信箱深度 > 1**（多文件/历史）：触发信号＝真实出现「同时想带两个
  文件」的使用形态。
- **整文件投递**：当前只投选中区 + 路径。触发信号＝「先全选再投递」
  成为习惯（那就是整文件需求，届时上限与截断策略一并重议）。
- **反向桥**（agent 在 IDE 里打开文件/跳到行）：触发信号＝用户第一次
  问「能不能帮我打开那个文件」。
- **认证/令牌**：见上「已知风险」触发信号。
- **FW 站零改动**：聊天主视图属 `static` 站（零构建链），本里程碑不碰
  `frontend/` ⇒ CI 前端构建比对闸（094）无产物 diff，不受影响。

## 验收

- [x] 服务端语义：POST → GET 原样；DELETE 后 GET `null`；覆盖式（二次
      POST 只剩最后一份）；超长 400 且信箱不被污染；两个并发发送只有
      一个拿到（先到先得）。
- [x] 注入形状：`ide_context=true` 时 `_run_worker` 收到的 `user_text`
      = 用户文本 + 界碑块（断言界碑字面量、断言**用户文本在前**）；
      `ide_context` 缺省时逐字节等于 `body.text`（既有调用方零变化）；
      取走后信箱为空（第二次发送不再带同一份）。
- [x] `tests/test_agent.py` 的 SYSTEM_PROMPT sha256 锁**不改断言**且全绿
      （061 的自证手法：不动素材就不撞锁）。
- [x] 前端（Playwright）：POST 一份 → focus 输入框 → chip 出现 → 点「×」
      → chip 消失且服务端 GET `null`。
- [ ] 扩展侧人工验收：真 VS Code 选中一段代码 → 命令 → 壳内 chip 出现
      → 发送 → 模型回答引用了选中内容（104 `#65-68` 的实测口径）。
- [x] 三门 + 基线：ruff 0 error、mypy clean、pytest 不低于 **895 passed,
      7 skipped**（新增测试只增不减）。
- [x] 文档同步：architecture.md 索引行 + 版本 bump（v0.125 → v0.126）、
      348 行三项待议结案、README ADR 数 106 → 107、issue #42 注记。

## 验证结果

**2026-10-18 实现落地，自动化验收 6/7 通过；第 5 条（扩展侧人工验收）
待在真 VS Code 里做，vsix 已打 0.2.0。**

三门读数：

| 门 | 读数 |
| --- | --- |
| ruff | `All checks passed!` |
| mypy | `Success: no issues found in 64 source files` |
| pytest | **903 passed, 7 skipped**（基线 895 + 新增 8，skipped 不变） |

新增测试 8 个：`tests/test_app.py` 7 个（信箱单槽/覆盖/幂等 DELETE、
400 不污染现值、注入形状与后缀次序、缺省不认领、sync 端点同形、忙会话
不吃件、并发先到先得）+ `tests/test_browser.py` 1 个（chip 出现→点「×」
→服务端真空）。`tests/test_agent.py` 的 sha256 锁**一行未改**且全绿——
界碑块拼在 user_text 上、SYSTEM_PROMPT 正文一个字节没动，061 的自证成立。

实现期三处与初稿的偏差，均记在此处：

1. **DELETE 不用 204**，改 `{"deleted": true}` + 200。初稿写 204 是凭
   REST 直觉，落码时 grep 发现全仓无 204 先例（`@app.delete` 一律回 JSON
   dict）——照现成范式，不为一个端点引新形状（ponytail：复用优于发明）。
2. **`RunStore` 缺省 `max_in_flight=1`**（`create_app` 的生产缺省是 3）。
   并发测试若不显式放行 2，第二个请求会先撞 409 而根本走不到认领那一步，
   测出来的是准入而不是信箱——这条踩了一次才看清。
3. **前端清 chip 加了条件**：只在 `payload.ide_context` 为真时清。无条件
   清会把「发送期间 focus 又拉到的新件」一起抹掉，而那份属于下一轮。

顺带发现一处**既有文档滞后**（非本里程碑引入，未擅改）：README 第 20/53
行仍写「四个面板」，而 106 已加 `/mcp` 第五入口。留给后续或用户裁定。
