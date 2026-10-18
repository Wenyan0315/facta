// 方案评审页主组件（ADR 111）——FW 第六页。
//
// 结构：分节材料 + 被评审仓库根 → 一次同步提交 → 渲染短评审。
// 「只读、不改代码、不跑命令」由后端的评审人设与只读工具集结构性保证，
// 本页不做二次判断；等待预算用尽时如实提示「后台仍在跑」，不假装已完成。
import { useState } from "preact/hooks";
import { submitReview } from "./api.js";

// 分节材料模板：与 REVIEW_SYSTEM_PROMPT 的输入约定一致（目标/验收/方案/历史决定）
const TEMPLATE =
  "【任务目标】\n\n\n【验收要求】\n\n\n【待评审方案】\n\n\n【相关历史决定（可选）】\n";

export default function App() {
  const [materials, setMaterials] = useState("");
  const [repoRoot, setRepoRoot] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [review, setReview] = useState(null);
  const [elapsed, setElapsed] = useState(null);
  const [timedOut, setTimedOut] = useState(false);

  const run = async () => {
    if (!materials.trim()) return;
    setBusy(true);
    setErr(null);
    setReview(null);
    setTimedOut(false);
    const t0 = Date.now();
    try {
      const res = await submitReview({ text: materials, repoRoot: repoRoot.trim() });
      setReview(res.text ?? "");
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
        按分节填【任务目标】【验收要求】【待评审方案】【相关历史决定】。评审只读
        ——不会改代码、执行命令或固化记忆；需实跑才能判断的会标「待验证」。
      </p>
      <p>
        <input
          value={repoRoot}
          onInput={(e) => setRepoRoot(e.target.value)}
          placeholder="被评审仓库根路径（留空 = 评审 Facta 本仓）"
          style="width:100%;"
        />
      </p>
      <p>
        <textarea
          value={materials}
          onInput={(e) => setMaterials(e.target.value)}
          placeholder={TEMPLATE}
          rows={14}
          style="width:100%;font-family:inherit;"
        />
      </p>
      <div class="memory-actions visible">
        <button onClick={run} disabled={busy || !materials.trim()}>
          {busy ? "评审中…" : "开始评审"}
        </button>
        <button onClick={() => setMaterials(TEMPLATE)} disabled={busy}>
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
          <span>等待预算用尽，评审仍在后台运行；稍后可在会话清单里查看结果。</span>
        </div>
      )}

      {review !== null && (
        <div style="margin-top:16px;">
          <hr />
          {elapsed && <p class="muted" style="font-size:12px;">评审用时 {elapsed}s</p>}
          {window.marked ? (
            <div dangerouslySetInnerHTML={{ __html: window.marked.parse(review) }} />
          ) : (
            <pre style="white-space:pre-wrap;">{review}</pre>
          )}
        </div>
      )}
    </div>
  );
}
