// ConfirmCard（ADR 118 §3）：任务面板内的确认面——FW 站第一处确认 UI。
//
// 只在「任务卡展开 && 状态 waiting-approval」时挂载 ⇒ 订阅数 = 展开的待确认
// 任务数，不常驻（118 §3 自我挑刺 2）。数据源是既有的 per-Run SSE：服务端断线
// 重放语义保证「request 无对应 resolved 则重新弹窗」（run_store.py 注），所以
// 这里的 fold 只需数事件——刷新页面/晚展开都不会漏掉那条待裁决。
// 决策端点唯一：POST /api/runs/{id}/confirm（与聊天页同一个，语义不分裂）。
import { useEffect, useState } from "preact/hooks";
import { confirmRun } from "./api.js";

// kind → 授权文案（后端 skills.WRITE_AUTH_GUARD = "write-auth"，ADR 117 §3 +
// 118 §3：写档任务首写停，批准后本任务后续写操作免停）。空 kind = 普通确认。
const KIND_NOTE = {
  "write-auth": "本任务写授权：批准后本任务后续写操作免停。",
};

// fold：最后一条 confirm.request 之后若没有 confirm.resolved，则它仍悬着。
export function foldConfirm(events) {
  let pending = null;
  for (const ev of events) {
    if (ev.type === "confirm.request") pending = ev.data;
    else if (ev.type === "confirm.resolved") pending = null;
  }
  return pending;
}

export default function ConfirmCard({ runId, onDecided }) {
  const [pending, setPending] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    const events = [];
    const es = new EventSource(`/api/runs/${runId}/events`);
    const onEvent = (e) => {
      try {
        // 外层信封 {schema_version, run_id, seq, type, data}，真实载荷在 .data
        // （RunDetail 踩过的坑：解包错 → 面板永久卡住）
        const envelope = JSON.parse(e.data);
        events.push({ type: e.type, data: envelope.data || {} });
        if (alive) setPending(foldConfirm(events));
      } catch (error) {
        console.warn("[ConfirmCard] 坏事件忽略：", e.type, error);
      }
    };
    ["confirm.request", "confirm.resolved"].forEach((t) => es.addEventListener(t, onEvent));
    return () => {
      alive = false;
      es.close();
    };
  }, [runId]);

  const decide = async (approve) => {
    setBusy(true);
    setErr(null);
    try {
      await confirmRun(runId, approve);
      setPending(null);          // 乐观收卡；SSE 的 resolved 事件随后印证
      onDecided?.();
    } catch (e) {
      setErr(e.message);         // 409「当前没有待确认的操作」等如实显示
    } finally {
      setBusy(false);
    }
  };

  if (!pending) return <p class="muted detail-muted">事件流里没有待确认的操作。</p>;
  return (
    <div class="confirm-box" data-testid="confirm-card">
      <div class="confirm-title">需要你的确认</div>
      <div class="confirm-tool">工具：{pending.tool}</div>
      {pending.kind && KIND_NOTE[pending.kind] ? (
        <div class="tp-kind-note" data-kind={pending.kind}>{KIND_NOTE[pending.kind]}</div>
      ) : null}
      <pre class="confirm-args">{JSON.stringify(pending.arguments, null, 2)}</pre>
      {err ? <div class="tp-err">{err}</div> : null}
      {/* 不用聊天页那套 #confirm-approve/#confirm-reject 的 id：本页可能同时展开
          多张待确认卡，重复 id 会让选择器与样式失准。data-testid 供 Playwright。 */}
      <div class="confirm-actions">
        <button class="tp-btn" data-testid="confirm-reject" disabled={busy} onClick={() => decide(false)}>
          拒绝
        </button>
        <button
          class="tp-btn tp-btn-danger"
          data-testid="confirm-approve"
          disabled={busy}
          onClick={() => decide(true)}
        >
          {busy ? "提交中…" : "批准"}
        </button>
      </div>
    </div>
  );
}
