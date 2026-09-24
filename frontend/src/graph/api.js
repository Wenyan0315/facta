// 知识图谱面板 API 封装：读全量图 + 重建图谱。
export async function fetchGraph() {
  const resp = await fetch("/api/graph");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function rebuildGraph() {
  const resp = await fetch("/api/graph/rebuild", { method: "POST" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}