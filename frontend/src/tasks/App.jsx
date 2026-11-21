// 任务视图主组件（FW 站 /tasks 入口）——ADR 118 §2 把它从「Run 列表」升级为
// **Task 一等视图**：委派表单在页首，任务卡是一等公民，Run 降为卡内下钻。
//
// 三条不变的旧纪律（沿用本页 v2 的写法）：
// ① 状态即真相：tasks/runs 是唯一数据源，UI 是它的投影，不手动改 DOM；
// ② 声明式列表：tasks.map(...) 描述「列表长什么样」；
// ③ JSX 文本节点自动转义（goal/报错串都来自外部）。
//
// 118 §2 负决策：**不新建任务级 SSE**——面板要的状态翻转（queued→running→…）
// 都是低频粗粒度，20s 轮询够用；实时性留给「展开时才订阅」的既有 per-Run SSE
// （RunDetail 的 plan.*、ConfirmCard 的 confirm.*），订阅数 = 展开数。
import { useEffect, useState } from "preact/hooks";
import { fetchIdeContext, fetchProjects, fetchRuns, fetchSkills, fetchStatus, fetchTasks } from "./api.js";
import NewTaskForm from "./NewTaskForm.jsx";
import ProjectsPanel from "./ProjectsPanel.jsx";
import TaskCard from "./TaskCard.jsx";

const REFRESH_MS = 20000;   // 118 §2：沿用本页既有轮询节奏，不为 Task 新造通道

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
      // 失败态不覆盖旧数据（瞬时抖动下一次轮询自愈），只如实报一行
      setError(e.message);
    }
  };

  // 侧料：技能表 / 项目表 / IDE 信箱 / 版本。信箱要跟着轮询走——壳里「附上当前
  // 上下文」可能发生在面板已经打开之后（118 §4.3 预填的动线）。
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
    const tick = () => {
      if (alive) {
        refreshList();
        refreshSide();
      }
    };
    tick();
    const timer = setInterval(tick, REFRESH_MS);
    return () => {
      alive = false;
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
