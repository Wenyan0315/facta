// MCP 配置面板 API 封装（106）：一读三写 + 整文件乐观锁（base_hash）。
//
// unwrap 与 notes/memory/api.js 同源复刻（沿 093「第二次出现不抽共享模块」
// 纪律，本处为第三次复刻——抽共享仍非本 PR 靶心，留给下次动 api.js 时）

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

export async function fetchMcp() {
  return unwrap(await fetch("/api/mcp"));
}

export async function addServer(name, body) {
  return unwrap(await fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function updateServer(name, body) {
  return unwrap(await fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function deleteServer(name, baseHash) {
  return unwrap(
    await fetch(`/api/mcp/servers/${encodeURIComponent(name)}?base_hash=${encodeURIComponent(baseHash)}`, {
      method: "DELETE",
    })
  );
}
