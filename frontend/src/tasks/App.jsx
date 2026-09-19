// 任务视图 v2 主组件（FW 站试点）——对照 v1（已删，git 史可查）的三个根本区别：
// ① 状态即真相：runs 是唯一数据源，UI 是它的投影；
//    v1 是 fetch 完手动 innerHTML + querySelector 逐个填（改数据+改 DOM 两步）
// ② 声明式列表：runs.map(...) 描述「列表长什么样」，不描述「怎么改 DOM」
// ③ JSX 文本节点自动转义——v1 靠纪律手写 textContent 防注入，v2 结构性免疫
import { useEffect, useState } from "preact/hooks";
import { fetchRuns } from "./api.js";

const STATUS_ZH = {
  pending: "排队中",
  running: "运行中",
  waiting_approval: "等待确认",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const REFRESH_MS = 5000; // 与 v1 同节奏：5 秒轮询

function TaskItem({ run }) {
  return (
    <div class="task-item">
      <div class="task-head">
        <span class={`task-badge ${run.status}`}>
          {STATUS_ZH[run.status] || run.status}
        </span>
        <span class="task-title">{run.title || "(无标题)"}</span>
      </div>
      <div class="task-preview">{run.preview || "（无交付摘要）"}</div>
    </div>
  );
}

export default function App() {
  // 三态合一：{loading} 初始 / {error} 失败 / {runs} 到货
  // 轮询不停：失败态会被下一次成功轮询自然覆盖（瞬时网络抖动自愈）
  const [state, setState] = useState({ loading: true });

  useEffect(() => {
    let alive = true; // 竞态守卫：卸载后的迟到响应不再 setState
    const refresh = async () => {
      try {
        const runs = await fetchRuns();
        if (alive) setState({ runs });
      } catch {
        if (alive) setState({ error: true });
      }
    };
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => {
      alive = false; // 卸载即清理：定时器和迟到响应一起带走
      clearInterval(timer);
    };
  }, []);

  if (state.loading) return <p class="muted">加载中…</p>;
  if (state.error) return <p class="muted">加载失败</p>;
  if (!state.runs.length) return <p class="muted">暂无任务运行</p>;
  return (
    <div>
      {state.runs.map((run) => (
        <TaskItem key={run.run_id} run={run} />
      ))}
    </div>
  );
}
