// TaskCard（ADR 118 §2）：Task 一等视图的一张卡。
//
// 数据全部来自 `Task.to_dict` 现有字段 + `_task_view` 派生；操作全部复用既有
// 端点（cancel / resume / steer / billing / confirm / worktree）。展开才挂 Run
// 下钻与确认卡 ⇒ SSE 订阅数随展开数走，不常驻。
// P0（S1-6/S2-5）：+绑定信息区块（工作区/目标分支/基线/执行方式）、三态分列
// （运行状态｜检查结果｜合入状态）、四段 diff 查看、收口钉 candidate 与 409 重新核验。
import { useState } from "preact/hooks";
import {
  cancelTask,
  fetchTaskBilling,
  fetchTaskWorktree,
  resumeTask,
  settleTaskWorktree,
  steerRun,
} from "./api.js";
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

// 历时：started_at → updated_at（终态）或此刻（在飞）。resume 会重置
// started_at（保险丝基线），所以这里报的是「本轮」历时，不是任务总寿命
function duration(task) {
  if (!task.started_at) return "—";
  const end = ["done", "failed", "cancelled"].includes(task.status)
    ? task.updated_at || task.started_at
    : Date.now() / 1000;
  let s = Math.max(0, Math.round(end - task.started_at));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  s = s % 60;
  return h ? `${h}时${m}分` : m ? `${m}分${s}秒` : `${s}秒`;
}

// Token 合计：tokens_in + tokens_out（prompt cache 命中/未命中都算在 in 里，
// 不重复计）。k/M 缩写防 chip 撑爆卡片
function tokens(total) {
  const n = (total?.tokens_in ?? 0) + (total?.tokens_out ?? 0);
  if (!n) return "—";
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
}

// updated_at 是 epoch 秒（Task.to_dict），不是 ISO 串——按本地时刻渲染成
// 「2026-11-19 22:14」够读了（秒级精度对 20s 轮询的面板没意义）
function stamp(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 基线/合入 commit 读验用短 SHA（8-12 位取 10）；空串如实不显示
const shortSha = (sha) => (sha ? String(sha).slice(0, 10) : "");

// 验证记录的 time 格式契约未钉（epoch 秒 / ISO 串都可能）：数字走 stamp，其余原样
const fmtTime = (t) => (typeof t === "number" ? stamp(t) : t || "—");

// 执行方式三态（_task_view.exec_mode）
const EXEC_ZH = {
  sandbox: "沙箱隔离",
  direct: "无隔离 · 直接在仓库工作",
  default: "默认工作区",
};

// 合入状态四态（S2-3）；徽标配色沿用 task-badge 既有变体，不新造色
const MERGE_ZH = { none: "未合入", merged: "已合入", conflicted: "冲突", discarded: "已丢弃" };
const MERGE_CLS = { none: "pending", merged: "done", conflicted: "blocked", discarded: "cancelled" };

const VERIF_ZH = { passed: "通过", failed: "失败", not_run: "未运行", skipped: "跳过" };

// 产物四段（S2-1）：label 用 spec 用语
const SEGMENTS = [
  ["committed", "已提交未合入"],
  ["staged", "已暂存"],
  ["unstaged", "未暂存"],
  ["untracked", "未跟踪"],
];

// 一段 diff 文本按 unified diff 的 `diff --git` 行切成文件块（文件级可展开）；
// 不带该标记的（stat 摘要 / 未跟踪清单）整段一块兜底展示
function fileChunks(text) {
  const chunks = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      chunks.push({ name: line.split(" b/").pop(), lines: [line] });
    } else if (chunks.length) {
      chunks[chunks.length - 1].lines.push(line);
    } else {
      chunks.push({ name: null, lines: [line] });
    }
  }
  return chunks;
}

// diff 呈现：四段分段展示；unified diff 切到文件级展开；segments 缺失回落兼容串
function DiffView({ wt }) {
  if (!wt.segments) return <pre class="tp-changes">{wt.changes || "（无改动）"}</pre>;
  return (
    <div data-testid="task-diff">
      {wt.baseline_inferred ? <div class="tp-warn">基线为推断值</div> : null}
      {SEGMENTS.map(([key, label]) => {
        const text = wt.segments[key] || "";
        if (!text) return <div class="detail-muted muted">{label}：（无）</div>;
        return (
          <details key={key} open>
            <summary class="detail-muted">{label}</summary>
            {fileChunks(text).map((c, i) =>
              c.name ? (
                <details key={i}>
                  <summary class="tp-chip">{c.name}</summary>
                  <pre class="tp-changes">{c.lines.join("\n")}</pre>
                </details>
              ) : (
                <pre class="tp-changes" key={i}>{c.lines.join("\n")}</pre>
              )
            )}
          </details>
        );
      })}
    </div>
  );
}

export default function TaskCard({ task, runs, onChanged }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [steerText, setSteerText] = useState("");
  const [billing, setBilling] = useState(null);   // null = 未展开明细（列表用 to_dict 里的合计）
  const [openRun, setOpenRun] = useState(null);
  const [wt, setWt] = useState(null);             // null = 未拉 GET worktree（全量：segments/candidate/…）
  const [settleR, setSettleR] = useState(null);   // settle 响应（旧键 result/cleared 与新键 status/detail 都收）
  const [stale, setStale] = useState(false);      // 预览失效（候选已变）：合入冻结在按钮态待重新核验
  const [checksOpen, setChecksOpen] = useState(false);   // 检查明细展开

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

  // 线五①：沙箱收口——改动摘要按需拉取（四段 + candidate）；合并/丢弃后把结果留给人看
  const loadWt = () =>
    act(async () => {
      if (wt !== null) {
        setWt(null);
        return;
      }
      setWt(await fetchTaskWorktree(task.task_id));
    });

  // 预览失效后的「重新核验」：重拉预览并解冻合入按钮（核验结论归用户）
  const recheck = () =>
    act(async () => {
      setStale(false);
      setSettleR(null);
      setWt(await fetchTaskWorktree(task.task_id));
    });

  // 收口：merge 带 GET 预览的 candidate 钉版本；409「重新核验」语义转按钮态提示
  const settleWt = (action) =>
    act(async () => {
      try {
        const r = await settleTaskWorktree(task.task_id, action, wt?.candidate || task.candidate);
        setSettleR(r);
        setStale(false);
        setWt(null);
      } catch (e) {
        if (/重新核验/.test(e.message) || (e.status === 409 && /失效/.test(e.message))) {
          setStale(true);
          setSettleR(null);
          setWt(await fetchTaskWorktree(task.task_id));   // 预览顺手刷新
          return;
        }
        throw e;   // 其余错误走 act 的就地报错
      }
    });

  // 三态分列（S2-5）：运行/检查/合入各自独立，运行完成 ≠ 已合入
  const verifs = Array.isArray(task.verifications) ? task.verifications : [];
  const passed = verifs.filter((v) => v.result === "passed").length;
  const failed = verifs.filter((v) => v.result === "failed").length;
  const notRun = verifs.length - passed - failed;
  const mergeStatus = task.merge_status || "none";
  const mergeLabel =
    mergeStatus === "merged"
      ? `已合入${task.merge_commit ? ` ${shortSha(task.merge_commit)}` : ""}`
      : MERGE_ZH[mergeStatus] || mergeStatus;
  // 清理失败（Spec 2.7）：settle 明报（merged 但没清干净），或处置记录留痕（重启后仍可见）
  const cleanupFailed =
    (settleR?.status === "merged" && settleR?.cleared === false) ||
    /清理失败/.test(`${task.disposition?.status ?? ""}${task.disposition?.detail ?? ""}`);

  // 绑定信息（S1-6）：旧任务字段全空 → 「未绑定（旧任务）」；exec_mode 缺失按旧字段派生兜底
  const bound =
    task.repo_root || task.target_branch || task.baseline || task.worktree_branch || task.worktree;
  const execMode =
    task.exec_mode || (task.worktree ? "sandbox" : task.repo_root || task.skill ? "direct" : "default");

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
        <span class="tp-chip">历时 {duration(task)}</span>
        <span class="tp-chip">Token {tokens(task.billing?.totals)}</span>
        <span class="tp-chip">账单：{money(task.billing?.totals)}</span>
        {task.worktree ? (
          // 线五①：沙箱可见性——终态=待收口（给动作），在飞=隔离中（只告知）
          <span class="tp-chip tp-ok" data-testid="sandbox-chip">
            {TERMINAL.includes(task.status) ? "沙箱待收口" : "沙箱隔离中"}
          </span>
        ) : null}
      </div>

      {/* S1-6 绑定信息：实际工作区（仓 + 沙箱路径）/目标分支/基线短 SHA/worktree 分支/
          执行方式徽标；旧任务字段全空如实标「未绑定（旧任务）」，不崩 */}
      <div class="tp-meta" data-testid="task-binding">
        {bound ? (
          <>
            {task.repo_root ? (
              <span class="tp-chip" style="word-break:break-all">仓：{task.repo_root}</span>
            ) : null}
            {task.worktree ? (
              <span class="tp-chip" style="word-break:break-all">沙箱：{task.worktree}</span>
            ) : null}
            {task.target_branch ? <span class="tp-chip">目标分支：{task.target_branch}</span> : null}
            {task.baseline ? <span class="tp-chip">基线：{shortSha(task.baseline)}</span> : null}
            {task.worktree_branch ? <span class="tp-chip">工作分支：{task.worktree_branch}</span> : null}
            <span class={`tp-chip ${execMode === "sandbox" ? "tp-ok" : ""}`}>
              执行方式：{EXEC_ZH[execMode] || execMode}
            </span>
          </>
        ) : (
          <span class="tp-chip">未绑定（旧任务）</span>
        )}
      </div>

      {/* S2-5 三态分列：运行状态｜检查结果｜合入状态——互不冒充，运行完成 ≠ 已合入 */}
      <div class="tp-meta" data-testid="task-tri">
        <span class="tp-chip">
          运行状态 <span class={`task-badge ${task.status}`}>{STATUS_ZH[task.status] || task.status}</span>
        </span>
        <span class="tp-chip" data-testid="tri-checks">
          检查结果{" "}
          {verifs.length ? (
            <>
              {passed} 通过 / <span style="color:#b91c1c">{failed} 失败</span> /{" "}
              <span style="color:#b45309">{notRun} 未运行</span>
            </>
          ) : (
            "无检查记录"
          )}
        </span>
        <span class="tp-chip" data-testid="tri-merge">
          合入状态 <span class={`task-badge ${MERGE_CLS[mergeStatus] || "pending"}`}>{mergeLabel}</span>
          {cleanupFailed ? <span class="task-badge failed">清理失败</span> : null}
        </span>
        {verifs.length ? (
          <button class="tp-btn tp-btn-mini" disabled={busy} onClick={() => setChecksOpen(!checksOpen)}>
            {checksOpen ? "收起检查明细" : "检查明细"}
          </button>
        ) : null}
      </div>
      {/* 检查明细：failed/not_run 如实着色不装绿，每条可定位检查名/结果/候选 SHA */}
      {checksOpen ? (
        <div class="tp-billing" data-testid="tri-checks-detail">
          {verifs.map((v, i) => (
            <div class="tp-billing-row" key={i}>
              <span
                class={`task-badge ${
                  v.result === "passed" ? "done" : v.result === "failed" ? "failed" : "pending"
                }`}
              >
                {VERIF_ZH[v.result] || v.result || "未运行"}
              </span>
              <span>{v.name || "（未命名检查）"}</span>
              {v.candidate ? <span class="muted">候选 {shortSha(v.candidate)}</span> : null}
              <span class="muted">{fmtTime(v.time)}</span>
            </div>
          ))}
        </div>
      ) : null}
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
          // 语义区分（编排控制台第一刀）：排队/熔断时没有在飞 Run，取消立即
          // 生效；running/waiting 时是协作式打断——当前步收尾后才停，不能
          // 让用户以为是立刻 kill（诚实降级做成可见）
          <button
            class="tp-btn tp-btn-danger"
            disabled={busy}
            title={
              ["running", "waiting-approval", "waiting-steer"].includes(task.status)
                ? "协作式打断：当前步收尾后才停；若正在写文件，可能留下半写状态"
                : undefined
            }
            onClick={() => act(() => cancelTask(task.task_id))}
          >
            {["running", "waiting-approval", "waiting-steer"].includes(task.status)
              ? "打断"
              : "取消（立即生效）"}
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
          {task.worktree ? (
            <div class="tp-billing" data-testid="task-sandbox">
              <div class="detail-muted">
                沙箱（git worktree）：文件改动未进主工作区，收口才生效
              </div>
              <div class="tp-actions">
                <button class="tp-btn" disabled={busy} onClick={loadWt}>
                  {wt === null ? "查看改动" : "收起改动"}
                </button>
                {stale ? (
                  // 预览失效（S2-4）：合入冻结在按钮态，重新核验才解冻
                  <>
                    <button class="tp-btn" disabled title="候选已变化，需重新核验后再合入">
                      产物已变化，请重新核验
                    </button>
                    <button class="tp-btn" disabled={busy} onClick={recheck}>
                      重新核验
                    </button>
                  </>
                ) : (
                  <button class="tp-btn" disabled={busy} onClick={() => settleWt("merge")}>
                    {settleR?.status === "conflict" ? "重试合入" : "合并入库"}
                  </button>
                )}
                <button
                  class="tp-btn tp-btn-danger"
                  disabled={busy}
                  title="整棵丢弃沙箱改动，不可恢复"
                  onClick={() => settleWt("discard")}
                >
                  丢弃改动
                </button>
              </div>
              {wt !== null ? <DiffView wt={wt} /> : null}
            </div>
          ) : null}
          {settleR || cleanupFailed ? (
            <div class="detail-muted">
              沙箱收口：{settleR?.result || "清理失败"}
              {settleR?.detail ? `（${settleR.detail}）` : ""}
              {settleR?.status === "conflict" ? " · 冲突可重试，候选已保留" : ""}
              {cleanupFailed ? (
                <button
                  class="tp-btn tp-btn-mini"
                  disabled={busy}
                  title="产物已安全合入，只重试清理 worktree 与分支"
                  onClick={() => settleWt("merge")}
                >
                  重试清理
                </button>
              ) : null}
            </div>
          ) : null}
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
