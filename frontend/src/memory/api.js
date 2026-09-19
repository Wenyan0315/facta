// 记忆面板 API 封装：三端点对应读/改/删，错误如实上抛。
export async function fetchLearned() {
  const resp = await fetch("/api/learned");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function updateEntry(category, line, content) {
  const resp = await fetch(`/api/learned/${category}/${line}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
}

export async function deleteEntry(category, line) {
  const resp = await fetch(`/api/learned/${category}/${line}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
}
