// 任务视图主组件（FW 站 /tasks 入口）——ADR 118 §2 把它从「Run 列表」升级为
// **Task 一等视图**：委派表单在页首，任务卡是一等公民，Run 降为卡内下钻。
//
// 三条不变的旧纪律（沿用本页 v2 的写法）：
// ① 状态即真相：tasks/runs 是唯一数据源，UI 是它的投影，不手动改 DOM；
// ② 声明式列表：tasks.map(...) 描述「列表长什么样」；
// ③ JSX 文本节点自动转义（goal/报错串都来自外部）。
//
// M5 承诺件 b：任务级 SSE 上线，118 §2「不新建任务级 SSE」的负决策翻案——
// 任务卡状态/确认/账单改走推送（task.updated/run.updated 增量 + snapshot
// 全量自愈 + Last-Event-ID 断线续传），20s 轮询降为 60s 兜底心跳（侧料
// 技能/项目/版本顺带刷新；IDE 信箱走 ide.updated 推送）。
import { useEffect, useState } from "preact/hooks";
import { fetchIdeContext, fetchProjects, fetchRuns, fetchSkills, fetchStatus, fetchTasks } from "./api.js";
import NewTaskForm from "./NewTaskForm.jsx";
import ProjectsPanel from "./ProjectsPanel.jsx";
import TaskCard from "./TaskCard.jsx";

const FALLBACK_MS = 60000;   // 兜底心跳（SSE 是主通道；断线自愈靠 onopen 重拉 + 快照）

export default function App() {
  const [tasks, setTasks] = useState(null);
  const [runs, setRuns] = useState([]);
  const [skills, setSkills] = useState([]);
  const [projects, setProjects] = useState([]);
  const [ideContext, setIdeContext] = useState(null);
  const [version, setVersion] = useState("");
  const [error, setError] = useState(null);

  // 列表：任务 + Run 摘要（Run 供卡内下钻；一次刷新两跳并发）
  const refreshList = async () => {
    try {
      const [t, r] = await Promise.all([fetchTasks(), fetchRuns()]);
      setTasks(t);
      setRuns(r);
      setError(null);
    } catch (e) {
      // 失败态不覆盖旧数据（下一次心跳/SSE 自愈），只如实报一行
      setError(e.message);
    }
  };

  // 侧料：技能表 / 项目表 / IDE 信箱 / 版本。信箱另有 ide.updated 推送
  // （壳里「附上当前上下文」可能发生在面板已经打开之后，118 §4.3 的动线）。
  const refreshSide = async () => {
    const [sk, pj, ide, st] = await Promise.allSettled([
      fetchSkills(), fetchProjects(), fetchIdeContext(), fetchStatus(),
    ]);
    if (sk.status === "fulfilled") setSkills(sk.value);
    if (pj.status === "fulfilled") setProjects(pj.value);
    if (ide.status === "fulfilled") setIdeContext(ide.value);   // 空槽 = null（正常）
    if (st.status === "fulfilled") setVersion(st.value.version || "");
  };

  useEffect(() => {
    let alive = true;   // 竞态守卫：卸载后的迟到响应不再 setState

    // 增量事件载荷是全量状态（task.updated=整个任务视图），按 id 幂等替换
    const upsert = (list, item, key) => {
      const i = list.findIndex((x) => x[key] === item[key]);
      return i >= 0 ? list.map((x) => (x[key] === item[key] ? item : x)) : [item, ...list];
    };
    const onEnvelope = (apply) => (e) => {
      if (!alive) return;
      try {
        apply(JSON.parse(e.data).data || {});
      } catch (err) {
        console.warn("[tasks] 坏事件忽略：", e.type, err);
      }
    };

    refreshList();
    refreshSide();

    const es = new EventSource("/api/tasks/events");
    es.addEventListener("snapshot", onEnvelope((d) => {
      setTasks(d.tasks || []);
      setRuns(d.runs || []);
      setError(null);
    }));
    es.addEventListener("task.updated", onEnvelope((d) =>
      setTasks((prev) => upsert(prev ?? [], d, "task_id"))));
    es.addEventListener("run.updated", onEnvelope((d) =>
      setRuns((prev) => upsert(prev, d, "run_id"))));
    es.addEventListener("ide.updated", () => refreshSide());
    es.onopen = () => { if (alive) refreshList(); };   // 断线重连自愈：重拉全量（快照随后也会到）

    const timer = setInterval(() => {
      if (alive) {
        refreshList();
        refreshSide();
      }
    }, FALLBACK_MS);
    return () => {
      alive = false;
      es.close();
      clearInterval(timer);
    };
  }, []);

  if (tasks === null) return <p class="muted">加载中…</p>;

  return (
    <div class="tp-page">
      <NewTaskForm
        skills={skills}
        projects={projects}
        ideContext={ideContext}
        onCreated={() => { refreshList(); refreshSide(); }}
        onIdeCleared={() => refreshSide()}
      />

      {error ? <div class="tp-err">刷新失败：{error}</div> : null}

      {tasks.length === 0 ? (
        <p class="muted">还没有委派任务。用上面的表单委派第一个。</p>
      ) : (
        tasks.map((task) => (
          <TaskCard key={task.task_id} task={task} runs={runs} onChanged={refreshList} />
        ))
      )}

      <ProjectsPanel projects={projects} onChanged={refreshSide} />

      {/* ADR 118 §6：版本号在页脚可观测（端点 /api/status 同源，不再靠猜） */}
      <div class="tp-footer muted" data-testid="footer-version">
        Facta {version || "?"} · 委派/批准/纠偏/取消都在本页；聊天与确认卡另见 <a href="/">对话页</a>
      </div>
    </div>
  );
}
