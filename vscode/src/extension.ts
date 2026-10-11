// N04（ADR 104）：VS Code 壳最小可用。本文件是「宿主」不是「UI」——
// 界面 100% 由 facta.server 伺服（单跳 iframe），这里只有三件事：
// 服务生命周期（拉起/探活/回收）、单跳 iframe 壳、剪贴板 IDE 桥。
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

function shellHtml(url: string): string {
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
  u.searchParams.set("id", new URLSearchParams(location.search).get("id") || "");
  u.searchParams.set("vscodeBrowserReqId", String(Date.now()));
  document.querySelector("iframe").src = u.toString();
</script>
</body>
</html>`;
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("facta.open", async () => {
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
        "Facta",
        vscode.ViewColumn.Beside,
        // iframe 内的聊天页自带脚本（app.js/SSE），宿主壳自身零脚本；
        // enableForms：webview 沙箱的 allow-forms 标志会被嵌套 iframe 继承——
        // 不开则壳内输入框打字正常但 Cmd+V 粘贴被沙箱吞掉（实测对照：VS Code
        // 自带 Simple Browser 开了此标志、粘贴可用，2026-10-11 实测定位）；
        // 嵌套 iframe 的显式 sandbox 四标志同样照抄 Simple Browser（对齐后才可粘）
        { enableScripts: true, enableForms: true, retainContextWhenHidden: true },
      );
      panel.webview.html = shellHtml(url);
    }),
    // IDE 桥（剪贴板模式，ADR 104 用户拍板）：跨源 DOM 写入不可行 +
    // 服务器零改动红线 ⇒ 复制格式化上下文，用户粘贴到对话框开头
    vscode.commands.registerCommand("facta.attachContext", async () => {
      const ed = vscode.window.activeTextEditor;
      if (!ed) {
        vscode.window.showInformationMessage("没有打开的活动编辑器");
        return;
      }
      const path = vscode.workspace.asRelativePath(ed.document.fileName);
      const sel = ed.document.getText(ed.selection).trim();
      const text = sel
        ? `正在看：\`${path}\`\n选中：\n\`\`\`\n${sel}\n\`\`\`\n`
        : `正在看：\`${path}\`\n`;
      await vscode.env.clipboard.writeText(text);
      vscode.window.showInformationMessage(
        "上下文已复制，粘贴到 Facta 对话框开头即可",
      );
    }),
  );
}

export function deactivate(): void {
  serverProc?.kill();
}
