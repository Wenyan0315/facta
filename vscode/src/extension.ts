// N04（ADR 104）：VS Code 壳最小可用。本文件是「宿主」不是「UI」——
// 界面 100% 由 facta.server 伺服（单跳 iframe），这里只有四件事：
// 服务生命周期（拉起/探活/回收）、单跳 iframe 壳、状态栏只读计数（118 §4.2）、
// IDE 上下文直投（107）。
// 三条 D01 评审裁定落点：①数据目录只经 FACTA_DATA_DIR env 注入；
// ②端口经 FACTA_SERVER_PORT 注入（serverUrl 语义=拉起或连接的地址）；
// ③单跳 iframe 保住页内 app 的同源执行（见 shellHtml 注释）。

import * as vscode from "vscode";
import { ChildProcess, spawn } from "child_process";

// 只回收自己拉起的进程；用户手动起的服务不动（040 续跑语义兼容，heal 兜底）
let serverProc: ChildProcess | null = null;

function cfg(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration("facta");
}

function serverUrl(): string {
  return cfg().get("serverUrl", "http://127.0.0.1:8000").replace(/\/$/, "");
}

/** 从 serverUrl 解析端口；URL 无显式端口时返回 null（不注入，服务端回落 8000）。 */
function portOf(url: string): number | null {
  try {
    const p = new URL(url).port;
    return p ? Number(p) : null;
  } catch {
    return null;
  }
}

async function healthy(url: string): Promise<boolean> {
  try {
    const resp = await fetch(`${url}/api/status`, {
      signal: AbortSignal.timeout(1500),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function ensureServer(): Promise<string> {
  const url = serverUrl();
  if (await healthy(url)) return url;   // 已有服务（用户手动的）——直接连
  const python = cfg().get("pythonPath", "python3");
  const dataDir = cfg().get("dataDir", "");
  const port = portOf(url);
  const env: NodeJS.ProcessEnv = { ...process.env };
  if (dataDir) env.FACTA_DATA_DIR = dataDir;       // 评审①：数据目录唯一注入点
  if (port) env.FACTA_SERVER_PORT = String(port);  // 评审②：端口可配
  serverProc = spawn(python, ["-m", "facta.server"], { env, stdio: "ignore" });
  serverProc.on("exit", () => {
    serverProc = null;
  });
  // 至多等 20s：装配含 KB/图谱同步，冷启动不快；超时诚实报错不装死
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if (await healthy(url)) return url;
  }
  throw new Error(
    "服务 20 秒内未就绪（检查 facta.pythonPath 是否指向装有 facta 的解释器）",
  );
}

function shellHtml(url: string, path = "/"): string {
  // VS Code 的 webview 结构上不能直接是远程 origin（HTML 串在
  // vscode-webview:// 沙箱里执行）——单跳 iframe src=服务地址让聊天 app
  // 在服务 origin 内跑，页内 EventSource 与 /api/* 同源不受损；CSP 只
  // 放行该 frame 源。壳内零 UI 复刻（不造第二个前端真值源）。
  //
  // 引导脚本（3 行，为「壳内可粘贴」而破「零自有脚本」口，照 Simple Browser
  // 的 iframe 装载方式对齐）：webview 的 service worker 会接管所有指向
  // localhost/127.0.0.1 的请求并按请求方 URL 里的 ?id= 识别所属 webview
  // （service-worker.js getWebviewIdForClient）；裸 URL 的 iframe 与
  // Simple Browser（id + 防缓存时间戳）路由形态不同，实测粘贴被吞。
  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="frame-src ${url};">
<style>html,body,iframe{margin:0;padding:0;height:100%;width:100%;border:0;display:block}</style>
</head>
<body>
<iframe sandbox="allow-scripts allow-forms allow-same-origin allow-downloads"></iframe>
<script>
  const u = new URL("${url}");
  u.pathname = "${path}";   // ADR 118 §4.1：壳只决定「打开哪个视图」，视图仍全由服务端伺服
  u.searchParams.set("id", new URLSearchParams(location.search).get("id") || "");
  u.searchParams.set("vscodeBrowserReqId", String(Date.now()));
  document.querySelector("iframe").src = u.toString();
</script>
</body>
</html>`;
}

/** 打开服务端某个视图（ADR 118 §4.1）：命令间只差 path 与标题，装配一份。 */
async function openShell(path: string, title: string): Promise<void> {
  let url: string;
  try {
    url = await ensureServer();
  } catch (e) {
    vscode.window.showErrorMessage(
      `Facta 服务不可达：${e instanceof Error ? e.message : String(e)}`,
    );
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    "facta",
    title,
    vscode.ViewColumn.Beside,
    // iframe 内的页面自带脚本（app.js/SSE），宿主壳自身零脚本；
    // enableForms：webview 沙箱的 allow-forms 标志会被嵌套 iframe 继承——
    // 不开则壳内输入框打字正常但 Cmd+V 粘贴被沙箱吞掉（实测对照：VS Code
    // 自带 Simple Browser 开了此标志、粘贴可用，2026-10-11 实测定位）；
    // 嵌套 iframe 的显式 sandbox 四标志同样照抄 Simple Browser（对齐后才可粘）
    { enableScripts: true, enableForms: true, retainContextWhenHidden: true },
  );
  panel.webview.html = shellHtml(url, path);
}

/**
 * 状态栏只读计数（ADR 118 §4.2）：`$(sync) Facta 2⟳ 1⏸`。
 *
 * 只读派生显示——读与面板同一个 `GET /api/tasks`、无写路径、坏了不影响决策，
 * 故不构成第二个前端真值源（操作面仍只在 Web 面板）。30s 一跳 + 单次 1.5s 超时
 * （与 healthy() 同口径）+ 失败静默隐藏；点击＝执行 facta.openTasks。
 */
function createTaskStatusBar(): vscode.Disposable {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100,
  );
  item.command = "facta.openTasks";
  item.tooltip = "Facta 任务：⟳ 运行中　⏸ 待确认（点击打开任务面板）";

  const tick = async () => {
    try {
      const resp = await fetch(`${serverUrl()}/api/tasks`, {
        signal: AbortSignal.timeout(1500),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const tasks = (await resp.json()) as Array<{ status: string }>;
      const running = tasks.filter((t) => t.status === "running").length;
      const waiting = tasks.filter((t) => t.status === "waiting-approval").length;
      const bits = [running && `${running}⟳`, waiting && `${waiting}⏸`]
        .filter(Boolean)
        .join(" ");
      item.text = bits ? `$(sync) Facta ${bits}` : "$(sync) Facta";
      item.show();
    } catch {
      item.hide();   // 服务没起/网络抖动：藏起来，不弹错不打扰编码
    }
  };
  void tick();
  const timer = setInterval(() => void tick(), 30_000);
  return {
    dispose: () => {
      clearInterval(timer);
      item.dispose();
    },
  };
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("facta.open", () => openShell("/", "Facta")),
    // 任务面板：委派/批准/纠偏/取消全在页里（壳不复刻操作面，只换 path）
    vscode.commands.registerCommand("facta.openTasks", () =>
      openShell("/tasks", "Facta · 任务"),
    ),
    createTaskStatusBar(),
    // IDE 桥（ADR 107，推翻 104 的剪贴板模式）：直投服务端单槽信箱。
    // 剪贴板路线在壳内走不通（issue #42：Cmd+V 不达嵌套 iframe，三轮对齐
    // 未解锁），而且它总要用户多做一步；改成 POST 之后服务端在发送那一刻
    // 注入，粘贴不再是链路的一环。
    vscode.commands.registerCommand("facta.attachContext", async () => {
      const ed = vscode.window.activeTextEditor;
      if (!ed) {
        vscode.window.showInformationMessage("没有打开的活动编辑器");
        return;
      }
      // path 照实投不加工：asRelativePath 在工作区内给相对路径，否则原样
      // 返回绝对路径——壳里常打开别的项目，两种都得能用（107「不做围栏」）
      const path = vscode.workspace.asRelativePath(ed.document.fileName);
      const selection = ed.document.getText(ed.selection).trim();
      try {
        const url = await ensureServer();
        const resp = await fetch(`${url}/api/ide/context`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, selection }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${await resp.text()}`);
        vscode.window.showInformationMessage(
          selection
            ? "已投递，回到 Facta 面板发消息即会带上"
            : `已投递（${path}），回到 Facta 面板发消息即会带上`,
        );
      } catch (e) {
        vscode.window.showErrorMessage(
          `投递失败：${e instanceof Error ? e.message : String(e)}`,
        );
      }
    }),
  );
}

export function deactivate(): void {
  serverProc?.kill();
}
