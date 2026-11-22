// TaskCard（ADR 118 §2）：Task 一等视图的一张卡。
//
// 数据全部来自 `Task.to_dict` 现有字段 + `_task_view` 的 waiting-approval 派生
// （后端零改动）；操作全部复用既有端点（cancel / resume / steer / billing /
// confirm）。展开才挂 Run 下钻与确认卡 ⇒ SSE 订阅数随展开数走，不常驻。
import { useState } from "preact/hooks";
import { cancelTask, fetchTaskBilling, resumeTask, steerRun } from "./api.js";
import ConfirmCard from "./ConfirmCard.jsx";
import RunDetail from "./RunDetail.jsx";

const STATUS_ZH = {
  queued: "排队中",
  running: "运行中",
  "waiting-approval": "待确认",
  "waiting-steer": "待纠偏",
  blocked: "已熔断",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};
// 与 CSS 类名同形（.task-badge.<status>）：Run 状态与 Task 状态两套词汇，
// 这里各给一个色，不共用（waiting_approval 是 Run 的下划线版，别混）
const TERMINAL = ["done", "failed", "cancelled"];

function money(total) {
  const cost = total?.llm_cost;
  const calls = total?.llm_calls;
  if (!cost && !calls) return "—";
  return `${calls ?? 0} 次调用 / ¥${(cost ?? 0).toFixed(4)}`;
}

// updated_at 是 epoch 秒（Task.to_dict），不是 ISO 串——按本地时刻渲染成
// 「2026-11-19 22:14」够读了（秒级精度对 20s 轮询的面板没意义）
function stamp(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function TaskCard({ task, runs, onChanged }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [steerText, setSteerText] = useState("");
  const [billing, setBilling] = useState(null);   // null = 未展开明细（列表用 to_dict 里的合计）
  const [openRun, setOpenRun] = useState(null);

  const lastRunId = task.run_ids?.[task.run_ids.length - 1] || null;
  const steerable = ["running", "waiting-steer"].includes(task.status) && lastRunId;
  const resumable = ["waiting-steer", "blocked"].includes(task.status);

  // 一次一个动作：失败文案就地显示（ADR 093「诚实降级做成可见」）
  const act = async (fn) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onChanged?.();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const showBilling = () =>
    act(async () => {
      setBilling(billing ? null : await fetchTaskBilling(task.task_id));
    });

  const myRuns = (runs || []).filter((r) => task.run_ids?.includes(r.run_id));

  return (
    <div class="task-item" data-testid="task-card" data-status={task.status}>
      <div class="task-head" onClick={() => setOpen(!open)}>
        <span class={`task-badge ${task.status}`}>
          {STATUS_ZH[task.status] || task.status}
        </span>
        <span class="task-title">{task.goal}</span>
        {task.status === "waiting-approval" ? (
          <span class="task-badge tp-alert" data-testid="pending-badge">待确认</span>
        ) : null}
        {task.fuse_fired ? <span class="task-badge failed">保险丝已熔断</span> : null}
        <span class="task-caret">{open ? "▾" : "▸"}</span>
      </div>

      {/* 117 观察 4：无技能任务不显示档位徽章（authorization 字段对它是空语义），
          改说「主 agent（全工具池）」——API 契约一字不动，只改呈现 */}
      <div class="tp-meta">
        {task.skill ? (
          <>
            <span class="tp-chip">技能：{task.skill}</span>
            <span class="tp-chip">
              档位：{task.authorization === "write" ? "write（可写）" : "readonly（只读）"}
            </span>
            {task.authorized_write ? <span class="tp-chip tp-ok">本任务写已授权</span> : null}
          </>
        ) : (
          <span class="tp-chip">主 agent（全工具池）</span>
        )}
        <span class="tp-chip">续跑 {task.continuations} 次</span>
        <span class="tp-chip">Run {task.run_ids?.length ?? 0} 条</span>
        <span class="tp-chip">账单：{money(task.billing?.totals)}</span>
        {task.repo_root ? <span class="tp-chip">仓：{task.repo_root}</span> : null}
      </div>
      {task.blocked_reason ? <div class="tp-blocked">熔断原因：{task.blocked_reason}</div> : null}

      <div class="tp-actions">
        {steerable ? (
          <button
            class="tp-btn"
            disabled={busy}
            onClick={() => act(() => steerRun(lastRunId, steerText))}
          >
            纠偏
          </button>
        ) : null}
        {steerable ? (
          <input
            class="tp-steer"
            placeholder="给在飞的 Run 改向（下次模型调用前生效）"
            value={steerText}
            onInput={(e) => setSteerText(e.target.value)}
          />
        ) : null}
        {resumable ? (
          <button class="tp-btn" disabled={busy} onClick={() => act(() => resumeTask(task.task_id))}>
            恢复
          </button>
        ) : null}
        {!TERMINAL.includes(task.status) ? (
          <button
            class="tp-btn tp-btn-danger"
            disabled={busy}
            onClick={() => act(() => cancelTask(task.task_id))}
          >
            取消
          </button>
        ) : null}
        <button class="tp-btn" disabled={busy} onClick={showBilling}>
          {billing ? "收起账单" : "查看账单"}
        </button>
        <span class="muted tp-time">更新 {stamp(task.updated_at)}</span>
      </div>

      {err ? <div class="tp-err">{err}</div> : null}

      {billing ? (
        <div class="tp-billing">
          <div class="detail-muted">
            合计：{money(billing.totals)}
            {billing.approx_runs?.length
              ? `（其中 ${billing.approx_runs.length} 条 Run 为并发近似值）`
              : ""}
          </div>
          {billing.runs?.map((r) => (
            <div class="tp-billing-row" key={r.run_id}>
              <code>{r.run_id.slice(0, 8)}</code> {money(r.usage)}
            </div>
          ))}
        </div>
      ) : null}

      {open ? (
        <div class="tp-detail">
          {task.status === "waiting-approval" && lastRunId ? (
            <ConfirmCard runId={lastRunId} onDecided={onChanged} />
          ) : null}
          <div class="detail-muted">本任务的 Run（点条目看计划面板）</div>
          {myRuns.length === 0 ? (
            <p class="muted detail-muted">尚无 Run（排队中，或本服务进程重启前创建）</p>
          ) : null}
          {myRuns.map((run) => (
            <div class="tp-run" key={run.run_id}>
              <div class="task-head" onClick={() => setOpenRun(openRun === run.run_id ? null : run.run_id)}>
                <span class={`task-badge ${run.status}`}>{run.status}</span>
                <span class="task-title">{run.title || "(无标题)"}</span>
                <span class="task-caret">{openRun === run.run_id ? "▾" : "▸"}</span>
              </div>
              <div class="task-preview">{run.preview || "（无交付摘要）"}</div>
              {openRun === run.run_id ? <RunDetail runId={run.run_id} /> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
