// 记忆面板 API 封装：三端点对应读/改/删，错误如实上抛。
// 089 起定位键从行号切到稳定 id；update 带 base_content 乐观锁。

// 与 notes/api.js 同源复刻（ADR 093：第二次出现，不抽共享模块）——
// 失败时把后端 detail 文案与状态码带上，组件按 err.status 给处置
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

export async function fetchLearned() {
  return unwrap(await fetch("/api/learned"));
}

export async function updateEntry(category, id, content, baseContent) {
  const resp = await fetch(`/api/learned/${category}/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, base_content: baseContent }),
  });
  if (!resp.ok) await unwrap(resp);
}

export async function deleteEntry(category, id) {
  const resp = await fetch(`/api/learned/${category}/${id}`, { method: "DELETE" });
  if (!resp.ok) await unwrap(resp);
}
