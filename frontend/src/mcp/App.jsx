// MCP 配置面板主组件（106）——FW 第五页。
//
// 结构沿 memory 面板同款分层：清单归 App（一份真相），编辑/删除确认归
// ServerRow（条目级状态放条目组件）；操作成功后 refetch 拿新 hash 与
// 对照状态。三条面板特有的诚实呈现：
// ① 待重启横幅：文件里 enabled 的名单 vs 当前进程 active 名单不一致
//    （MCP 装配在服务启动时一次完成，改清单不重启不生效）
// ② env 覆盖横幅：MCP_SERVERS 指向的个人文件才是当前生效配置
// ③ headers 只显示有无（值在服务端就被拦下，密钥不经 UI）
import { useEffect, useState } from "preact/hooks";
import { addServer, deleteServer, fetchMcp, updateServer } from "./api.js";

// command 是数组，编辑框用逗号分隔文本往返（命令参数里逗号罕见；
// 真有含逗号的参数请手编清单——面板不假装能表达一切）
const toCmdText = (cmd) => (cmd || []).join(", ");
const fromCmdText = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);

function ServerForm({ initial, busy, err, onSave, onCancel }) {
  // initial 为 null = 新增（name 可填）；否则编辑（name 只读——它是条目身份）
  const [name, setName] = useState(initial?.name ?? "");
  const [type, setType] = useState(initial?.command ? "stdio" : "http");
  const [cmdText, setCmdText] = useState(toCmdText(initial?.command));
  const [url, setUrl] = useState(initial?.url ?? "");
  const [prefix, setPrefix] = useState(initial?.prefix ?? "");
  const [timeout, setTimeout] = useState(initial?.timeout ?? 30);

  const buildBody = (hash) => ({
    command: type === "stdio" ? fromCmdText(cmdText) : null,
    url: type === "http" ? url : null,
    prefix: prefix.trim(),
    timeout: Number(timeout) || 30,
    enabled: initial ? initial.enabled : true,
    base_hash: hash,
  });

  return (
    <div class="memory-entry editing">
      {initial ? null : (
        <p>
          <input
            value={name}
            onInput={(e) => setName(e.target.value)}
            placeholder="服务器名（如 notes）"
          />
        </p>
      )}
      <p>
        <label><input type="radio" checked={type === "stdio"} onClick={() => setType("stdio")} /> stdio（本地命令）</label>
        {"　"}
        <label><input type="radio" checked={type === "http"} onClick={() => setType("http")} /> HTTP（远程 URL）</label>
      </p>
      {type === "stdio" ? (
        <p>
          <input
            value={cmdText}
            onInput={(e) => setCmdText(e.target.value)}
            placeholder='命令，逗号分隔（如 {python}, servers/notes_server.py）'
            style="width:100%;"
          />
        </p>
      ) : (
        <p>
          <input
            value={url}
            onInput={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/mcp"
            style="width:100%;"
          />
        </p>
      )}
      <p class="muted" style="font-size:12px;">
        prefix：<input value={prefix} onInput={(e) => setPrefix(e.target.value)} placeholder={`缺省 ${name || "name"}__`} />
        {"　"}timeout（秒）：<input value={timeout} onInput={(e) => setTimeout(e.target.value)} style="width:60px;" />
        {initial?.has_headers && "　含密钥头（headers 面板不展示，保存时保留）"}
      </p>
      <div class="memory-actions visible">
        <button
          onClick={() => onSave(name.trim(), buildBody())}
          disabled={busy || !name.trim() || (type === "stdio" ? !cmdText.trim() : !url.trim())}
        >
          保存
        </button>
        <button onClick={onCancel} disabled={busy}>取消</button>
      </div>
      {err && (
        <div class="notes-banner notes-banner-err"><span>{err.message}</span></div>
      )}
    </div>
  );
}

function ServerRow({ entry, baseHash, onChanged }) {
  const [mode, setMode] = useState(null);   // null / edit / delete，与 memory Entry 同款
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  // enabled 开关是高频操作，一步直达（不开编辑态）
  const toggle = async () => {
    setBusy(true);
    setErr(null);
    try {
      await updateServer(entry.name, {
        command: entry.command, url: entry.url, prefix: entry.prefix,
        timeout: entry.timeout, enabled: !entry.enabled, base_hash: baseHash,
      });
      await onChanged();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setErr(null);
    try {
      await deleteServer(entry.name, baseHash);
      await onChanged();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  const save = async (name, body) => {
    setBusy(true);
    setErr(null);
    try {
      await updateServer(entry.name, body);
      setMode(null);
      await onChanged();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  const errBanner = err && (
    <div class="notes-banner notes-banner-err">
      <span>{err.message}</span>
      {(err.status === 409 || err.status === 404) && (
        <button onClick={() => { setErr(null); setMode(null); onChanged(); }}>重新载入</button>
      )}
    </div>
  );

  if (mode === "edit") {
    return <ServerForm initial={entry} busy={busy} err={err} onSave={save} onCancel={() => setMode(null)} />;
  }

  // 运行时对照：active 名单来自服务启动时的装配（重启才刷新）
  const running = entry.enabled && entry.active;
  return (
    <div class="memory-entry">
      <span class="memory-date">
        {entry.enabled ? (running ? "运行中" : "待重启") : "已禁用"}
      </span>
      <span class="memory-content">
        <strong>{entry.name}</strong>
        {"　"}
        <span class="muted">{entry.command ? "stdio" : "HTTP"}</span>
        {"　"}
        <span class="muted" style="font-size:12px;">
          {entry.command ? entry.command.join(" ") : entry.url} · prefix {entry.prefix} · {entry.timeout}s
        </span>
      </span>
      <span class="memory-actions">
        {mode === "delete" ? (
          <>
            <button class="danger" onClick={remove} disabled={busy}>确认删除</button>
            <button onClick={() => setMode(null)} disabled={busy}>取消</button>
          </>
        ) : (
          <>
            <button onClick={toggle} disabled={busy}>{entry.enabled ? "停用" : "启用"}</button>
            <button onClick={() => setMode("edit")}>编辑</button>
            <button class="danger" onClick={() => setMode("delete")}>删除</button>
          </>
        )}
      </span>
      {errBanner}
    </div>
  );
}

function AddForm({ baseHash, onChanged }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const save = async (name, body) => {
    setBusy(true);
    setErr(null);
    try {
      await addServer(name, body);
      setOpen(false);
      await onChanged();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  if (!open) return <button onClick={() => setOpen(true)}>＋ 新增服务器</button>;
  return (
    <div>
      <ServerForm initial={null} busy={busy} err={err} onSave={save} onCancel={() => setOpen(false)} />
    </div>
  );
}

export default function App() {
  const [state, setState] = useState({ loading: true });

  const load = async () => {
    try {
      setState({ data: await fetchMcp() });
    } catch {
      setState({ error: true });
    }
  };

  useEffect(() => { load(); }, []);

  if (state.loading) return <p class="muted">加载中…</p>;
  if (state.error) return <p class="muted">加载失败</p>;

  const { data } = state;
  const enabledNames = data.servers.filter((s) => s.enabled).map((s) => s.name);
  const pendingAdd = enabledNames.filter((n) => !data.active.includes(n));
  const pendingRemove = data.active.filter((n) => !enabledNames.includes(n));
  const pending = pendingAdd.length + pendingRemove.length > 0;

  return (
    <div>
      {data.parse_error && (
        <div class="notes-banner notes-banner-err">
          <span>清单文件损坏，面板无法写入：{data.parse_error}（请手修或删除该文件）</span>
        </div>
      )}
      {data.env_override && (
        <div class="notes-banner">
          <span>当前生效的是 MCP_SERVERS 指向的个人文件：{data.effective_path}——面板改动不影响当前运行配置（密钥文件不经面板读写）</span>
        </div>
      )}
      {pending && (
        <div class="notes-banner">
          <span>
            清单与当前运行时不一致，重启服务后生效：
            {pendingAdd.length > 0 && ` 待接入 [${pendingAdd.join(", ")}]`}
            {pendingRemove.length > 0 && ` 待移除 [${pendingRemove.join(", ")}]`}
          </span>
        </div>
      )}
      <p class="muted" style="font-size:12px;">
        清单文件：{data.managed_path}（面板管文件不管进程；MCP 装配在服务启动时一次完成）
      </p>

      {data.servers.length === 0 ? (
        <p class="muted">暂无服务器（新增一台，或手编清单后重启）</p>
      ) : (
        data.servers.map((s) => (
          <ServerRow
            key={s.name}
            entry={{ ...s, active: data.active.includes(s.name) }}
            baseHash={data.hash}
            onChanged={load}
          />
        ))
      )}
      <p><AddForm baseHash={data.hash} onChanged={load} /></p>
    </div>
  );
}
