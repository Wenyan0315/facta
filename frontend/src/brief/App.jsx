// 开工 Brief 页主组件（ADR 113）——FW 第七页。
//
// 结构：任务意图 + 目标仓库根 → 一次同步提交 → 渲染六节 Brief 草稿。
// 「只读、不改代码、不跑命令、不固化记忆」由后端技能人设与只读工具集
// 结构性保证（brief.md settle=skip），本页不做二次判断；等待预算用尽时
// 如实提示「后台仍在跑」，不假装已完成。
import { useState } from "preact/hooks";
import { submitBrief } from "./api.js";

// 任务意图模板（113 §1 输入形态：一句到一段）
const TEMPLATE = "【任务意图】\n\n\n【补充上下文（可选）】\n";

export default function App() {
  const [intent, setIntent] = useState("");
  const [repoRoot, setRepoRoot] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [brief, setBrief] = useState(null);
  const [elapsed, setElapsed] = useState(null);
  const [timedOut, setTimedOut] = useState(false);

  const run = async () => {
    if (!intent.trim()) return;
    setBusy(true);
    setErr(null);
    setBrief(null);
    setTimedOut(false);
    const t0 = Date.now();
    try {
      const res = await submitBrief({ text: intent, repoRoot: repoRoot.trim() });
      setBrief(res.text ?? "");
      setTimedOut(Boolean(res.timed_out));
      setElapsed(((Date.now() - t0) / 1000).toFixed(1));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <p class="muted" style="font-size:12px;">
        输入任务意图，生成六节开工 Brief 草稿（目标复述/历史决定/仓库现状/约束/
        验收建议/待补充）。草稿不会固化进项目记忆——采纳与否由你定。
      </p>
      <p>
        <input
          value={repoRoot}
          onInput={(e) => setRepoRoot(e.target.value)}
          placeholder="目标仓库根路径（留空 = 本仓）"
          style="width:100%;"
        />
      </p>
      <p>
        <textarea
          value={intent}
          onInput={(e) => setIntent(e.target.value)}
          placeholder={TEMPLATE}
          rows={10}
          style="width:100%;font-family:inherit;"
        />
      </p>
      <div class="memory-actions visible">
        <button onClick={run} disabled={busy || !intent.trim()}>
          {busy ? "生成中…" : "生成 Brief"}
        </button>
        <button onClick={() => setIntent(TEMPLATE)} disabled={busy}>
          填模板
        </button>
      </div>

      {err && (
        <div class="notes-banner notes-banner-err">
          <span>{err.message}</span>
        </div>
      )}
      {timedOut && (
        <div class="notes-banner">
          <span>等待预算用尽，Brief 仍在后台生成；稍后可在会话清单里查看结果。</span>
        </div>
      )}

      {brief !== null && (
        <div style="margin-top:16px;">
          <hr />
          {elapsed && <p class="muted" style="font-size:12px;">生成用时 {elapsed}s</p>}
          {window.marked ? (
            <div dangerouslySetInnerHTML={{ __html: window.marked.parse(brief) }} />
          ) : (
            <pre style="white-space:pre-wrap;">{brief}</pre>
          )}
        </div>
      )}
    </div>
  );
}
