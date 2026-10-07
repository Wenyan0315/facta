# D01 首例材料：VS Code 壳最小可用（097/N04）

> 状态：练习例（098 验证计划标注——方案由本项目 assistant 起草，评审
> 独立性打折，不作为 5 例正式验证之一计数）。材料：目标 / 验收 / 方案
> 三段，交独立会话按 docs/d01/rubric.md 评审。

## 任务目标

给 Facta 加一个 VS Code 壳：在 VS Code 里直接使用现有的 Facta Web 界面
与能力，并接通最小 IDE 桥（当前文件、选中区），让用户不必切出 IDE 就
能跟 Facta 对话。沿用既有裁定「做壳不重写 UI」——扩展只当宿主，四面板
（任务/记忆/图谱/语料）原样复用。

## 验收要求

1. 壳内能完成一轮真实对话（输入 → 工具调用 → 流式回答全程在 VS Code 内）。
2. 不造第二个前端真值源：界面仍是 `facta.server` 提供的现有页面，扩展
   不内嵌任何复刻 UI。
3. IDE 桥两项可用：能把「当前打开文件的路径」与「当前选中区文本」作为
   上下文带进一次对话。
4. LSP 诊断、diff 视图等后续能力明确不在本版范围。

## 待评审方案（草案）

### 结构

新建 `vscode/` 目录（与 `frontend/` 平级），内含一个最小 VS Code 扩展：

```
vscode/
  package.json          # 扩展清单：activationEvents=onCommand:facta.open，
                       # contributes.command "facta.open: 打开 Facta"
  src/extension.ts      # 入口：activate/deactivate
  src/bridge.ts         # IDE 桥：读当前文件与选中区
  webview/index.html    # 薄壳：iframe 加载 http://127.0.0.1:8000
```

### 服务生命周期

扩展激活时检查 `http://127.0.0.1:8000/api/status` 是否可达；不可达则用
`child_process.spawn` 拉起 `python -m facta.server`（从 `.env` 或扩展配置
读工作目录），等待端口就绪后再打开 webview。deactivate 时杀掉自己拉起
的进程；不是自己拉起的（用户手动开的）不杀。

### IDE 桥

- `bridge.ts` 提供 `getCurrentFile()` / `getSelection()` 两个函数，经
  `acquireVsCodeApi().postMessage` 与 webview 内脚本通信。
- webview 页面里加一个「带上当前文件/选中区」按钮：点击后把
  「正在看：`<path>`」与「选中：`<text>`」两行文本拼进聊天输入框开头，
  用户可改可删，随消息一起发出。
- 服务器零改动——桥是纯客户端拼装，不新增任何 API。

### 配置与打包

- 扩展配置项：`facta.serverUrl`（默认 `http://127.0.0.1:8000`）、
  `facta.workspaceDir`（服务器的工作目录，默认当前工作区根）。
- 用 `vsce package` 打成 `.vsix`，本地安装使用；暂不发布 marketplace。
- TypeScript 编译产物进 `vscode/out/`，npm 依赖只有 `@types/vscode`。

### 验证方法

1. 在扩展开发宿主（F5）里打开 webview，确认聊天页能出流式回答。
2. 打开任一文件、选中一段文本，点桥按钮，确认输入框出现两行上下文。
3. 关闭 VS Code，确认自己拉起的服务进程被回收（`ps` 检查）。
4. `npm run build --prefix frontend` 与 `pytest -q` 全绿（壳不碰它们，
   跑一遍防意外）。
