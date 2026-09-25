// 知识语料面板 API 封装（042）：列表 / 读取 / 保存 / 同步向量库。
//
// 与 memory/api.js 只差一处：这里要能区分 409。409 = 磁盘上的版本在用户编辑期间
// 被改过（多半就是用户自己在 IDE 里改的同一篇），UI 的出路是「重新载入」而不是
// 重试。所以给 Error 挂 status，并把服务端的中文 detail 原样带上来——那些消息
// 本来就是写给人看的。
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

export async function fetchNotes() {
  return unwrap(await fetch("/api/notes"));
}

export async function fetchNote(name) {
  return unwrap(await fetch(`/api/notes/${encodeURIComponent(name)}`));
}

// baseHash 是乐观锁凭证：把载入时拿到的 hash 原样送回，服务端与磁盘现值比对。
export async function saveNote(name, content, baseHash) {
  return unwrap(
    await fetch(`/api/notes/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, base_hash: baseHash }),
    }),
  );
}

export async function syncNotes() {
  return unwrap(await fetch("/api/notes/sync", { method: "POST" }));
}
