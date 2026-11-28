// ApprovalPanel（P1 审批恢复与任务推送）：任务详情内的审批区块——审批项列表 + 裁决。
//
// 数据源 GET /api/approvals?task_id=…（审批项快照，重启存活）；裁决走
// POST /api/approvals/{id}/decision（幂等：重复裁决返回原裁决；异参 409）。
// 「编辑参数后批准」带与原项不同的 arguments/target 提交 → 后端把原项置
// superseded、生成新 pending 版本（supersedes/superseded_by 版本链可溯），
// 前端收到 message「生成新的待审版本」后刷新列表：新版本待审继续呈现，
// 旧项落进版本链痕迹（标「已被新版本取代」）。
// 旧布尔弹窗（ConfirmCard）不删，本面板走审批项端点（新面板优先）。
// 旧任务/无审批项 → 显示「无待审批」，不崩。
import { useEffect, useState } from "preact/hooks";
import { decideApproval, fetchApprovals } from "./api.js";

const STATUS_ZH = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已拒绝",
  superseded: "已被新版本取代",
  unknown_result: "结果未知，需核验",
};
// 徽标配色沿用 task-badge 既有变体（tp-alert=红高亮待审，waiting-approval=黄警示）
const STATUS_CLS = {
  pending: "tp-alert",
  approved: "done",
  rejected: "failed",
  superseded: "pending",
  unknown_result: "waiting-approval",
};

// 时间戳格式契约未钉（epoch 秒 / ISO 串都可能）：数字按本地时刻，其余原样
export function fmtTime(t) {
  if (t == null || t === "") return "—";
  if (typeof t !== "number") return String(t);
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const shortId = (id) => (id ? String(id).slice(0, 8) : "");

// arguments 键值可读展示：字符串原样，其余 JSON 串化（对象/数组防 [object Object]）
function ArgsView({ args }) {
  const entries = Object.entries(args || {});
  if (!entries.length) return <div class="muted detail-muted">（无参数）</div>;
  return (
    <div class="tp-meta" data-testid="approval-args">
      {entries.map(([k, v]) => (
        <span class="tp-chip" style="word-break:break-all" key={k}>
          {k}：{typeof v === "string" ? v : JSON.stringify(v)}
        </span>
      ))}
    </div>
  );
}

// 编辑表单：arguments 走 JSON 文本、target 走单行；提交时解析（坏 JSON 就地报错，
// 不发请求）。语义是「编辑参数后批准」——只出批准，不出拒绝（拒绝不带新参数）
function EditForm({ item, onSubmit, onCancel, busy }) {
  const [argsText, setArgsText] = useState(JSON.stringify(item.arguments ?? {}, null, 2));
  const [target, setTarget] = useState(item.target ?? "");
  const [err, setErr] = useState(null);
  const submit = () => {
    let args;
    try {
      args = JSON.parse(argsText || "{}");
    } catch (e) {
      setErr(`参数不是合法 JSON：${e.message}`);
      return;
    }
    setErr(null);
    onSubmit(args, target);
  };
  return (
    <div class="tp-billing" data-testid="approval-edit">
      <div class="detail-muted">arguments（JSON）</div>
      <textarea
        class="tp-goal"
        rows={6}
        value={argsText}
        onInput={(e) => setArgsText(e.target.value)}
      />
      <div class="detail-muted">target（操作作用目标）</div>
      <input
        class="tp-steer"
        style="width:100%;box-sizing:border-box"
        value={target}
        onInput={(e) => setTarget(e.target.value)}
      />
      {err ? <div class="tp-err">{err}</div> : null}
      <div class="tp-actions">
        <button class="tp-btn tp-btn-danger" disabled={busy} onClick={submit}>
          以新版本批准
        </button>
        <button class="tp-btn" disabled={busy} onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  );
}

// 一个审批项：pending 高亮 + 批准/拒绝/编辑后批准；已决项折叠展示裁决与执行结果
// （导出供视图字段断言，同 foldConfirm/foldPlan 惯例）
export function ApprovalRow({ item, busy, editing, onEdit, onCancelEdit, onDecide }) {
  const decided = item.status !== "pending";
  return (
    <div class="confirm-box" data-testid="approval-item" data-status={item.status}>
      <div class="confirm-title">
        <span class={`task-badge ${STATUS_CLS[item.status] || "pending"}`}>
          {STATUS_ZH[item.status] || item.status}
        </span>
        <span class="confirm-tool">工具：{item.tool}</span>
      </div>
      {item.status === "unknown_result" ? (
        <div class="tp-warn" data-testid="approval-unknown">
          结果未知，需核验——不自动执行，请人工核对后再处置
        </div>
      ) : null}
      {item.target ? (
        <div class="detail-muted" style="word-break:break-all">目标：{item.target}</div>
      ) : null}
      <ArgsView args={item.arguments} />
      <div class="muted tp-time">发起 {fmtTime(item.created_at)}</div>
      {!decided ? (
        editing === item.id ? (
          <EditForm
            item={item}
            busy={busy}
            onSubmit={(args, target) => onDecide(item, true, args, target)}
            onCancel={onCancelEdit}
          />
        ) : (
          <div class="confirm-actions">
            <button
              class="tp-btn"
              data-testid="approval-edit-btn"
              disabled={busy}
              onClick={() => onEdit(item.id)}
            >
              编辑参数后批准
            </button>
            <button
              class="tp-btn"
              data-testid="approval-reject"
              disabled={busy}
              onClick={() => onDecide(item, false)}
            >
              拒绝
            </button>
            <button
              class="tp-btn tp-btn-danger"
              data-testid="approval-approve"
              disabled={busy}
              onClick={() => onDecide(item, true)}
            >
              {busy ? "提交中…" : "批准"}
            </button>
          </div>
        )
      ) : (
        <details>
          <summary class="detail-muted">裁决与执行结果</summary>
          <div class="muted tp-time">
            裁决 {fmtTime(item.decided_at)}
            {item.approved != null ? (item.approved ? " · 批准" : " · 拒绝") : ""}
            {item.executed ? " · 已执行" : " · 未执行"}
            {item.supersedes ? ` · 取代版本 ${shortId(item.supersedes)}` : ""}
          </div>
          {item.result ? <pre class="confirm-args">{item.result}</pre> : null}
        </details>
      )}
    </div>
  );
}

export default function ApprovalPanel({ taskId, updatedAt, onDecided }) {
  const [items, setItems] = useState(null);   // null = 未拉取
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);       // 裁决端点 message（新版本/重复请求等注明）
  const [editing, setEditing] = useState(null);   // 正在编辑的审批项 id

  const load = async () => {
    try {
      const r = await fetchApprovals(taskId);
      setItems(r.approvals || []);
      setErr(null);
    } catch (e) {
      setItems((prev) => prev ?? []);   // 拉取失败不崩、不留 loading 态，错误如实报一行
      setErr(e.message);
    }
  };

  // task.updated_at 变（SSE 推送 / 轮询刷新）→ 重拉审批项：恢复呈现、外部裁决同步
  useEffect(() => {
    load();
  }, [taskId, updatedAt]);

  const decide = async (item, approved, args, target) => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await decideApproval(item.id, approved, args, target);
      setMsg(r.message || "");
      setEditing(null);
      await load();   // 原裁决/新版本一律刷新列表（新版本待审在列表里继续呈现）
      onDecided?.();
    } catch (e) {
      setErr(e.message);   // 409 异参裁决等如实显示，不改写原项
    } finally {
      setBusy(false);
    }
  };

  const list = items || [];
  const active = list.filter((a) => a.status !== "superseded");
  const history = list.filter((a) => a.status === "superseded");

  return (
    <div class="tp-billing" data-testid="approval-panel">
      <div class="detail-muted">审批（写入操作待裁决 · 重启可恢复）</div>
      {err ? <div class="tp-err">{err}</div> : null}
      {msg ? <div class="tp-hint" data-testid="approval-msg">{msg}</div> : null}
      {items === null ? (
        <p class="muted detail-muted">加载中…</p>
      ) : active.length === 0 ? (
        <p class="muted detail-muted" data-testid="approval-empty">无待审批</p>
      ) : (
        active.map((item) => (
          <ApprovalRow
            key={item.id}
            item={item}
            busy={busy}
            editing={editing}
            onEdit={setEditing}
            onCancelEdit={() => setEditing(null)}
            onDecide={decide}
          />
        ))
      )}
      {history.length ? (
        <details data-testid="approval-history">
          <summary class="detail-muted">
            版本链痕迹（{history.length} 个已被新版本取代）
          </summary>
          {history.map((item) => (
            <div class="tp-billing-row" key={item.id}>
              <span class="task-badge pending">{STATUS_ZH.superseded}</span>
              <span>{item.tool}</span>
              <span class="muted">{shortId(item.id)}</span>
              {item.superseded_by ? (
                <span class="muted">→ 新版本 {shortId(item.superseded_by)}</span>
              ) : null}
              <span class="muted">{fmtTime(item.created_at)}</span>
            </div>
          ))}
        </details>
      ) : null}
    </div>
  );
}
