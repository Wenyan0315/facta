// 开工 Brief 页 API 封装（ADR 113）：一次同步提交，拿回完整 Brief 文本。
//
// 与 review/api.js 同源复刻（沿 093「第二次出现不抽共享模块」纪律）。
// 走 /api/runs/sync：复用既有 run 机制（写 run_store、会话落盘、SSE 同源），
// mode="brief" 让 worker 选 Brief 技能；repo_root 指定目标仓库根。

async function unwrap(resp) {
  if (resp.ok) return resp.json();
  let message = `HTTP ${resp.status}`;
  try {
    message = (await resp.json()).detail ?? message;
  } catch {
    // 非 JSON 响应体（网关 502 之类）：保留状态码这一条信息
  }
  const err = new Error(message);
  err.status = resp.status;
  throw err;
}

export async function submitBrief({ text, repoRoot, timeout = 900 }) {
  const q = timeout ? `?timeout=${timeout}` : "";
  return unwrap(
    await fetch(`/api/runs/sync${q}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode: "brief", repo_root: repoRoot || null }),
    })
  );
}
