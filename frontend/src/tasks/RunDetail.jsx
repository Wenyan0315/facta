// RunDetail：任务视图的 Run 详情展开（S5c 计划面板）。
// 任务视图第一次消费 SSE：/api/runs/{id}/events——服务端先重放历史段
// （Last-Event-ID 机制，断线重连/历史 Run 的计划事件全量可见），再实时流。
// plan.* 事件的 fold 与后端 PlanState.view() 同构——同一份事件流两个消费者
// （后端 fold 给模型看，这里 fold 给人看）。
import { useEffect, useState } from "preact/hooks";

// 步骤记号：与 CLI 格式化同款（一个心智模型两处呈现）
const MARKS = {
  pending: "○",
  in_progress: "◐",
  done: "●",
  skipped: "×",
  failed: "✗",
};

const STATUS_CLASS = {
  pending: "",
  in_progress: "in-progress",
  done: "done",
  skipped: "skipped",
  failed: "failed",
};

// fold：plan.created 初始化 / plan.revised 换表 / plan.step_updated 改单步。
// 修订换表后旧 id 不在新表 → 跳过（与后端 view() 的孤儿事件语义一致）
export function foldPlan(events) {
  let steps = [];
  let finished = null;
  for (const ev of events) {
    if (ev.type === "plan.created") {
      steps = ev.data.steps.map((s) => ({ ...s, status: "pending", note: "" }));
    } else if (ev.type === "plan.revised") {
      steps = ev.data.steps.map((s) => ({
        ...s,
        status: s.status || "pending",
        note: s.note || "",
      }));
    } else if (ev.type === "plan.step_updated") {
      const step = steps.find((s) => s.id === ev.data.id);
      if (step) {
        step.status = ev.data.status;
        step.note = ev.data.note || "";
      }
    } else if (ev.type === "plan.finished") {
      finished = ev.data.summary || "";
    }
  }
  return { steps, finished };
}

export default function RunDetail({ runId }) {
  // 两态合一：{connecting} 初始 / {plan, hasEvents}——事件流天然自愈
  // （SSE 重连服务端重放），失败态由 EventSource 自动恢复，不单独建模
  const [state, setState] = useState({ connecting: true });

  useEffect(() => {
    // alive 竞态守卫（同 App.jsx 轮询模式）：卸载后的迟到事件不再 setState
    let alive = true;
    const events = [];
    const apply = () => {
      if (alive) setState({ plan: foldPlan(events), hasEvents: events.length > 0 });
    };

    const es = new EventSource(`/api/runs/${runId}/events`);
    // 服务端 SSE 编码：事件名=type，data=JSON。全类型监听（plan.* 折叠进
    // 计划视图；tool_*/text.* 等先收集不展示——执行时间线挂触发信号）
    const onEvent = (e) => {
      try {
        // 服务端 SSE 帧有外层信封 {schema_version, run_id, seq, type, data}——
        // 真实载荷在内层 .data（encode_sse 的编码约定）。解包错误=TypeError
        // 被 catch 吞掉 → 面板永久卡 connecting（浏览器验收抓到的 P0，此为修复）
        const envelope = JSON.parse(e.data);
        events.push({ type: e.type, data: envelope.data || {} });
        apply();
      } catch (err) {
        // 坏事件不拖垮面板，但不再静默——控制台留痕（这次验收的教训）
        console.warn("[RunDetail] 坏事件忽略：", e.type, err);
      }
    };
    // SSE 端点每类型一个 addEventListener（EventSource 只默认收 message
    // 类型——服务端 encode_sse 用事件名编码，需逐类型订阅）。
    // 契约修正（评审修复轮）：服务端 _EVENT_MAP 把内核下划线事件映射为
    // 点分（tool_started→tool.started）——此前订阅下划线版导致 tool 事件
    // 全收不到（v1 未渲染工具时间线所以潜伏未暴露）
    const TYPES = [
      "plan.created", "plan.revised", "plan.step_updated", "plan.finished",
      "tool.started", "tool.result", "run.completed", "run.failed", "run.cancelled",
    ];
    TYPES.forEach((t) => es.addEventListener(t, onEvent));
    es.onerror = () => {
      // EventSource 自带重连；这里只负责初次连接后的兜底文案
      if (alive && events.length === 0) setState({ connecting: false, noStream: true });
    };

    return () => {
      alive = false;
      es.close();
    };
  }, [runId]);

  if (state.connecting) return <p class="muted detail-muted">连接事件流…</p>;
  if (!state.plan || state.plan.steps.length === 0)
    return <p class="muted detail-muted">本次运行没有计划（简单任务直答，或未走 make_plan）</p>;

  const { steps, finished } = state.plan;
  return (
    <div class="plan-panel">
      {steps.map((s) => (
        <div class={`plan-step ${STATUS_CLASS[s.status] || ""}`} key={s.id}>
          <span class="plan-mark">{MARKS[s.status] || "○"}</span>
          <span class="plan-title">
            {s.id}. {s.title}
            {s.note ? <span class="plan-note"> —— {s.note}</span> : null}
          </span>
        </div>
      ))}
      {finished ? <div class="plan-finished">收官：{finished}</div> : null}
    </div>
  );
}
