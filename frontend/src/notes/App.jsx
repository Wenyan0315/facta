// 知识语料面板主组件（042）——notes 原文的列表与编辑，FW 新栈第四页。
//
// 与记忆面板（单行条目改删）相比，本页教学靶心是「整文件编辑」独有的两件事：
// ① 乐观锁：载入时记住服务端给的 hash，保存时原样送回；服务端与磁盘现值比对，
//    不一致回 409 → 不覆盖，引导重新载入。挡的事故很具体：用户在 IDE 里改着
//    同一篇 md，面板一保存就是整文件盲覆盖（比 041 的「行号错位」严重一个量级）
// ② 陈旧投影：存盘成功 ≠ 知识更新。向量库与图谱都是旧内容的投影，服务端按指纹
//    如实回报哪边陈旧，这里就照实显示 + 给出各自的出路，不假装已同步
// ③ 未保存拦截：改动在手时切笔记要确认、关标签页要拦一下——数据丢失的口子
//    堵在 UI 层，而不是指望用户记得
import { useEffect, useState } from "preact/hooks";
import { fetchNote, fetchNotes, saveNote, syncNotes } from "./api.js";

export default function App() {
  const [names, setNames] = useState([]);      // [{name, size}]
  const [selected, setSelected] = useState(null);
  const [doc, setDoc] = useState(null);        // {content, hash} 服务端现值（保存基线）
  const [draft, setDraft] = useState("");      // 编辑框内容（受控）
  const [err, setErr] = useState(null);        // {status, message}
  const [busy, setBusy] = useState(false);     // 保存/同步在途：按钮禁用防双击
  const [stale, setStale] = useState(null);    // {kb, graph} 保存后服务端回报
  const [syncMsg, setSyncMsg] = useState(null);

  const dirty = doc != null && draft !== doc.content;

  // 载入一篇：拿原文 + 当前指纹，指纹即之后的保存基线
  const load = (name) => {
    setErr(null);
    setStale(null);
    setSyncMsg(null);
    return fetchNote(name)
      .then((d) => {
        setDoc({ content: d.content, hash: d.hash });
        setDraft(d.content);
      })
      .catch((e) => {
        setDoc(null);
        setErr({ status: e.status, message: e.message });
      });
  };

  // 挂载：拉列表，并按 URL 的 ?n= 选中——图谱面板「出自：X.md」链接就是走这个入口，
  // 闭合「发现脏数据 → 去核对原文」那一圈（035 已知边界①的处置）
  useEffect(() => {
    const want = new URLSearchParams(location.search).get("n");
    fetchNotes()
      .then((list) => {
        setNames(list);
        const target = list.find((it) => it.name === want) || list[0];
        if (target) setSelected(target.name);
      })
      .catch((e) => setErr({ message: `列表加载失败：${e.message}` }));
  }, []);

  // 切笔记即载入（首次选中也走这里）
  useEffect(() => {
    if (selected) load(selected);
  }, [selected]);

  // 改动在手时拦住刷新/关标签页（切笔记另由 choose 里的 confirm 挡）
  useEffect(() => {
    if (!dirty) return undefined;
    const stop = (e) => e.preventDefault();
    window.addEventListener("beforeunload", stop);
    return () => window.removeEventListener("beforeunload", stop);
  }, [dirty]);

  const choose = (name) => {
    if (name === selected) return;
    if (dirty && !window.confirm("这篇还有未保存的改动，切换会丢掉。继续？")) return;
    setSelected(name);
  };

  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await saveNote(selected, draft, doc.hash);
      // 新基线 = 刚存下的内容与 hash（下次保存拿它当凭证）
      setDoc({ content: draft, hash: r.hash });
      setStale(r.stale);
    } catch (e) {
      setErr({ status: e.status, message: e.message });
    } finally {
      setBusy(false);
    }
  };

  const sync = async () => {
    setBusy(true);
    setSyncMsg(null);
    try {
      const r = await syncNotes();
      setSyncMsg(`向量库已对齐：新增 ${r.added}、删除 ${r.removed}、未变 ${r.unchanged}`);
      // 只救得了向量库这一边；图谱要重建（走 LLM 花钱，按钮在图谱面板）
      setStale((s) => (s ? { ...s, kb: false } : s));
    } catch (e) {
      setSyncMsg(`同步失败：${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="notes-panel">
      <div class="notes-toolbar">
        <strong>{names.length}</strong>
        <span class="muted">篇笔记</span>
        <span class="notes-toolbar-gap" />
        <button onClick={sync} disabled={busy}>同步向量库</button>
        {syncMsg && <span class="muted notes-sync-msg">{syncMsg}</span>}
      </div>

      <div class="notes-body">
        <ul class="notes-list">
          {names.length === 0 && <li class="muted">知识库还没有笔记</li>}
          {names.map((it) => (
            <li
              key={it.name}
              class={it.name === selected ? "notes-item selected" : "notes-item"}
              onClick={() => choose(it.name)}
            >
              <span class="notes-name">{it.name}</span>
              <span class="muted notes-size">{(it.size / 1024).toFixed(1)}K</span>
            </li>
          ))}
        </ul>

        <div class="notes-editor">
          {err && (
            <div class="notes-banner notes-banner-err">
              <span>{err.message}</span>
              {err.status === 409 && <button onClick={() => load(selected)}>重新载入磁盘版本</button>}
            </div>
          )}
          {stale && (stale.kb || stale.graph) && (
            <div class="notes-banner notes-banner-warn">
              <span>已存盘，但下游还是旧内容的投影：</span>
              {stale.kb && <span>向量库陈旧（点上方「同步向量库」）</span>}
              {stale.graph && (
                <span>
                  知识图谱陈旧（去 <a href="/graph">图谱面板</a> 重建）
                </span>
              )}
            </div>
          )}
          {doc ? (
            <>
              <textarea
                class="notes-textarea"
                value={draft}
                onInput={(e) => setDraft(e.target.value)}
                spellcheck={false}
              />
              <div class="notes-actions">
                <button onClick={save} disabled={busy || !dirty}>保存</button>
                <button onClick={() => load(selected)} disabled={busy || !dirty}>放弃改动</button>
                {dirty && <span class="muted">未保存</span>}
              </div>
            </>
          ) : (
            !err && <p class="muted">选择左侧一篇笔记</p>
          )}
        </div>
      </div>
    </div>
  );
}
