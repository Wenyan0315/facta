# ADR 104：N04 VS Code 壳最小可用——单跳 iframe 宿主 + 剪贴板桥 + 端口可配

- 状态：已裁定（2026-10-11，D01 评审三条 + 桥模式经用户拍板）；实现与
  验证读数见文末
- 日期：2026-10-11
- 上游：[097](097-next-milestone-mechanism-closeout-and-product.md) N04、
  architecture 待讨论「VS Code 壳 vs 独立 Web」（336 条：做壳不重写 UI）、
  [D01 评审裁定](../d01/case-1-record.md)（三条约束）、[071](071-review-pending-signals-cashed.md)（数据目录
  env）、[044](044-memory-layer-mcp.md)/[095](095-wheel-distribution-verification.md)
  （两道前置）
- 推翻关系：部分修正 D01 首例草案的两处——数据目录传递（评审①采纳：
  `FACTA_DATA_DIR` env 注入，`workspaceDir`/cwd 语义作废）与桥的形态
  （草案的 webview 按钮 postMessage 注入技术不可行，改剪贴板）

## 设计

### 服务端（仅两处 env，都在入口层）

- `server/__main__.py`：端口从 `FACTA_SERVER_PORT` 读（缺省 8000）——
  D01 评审②裁定「支持可配端口」的落地；host 维持只绑 127.0.0.1（安全
  默认不动）。
- 数据目录零代码改动：`FACTA_DATA_DIR` 通道 071 已有，扩展侧注入即可。

### 扩展（`vscode/`，TypeScript）

- 配置三件：`facta.serverUrl`（**扩展拉起或连接的服务地址**，缺省
  `http://127.0.0.1:8000`——评审②语义）、`facta.pythonPath`（缺省
  `python3`，须为装好 facta 的解释器）、`facta.dataDir`（缺省空=不注入）。
- `facta.open`：GET `/api/status` 探活 → 不通则
  `spawn(python, ["-m", "facta.server"], env={FACTA_DATA_DIR?, FACTA_SERVER_PORT})`
  （端口从 serverUrl 解析；cwd 不传目录职责——评审①）→ 轮询至多 20s
  就绪 → 开 webview 面板「Facta」；探活失败/超时诚实报错（8000 被非
  facta 进程占用属用户侧事，报错即对）。
- **webview 壳 = 单跳 iframe**：VS Code 的 webview 结构上不能直接是
  远程 origin（HTML 串在 `vscode-webview://` 沙箱里跑）；单跳 iframe
  `src=serverUrl` 让聊天 app 在服务 origin 内执行——页内
  EventSource/`/api/*` 同源不受损。评审③「不套 iframe」的**意图是保
  同源**（跨源的是「自定义 origin 再多层嵌套」那条路），单跳 iframe
  正是实现该意图的路径，记此防将来误读。CSP `frame-src` 只放行
  serverUrl。
- `facta.attachContext`（IDE 桥）：读活动编辑器文件名 + 选中区 →
  格式化两行（`` 正在看：`相对路径` `` + 选中代码块）→ 写剪贴板 →
  提示「粘贴到 Facta 对话框开头」。**剪贴板模式系用户拍板**：草案的
  postMessage 注入被跨源 DOM 写入挡死、直写输入框需服务端改动（破
  「服务器零改动」），故采零服务端改动的剪贴板方案，代价是一步粘贴。
- 生命周期：扩展自己拉起的服务进程在 `deactivate` 杀掉；用户手动的
  不管（040 续跑语义兼容——heal 兜底）。

## 边界

- LSP/diff 视图不在本版（097 原文后置）；不发布 marketplace（本地
  `.vsix` 装）；不加 keybinding（命令面板入口够用）。
- 扩展不进 CI（最小版不值得为 ~150 行 TS 加整条 node 工具链 job；
  触发信号＝扩展演进超出最小版）；`node_modules/`、`out/`、`*.vsix`
  进 .gitignore。
- 不造第二个前端真值源：webview 壳只有 iframe，零 UI 复刻（097 验收
  红线）。

## 验收（097 N04 原文）

- [x] 壳里能完成一轮真实对话（2026-10-11 用户实测：vsix 0.1.3 装入
  VS Code，壳内完成真实对话，流式回复正常）。
- [x] 不造第二个前端真值源（结构审查：壳 HTML 仅一个 iframe + CSP，
  零 UI 复刻；「零自有脚本」自 0.1.3 起为例外——3 行引导脚本，见执行校正）。
- [x] IDE 桥两项可用：当前文件路径 + 选中区进上下文——桥命令实测可用
  （剪贴板写入经 `pbpaste` 实读验证，路径 + 选中区格式正确）；粘贴进
  壳内对话框受 VS Code webview 平台限制（Cmd+V 不达嵌套 iframe、
  Simple Browser 同页可粘），挂 [issue #42](https://github.com/Wenyan0315/facta/issues/42)。

## 验证结果

- 三门全绿：ruff 0 error、mypy 64 files clean、pytest **880 passed,
  7 skipped**（879 基线 +1 端口测试）。本机跑 MCP 测试一度撞 087
  已记录的「本机代理劫持 localhost 转 502」环境形状（ICUBE 代理），
  `NO_PROXY=localhost,127.0.0.1` 后全过——环境现象非代码问题，
  以 CI 全绿为准。
- 端口端到端：`FACTA_SERVER_PORT=8123` + mock 档起服务 →
  `/api/status` 正常响应、进程回收干净（评审②裁定的实机兑现）。
- 扩展：`tsc` strict 编译零错误；`vsce package` 产出
  `facta-0.1.0.vsix`（7 files，9.48 KB）；`node_modules/`、`out/`、
  `*.vsix` 已进 .gitignore。
- 待用户实测的两条：壳内真实对话 + 桥命令实测（上表 ①③）——安装
  方式：`code --install-extension vscode/facta-0.1.0.vsix` 或 F5。

## 执行校正（2026-10-11 用户实测收口）

**验收③的粘贴半边被 VS Code webview 平台限制挡住，issue #42 挂档。**

- 实测链路：壳内对话框打字正常、`Cmd+V` 无反应、右键无菜单；**同一页面**
  在 VS Code 自带 Simple Browser 里粘贴正常；系统剪贴板内容正确（`pbpaste`
  实读桥输出 247 字节，路径 + 选中区格式无误）——桥的写入半边坐实可用。
  完整排查记录与触发信号见
  [issue #42](https://github.com/Wenyan0315/facta/issues/42)。
- **三轮对齐尝试（vsix 0.1.1–0.1.3，每轮 reload 后实测，均未解锁粘贴）**：
  ①webview 选项 `enableForms: true`；②iframe 显式 sandbox 四标志
  （`allow-scripts allow-forms allow-same-origin allow-downloads`）；③iframe
  URL 拼 `?id=<webview id>&vscodeBrowserReqId=<ts>` 引导脚本（webview
  service worker 对 localhost 请求按 `?id=` 路由，照 Simple Browser 的
  index.js 抄）。三者保留进 0.1.3（与 Simple Browser 对齐零成本，且 0.1.3
  即实测通过验收①的版本）；根因未定位——Simple Browser 与最小壳的
  差异不止这三处。
- **「零自有脚本」红线破例**：壳 HTML 自 0.1.3 含 3 行引导脚本（读
  location.search 的 id 拼给 iframe src）——性质是装载引导不是 UI，红线
  本意「不造第二个前端真值源」未破；若 issue #42 根因定位后证明脚本
  非必需，收回这道口。
- 验收①③均按实测收口（③带 issue #42 注记）；vsix 现行版本 0.1.3
  （`code --install-extension vscode/facta-0.1.3.vsix`，旧版已清理）。