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

- [ ] 壳里能完成一轮真实对话（用户在 VS Code 里手动验收——安装
  `vscode/facta-0.1.0.vsix` 或 F5 开发宿主实测后勾）。
- [x] 不造第二个前端真值源（结构审查：壳 HTML 仅一个 iframe + CSP，
  零 UI 复刻、零自有脚本）。
- [ ] IDE 桥两项可用：当前文件路径 + 选中区进上下文（命令编译通过，
  VS Code 内实测后勾）。

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