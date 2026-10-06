// 记忆面板 API 封装：三端点对应读/改/删，错误如实上抛。
// 089 起定位键从行号切到稳定 id；update 带 base_content 乐观锁。
export async function fetchLearned() {
  const resp = await fetch("/api/learned");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function updateEntry(category, id, content, baseContent) {
  const resp = await fetch(`/api/learned/${category}/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, base_content: baseContent }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
}

export async function deleteEntry(category, id) {
  const resp = await fetch(`/api/learned/${category}/${id}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
}
