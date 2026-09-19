// API 封装：fetch + 状态检查，错误如实上抛（怎么呈现归调用方）。
export async function fetchRuns() {
  const resp = await fetch("/api/runs");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}
