// 记忆面板主组件（025「护城河可视化」）——FW 新栈第二页。
//
// 与任务视图（只读轮询）相比，本页教学靶心是「状态写路径」：
// ① 状态分层：entries 归 App（全列表一份真相），编辑框/删除确认归
//    Entry 自己（条目级状态放条目组件——谁的状态归谁，不都堆顶层）
// ② 操作后 refetch：改/删成功 → 重新拉全量 → 新状态驱动新 UI，
//    全程没有一行手动 DOM；行号协议自洽（服务端删行后返回新行号）
// ③ 受控组件：编辑框 value 绑定本地 state，输入即状态
import { useEffect, useState } from "preact/hooks";
import { deleteEntry, fetchLearned, updateEntry } from "./api.js";

const CATEGORY_ZH = {
  decisions: "决定与理由",
  constraints: "约束与教训",
  other: "其他事实",
  user: "用户级记忆",
};
const CATEGORY_ORDER = ["decisions", "constraints", "other", "user"];
// 041：user.md 住在仓库外（~/.facta/），git 兜不住 → 误删没有
// `git checkout` 那条后路。不可逆性写在 UI 上，而不是假装能撤销。
const CATEGORY_HINT = {
  user: "跨项目生效 · 不进 git，删除不可撤销",
};

function Entry({ entry, onChanged }) {
  // 条目级三态：查看（默认）/ 编辑 / 删除确认——互斥，null = 查看
  const [mode, setMode] = useState(null);
  const [draft, setDraft] = useState("");   // 编辑草稿（受控）
  const [busy, setBusy] = useState(false);  // 请求在途：按钮禁用防双击

  const save = async () => {
    setBusy(true);
    try {
      // 089：按稳定 id 定位；base_content 是乐观锁（GET 时的可见文本原样带回）
      await updateEntry(entry.category, entry.id, draft, entry.content);
      setMode(null);
      await onChanged();   // 成功后由 App 重拉全量（新 id 从新数据来）
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await deleteEntry(entry.category, entry.id);
      await onChanged();
    } finally {
      setBusy(false);
    }
  };

  if (mode === "edit") {
    return (
      <div class="memory-entry editing">
        <textarea
          value={draft}
          onInput={(e) => setDraft(e.target.value)}
          rows={2}
        />
        <div class="memory-actions visible">
          <button onClick={save} disabled={busy || !draft.trim()}>保存</button>
          <button onClick={() => setMode(null)} disabled={busy}>取消</button>
        </div>
      </div>
    );
  }

  return (
    <div class="memory-entry">
      <span class="memory-date">{entry.date || "—"}</span>
      <span class="memory-content">{entry.content}</span>
      <span class="memory-actions">
        {mode === "delete" ? (
          <>
            <button class="danger" onClick={remove} disabled={busy}>确认删除</button>
            <button onClick={() => setMode(null)} disabled={busy}>取消</button>
          </>
        ) : (
          <>
            <button onClick={() => { setDraft(entry.content); setMode("edit"); }}>编辑</button>
            <button class="danger" onClick={() => setMode("delete")}>删除</button>
          </>
        )}
      </span>
    </div>
  );
}

export default function App() {
  const [state, setState] = useState({ loading: true });

  const load = async () => {
    try {
      const entries = await fetchLearned();
      setState({ entries });
    } catch {
      setState({ error: true });
    }
  };

  useEffect(() => { load(); }, []);   // 挂载即拉一次；记忆是落盘资产，无轮询必要

  if (state.loading) return <p class="muted">加载中…</p>;
  if (state.error) return <p class="muted">加载失败</p>;

  return (
    <div>
      {CATEGORY_ORDER.map((category) => {
        const items = state.entries.filter((e) => e.category === category);
        return (
          <section class="memory-group">
            <h3 class="memory-group-title">{CATEGORY_ZH[category]}</h3>
            {CATEGORY_HINT[category] && (
              <p class="muted memory-hint">{CATEGORY_HINT[category]}</p>
            )}
            {items.length === 0 ? (
              <p class="muted">暂无条目</p>
            ) : (
              items.map((entry) => (
                <Entry key={`${entry.category}:${entry.id}`} entry={entry} onChanged={load} />
              ))
            )}
          </section>
        );
      })}
    </div>
  );
}
