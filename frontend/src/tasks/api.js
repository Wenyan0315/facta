// API 封装：fetch + 状态检查，错误如实上抛（怎么呈现归调用方）。
//
// ADR 118 §2：本页数据源从 Run 换轨到 Task——`GET /api/tasks` 是一等视图，
// Run 降为任务卡展开后的下钻。写路径大部分复用 M1/M3 既有端点（cancel/resume/
// steer/confirm/tasks）；线五①起新增任务沙箱收口两端点（worktree）。
async function unwrap(resp) {
  if (!resp.ok) {
    // 后端错误体两种形状：HTTPException = {detail:"中文理由"}；
    // pydantic 校验 = {detail:[{...}]}。原样上抛（118 §2：不重写校验文案）。
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* 非 JSON 响应体：保留状态码文案 */
    }
    const err = new Error(detail);
    err.status = resp.status;   // 调用方按状态码分流（如 settle 409 = 预览失效）
    throw err;
  }
  return resp.json();
}

const get = (url) => fetch(url).then(unwrap);
const post = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }).then(unwrap);
const put = (url, body) =>
  fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }).then(unwrap);

// ---- 读 ----
export const fetchTasks = () => get("/api/tasks");
export const fetchRuns = () => get("/api/runs");   // Run 下钻摘要（既有契约不动）
export const fetchTaskBilling = (taskId) => get(`/api/tasks/${taskId}/billing`);
export const fetchTaskWorktree = (taskId) => get(`/api/tasks/${taskId}/worktree`);
export const fetchSkills = () => get("/api/skills");
export const fetchProjects = async () => (await get("/api/projects")).projects;
export const fetchStatus = () => get("/api/status");
export const fetchIdeContext = () => get("/api/ide/context");   // 空槽返回 null

// ---- 写 ----
export const createTask = (body) => post("/api/tasks", body);
export const cancelTask = (taskId) => post(`/api/tasks/${taskId}/cancel`);
export const resumeTask = (taskId, text = "") => post(`/api/tasks/${taskId}/resume`, { text });
export const steerRun = (runId, text) => post(`/api/runs/${runId}/steer`, { text });
export const confirmRun = (runId, approve) => post(`/api/runs/${runId}/confirm`, { approve });
// settle 带 candidate 钉预览版本（S2-3 候选比对，不一致后端回 409）；缺省不发该键
export const settleTaskWorktree = (taskId, action, candidate) =>
  post(`/api/tasks/${taskId}/worktree`, { action, ...(candidate ? { candidate } : {}) });
export const putProject = (name, body) => put(`/api/projects/${name}`, body);
// 107「发送时刻取走」语义只作用于 run 通道；任务创建不经它，故信箱靠显式 DELETE 清
export const clearIdeContext = () => fetch("/api/ide/context", { method: "DELETE" }).then(unwrap);
