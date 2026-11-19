// ProjectsPanel（ADR 118 §5）：项目设置**并入 `/tasks` 页的折叠区块**，不新建
// 第八个 FW 入口。唯一操作 = 记忆归属模式切换（PUT /api/projects/{name}），
// 兑现 ADR 114 明裁「切换入口进项目设置」——此前双模式只能 curl 切。
//
// 114 红线：往**工作仓**写目录须显式同意 ⇒ 切到就地式时先出确认行（含
// 「连旧三桶一起搬过去」勾选），点确认才发 PUT。切回集中式不写工作仓，直接发。
import { useState } from "preact/hooks";
import { putProject } from "./api.js";

export const MODE_ZH = {
  centralized: "集中式（data/projects/<名>/learned）",
  "in-repo": "就地式（<工作仓>/.facta/learned）",
};

export default function ProjectsPanel({ projects, onChanged }) {
  // asking = 正在等显式确认的那条项目名；一次只问一个（写工作仓是重动作）
  const [asking, setAsking] = useState(null);
  const [migrate, setMigrate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const doSwitch = async (name, mode, opts = {}) => {
    setBusy(true);
    setErr(null);
    try {
      await putProject(name, { memory_mode: mode, ...opts });
      setAsking(null);
      onChanged?.();
    } catch (e) {
      setErr(e.message);   // 400「就地模式须显式确认」等如实显示，不猜
    } finally {
      setBusy(false);
    }
  };

  return (
    <details class="tp-projects" data-testid="projects-panel">
      <summary>项目设置（记忆归属：{(projects || []).length} 个已登记项目）</summary>
      {err ? <div class="tp-err">{err}</div> : null}
      {(projects || []).length === 0 ? (
        <p class="muted">还没有登记项目。委派任务时填 repo_root 不会自动登记；登记走 POST /api/projects。</p>
      ) : null}
      {(projects || []).map((p) => (
        <div class="tp-project" key={p.name}>
          <div class="tp-project-head">
            <strong>{p.name}</strong>
            <span class="muted">{p.repo_root}</span>
            <span class="tp-chip">{MODE_ZH[p.memory_mode] || p.memory_mode}</span>
          </div>
          <div class="muted tp-project-path">记忆：{p.learned_dir}</div>

          {p.memory_mode === "centralized" ? (
            asking === p.name ? (
              <div class="tp-confirm-row">
                <div class="tp-warn">
                  切到就地式会在<strong>工作仓内</strong>创建 .facta/learned/ 并写入文件（git 可见）。
                </div>
                <label class="tp-ide">
                  <input
                    type="checkbox"
                    checked={migrate}
                    onChange={(e) => setMigrate(e.target.checked)}
                  />
                  连集中式旧三桶一起搬过去（复制，不删原件）
                </label>
                <div class="tp-actions">
                  <button class="tp-btn" disabled={busy} onClick={() => setAsking(null)}>
                    算了
                  </button>
                  <button
                    class="tp-btn tp-btn-danger"
                    disabled={busy}
                    onClick={() =>
                      doSwitch(p.name, "in-repo", { migrate, confirm_repo_write: true })
                    }
                  >
                    {busy ? "切换中…" : "确认切换"}
                  </button>
                </div>
              </div>
            ) : (
              <button class="tp-btn" disabled={busy} onClick={() => { setAsking(p.name); setMigrate(true); }}>
                切到就地式
              </button>
            )
          ) : (
            <button class="tp-btn" disabled={busy} onClick={() => doSwitch(p.name, "centralized")}>
              切回集中式
            </button>
          )}
        </div>
      ))}
    </details>
  );
}
