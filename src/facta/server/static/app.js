// Personal Agent Web 壳（S2b v1）——对话视图前端，零构建链

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const cancelEl = document.getElementById("cancel");
const sessionListEl = document.getElementById("session-list");
const newSessionBtn = document.getElementById("new-session");

let currentSessionId = null;  // 「当前会话」是前端概念（S8a）：后端不再有 active 特例
let currentRunId = null;      // 可见会话的 in-flight Run；切走即脱钩，Run 在后端照跑
let currentSource = null;     // 当前 EventSource
let sessionBusy = false;      // 会话切换 in-flight：防双击造成回放交错
let sessionMetas = [];        // 最近一次 /api/sessions 结果（running 标记要查它）
let pendingText = "";         // 当前 assistant 回合累积的流式文本
let currentTextEl = null;     // 当前正在累积文本的元素（工具卡片后会重置）
let rafPending = false;

// ---- 渲染基础 ----

function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---- Markdown 渲染 ----
// 流式中用 textContent（快，pre-wrap 保换行）；终态渲染一次 md。
// 用户输入与工具结果永远 textContent——不可信内容不进 innerHTML。
// XSS 修复（评审修复轮）：assistant 消息同样是不可信内容（模型可能复述
// 外部网页/用户输入里的恶意 HTML——assistant 身份≠内容安全）。两道防线：
// ① escape-before-parse：HTML 标签全部变文本（md 语法不受影响），
//    script/onerror 注入面结构性消失；② 渲染后 <a> 协议白名单，
//    挡 javascript: 链接（escape 不影响 href 内容）。零新依赖。
if (window.marked) marked.setOptions({ gfm: true, breaks: true });

function sanitizeLinks(el) {
  el.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") || "";
    if (!/^(https?:|mailto:|#)/i.test(href)) a.removeAttribute("href");
  });
}

function renderMarkdown(el, text) {
  if (window.marked && text) {
    el.innerHTML = marked.parse(escapeHtml(text));
    sanitizeLinks(el);
    el.classList.add("md-rendered");   // 关掉 pre-wrap：换行交给 md 的 <br>
  } else el.textContent = text || "";
}

function scheduleRender() {
  // rAF 节流：网络高频收 text.delta，界面一帧合并画一次
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    if (currentTextEl) currentTextEl.textContent = pendingText;
    scrollBottom();
  });
}

function newTextEl(turn) {
  const el = document.createElement("div");
  el.className = "bubble assistant";
  turn.appendChild(el);
  currentTextEl = el;
  pendingText = "";
  return el;
}

function addUser(text) {
  const el = document.createElement("div");
  el.className = "bubble user";
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollBottom();
}

function addAssistantTurn() {
  const turn = document.createElement("div");
  turn.className = "assistant-turn";
  messagesEl.appendChild(turn);
  newTextEl(turn);
  return turn;
}

function addToolCard(turn, name, rawArgs) {
  currentTextEl = null;   // 工具卡片会打断文本流，之后文本另起新元素
  const card = document.createElement("div");
  card.className = "tool-card";

  let title = name;
  try {
    const args = JSON.parse(rawArgs || "{}");
    if (name === "write_note" || name.includes("write")) title = `写入文件 ${args.filename || ""}`;
  } catch (_) { /* arguments 非 JSON 时保持原名 */ }

  const nameEl = document.createElement("div");
  nameEl.className = "tool-name";
  nameEl.textContent = "🔧 " + title;
  const resultEl = document.createElement("div");
  resultEl.className = "tool-result";
  card.appendChild(nameEl);
  card.appendChild(resultEl);
  turn.appendChild(card);
  scrollBottom();
  return resultEl;
}

// ---- 事件源与 Run 生命周期 ----

// ---- L2 确认弹窗（S4b）----
const confirmModal = document.getElementById("confirm-modal");
const confirmToolEl = document.getElementById("confirm-tool");
const confirmArgsEl = document.getElementById("confirm-args");

function showConfirm(runId, tool, args) {
  confirmToolEl.textContent = "工具：" + tool;
  // 事件里的 arguments 已是对象（SSE JSON 解析过）；字符串形态仅防御性兜底。
  // 验收踩坑：拿对象去 JSON.parse → 异常 → catch 里对象直进 textContent →
  // 弹窗显示 [object Object]，命令全文没露给用户（裁决依据缺失）
  let obj = args;
  if (typeof args === "string") {
    try { obj = JSON.parse(args || "{}"); } catch (_) { obj = { command: args }; }
  }
  const pretty = (obj && obj.command) || JSON.stringify(obj ?? {}, null, 2);
  confirmArgsEl.textContent = pretty;
  confirmModal.classList.remove("hidden");
  document.getElementById("confirm-approve").onclick = () => decideConfirm(runId, true);
  document.getElementById("confirm-reject").onclick = () => decideConfirm(runId, false);
}

function hideConfirm() {
  confirmModal.classList.add("hidden");
}

async function decideConfirm(runId, approve) {
  hideConfirm();   // 先收窗：裁决已落子，后端 resolved 事件随后也会到（幂等）
  await fetch(`/api/runs/${runId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approve }),
  });
}

function detachStream() {
  // 脱钩可见会话的事件流（收尾、切会话都走这里）。Run 在后端照跑——
  // 切回来时按 session 查 in-flight Run 重挂，SSE 从 seq 0 全量重放本轮。
  if (currentSource) { currentSource.close(); currentSource = null; }
  currentRunId = null;
  hideConfirm();
  cancelEl.disabled = false;
  cancelEl.textContent = "取消";
  cancelEl.classList.add("hidden");
}

function onDone() {
  detachStream();
  loadSessions();   // 标题是收尾时才补的（settle_session），Run 结束要刷清单
}

function setupEventSource(runId, turn) {
  const source = new EventSource(`/api/runs/${runId}/events`);
  currentSource = source;
  let pendingResultEl = null;
  // 收尾提示（S8a）：补标题 + 增量固化都要调 LLM，终态前有几秒静默——
  // 显式说出来，别让人以为是卡死。终态到达即撤掉。
  let settlingEl = null;
  const clearSettling = () => { if (settlingEl) { settlingEl.remove(); settlingEl = null; } };

  source.addEventListener("text.delta", (e) => {
    pendingText += JSON.parse(e.data).data.delta || "";
    scheduleRender();
  });

  source.addEventListener("tool.started", (e) => {
    const d = JSON.parse(e.data).data;
    pendingResultEl = addToolCard(turn, d.name, d.arguments);
  });

  source.addEventListener("tool.result", (e) => {
    const d = JSON.parse(e.data).data;
    if (pendingResultEl) pendingResultEl.textContent = d.result || "";
    pendingResultEl = null;
    newTextEl(turn);   // 工具结果之后，模型继续说话从新文本开始
  });

  source.addEventListener("max_rounds", () => {
    const el = document.createElement("div");
    el.className = "tool-card";
    el.textContent = "已达到工具调用轮数上限，强制结束本轮";
    turn.appendChild(el);
    scrollBottom();
  });

  source.addEventListener("stuck", () => {
    const el = document.createElement("div");
    el.className = "tool-card";
    el.textContent = "检测到原地重复调用，已停止本轮——可换个说法或缩小任务重试";
    turn.appendChild(el);
    scrollBottom();
  });

  // L2 确认（S4b）：request 弹窗 → resolved 收窗。事件按 seq 序到达，
  // 断线重放时 request 无 resolved 配对则弹窗自然重现（确认不丢）。
  source.addEventListener("confirm.request", (e) => {
    const d = JSON.parse(e.data).data;
    showConfirm(runId, d.tool, d.arguments);
  });
  source.addEventListener("confirm.resolved", () => hideConfirm());

  source.addEventListener("error", (e) => {
    if (!e.data) return;   // 原生 EventSource 网络错误（无 data），交给 onerror
    const d = JSON.parse(e.data).data;
    const el = document.createElement("div");
    el.className = "tool-card";
    el.style = "background:#fee2e2;border-color:#fecaca;";
    el.textContent = "[模型不可用] " + (d.message || "");
    turn.appendChild(el);
    scrollBottom();
  });

  source.addEventListener("run.settling", () => {
    settlingEl = document.createElement("div");
    settlingEl.className = "tool-card";
    settlingEl.textContent = "正在整理这段对话…";
    turn.appendChild(settlingEl);
    scrollBottom();
  });

  source.addEventListener("run.completed", () => {
    // 终态渲染 markdown：流式中纯文本，收尾一次性成稿
    if (currentTextEl && pendingText) renderMarkdown(currentTextEl, pendingText);
    clearSettling();
    onDone();
  });
  source.addEventListener("run.failed", () => { clearSettling(); onDone(); });
  source.addEventListener("run.cancelled", () => {
    clearSettling();
    const el = document.createElement("div");
    el.className = "tool-card";
    el.textContent = "已取消本轮";
    turn.appendChild(el);
    scrollBottom();
    onDone();
  });
  source.onerror = () => { clearSettling(); onDone(); };   // EventSource 网络层错误（与业务 error 事件区分）
}

async function send() {
  const text = inputEl.value.trim();
  // currentRunId 只是「可见会话」的在跑标记 ⇒ 同会话串行（后端准入同口径），
  // 别的会话在后台跑不挡这里——那正是 S8a 要的「长任务期间另开一段聊」
  if (!text || currentRunId) return;
  inputEl.value = "";
  inputEl.style.height = "";   // 高度复位到默认两行（auto-grow 的内联样式清掉）

  addUser(text);
  const turn = addAssistantTurn();
  cancelEl.classList.remove("hidden");

  // 省略 session_id = 后端新开一段对话，真实 id 从响应里认
  const payload = currentSessionId ? { text, session_id: currentSessionId } : { text };
  let resp;
  try {
    resp = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (_) {
    onDone();
    addToolCard(turn, "network", "");
    turn.querySelector(".tool-result").textContent = "无法连接后端";
    return;
  }

  if (!resp.ok) {
    onDone();
    currentTextEl.textContent = "[服务返回错误] " + (await resp.text());
    return;
  }

  const data = await resp.json();
  currentRunId = data.run_id;
  currentSessionId = data.session_id;
  setupEventSource(data.run_id, turn);
  loadSessions();   // 新会话入清单 / 清单标题跟上
}

// ---- 会话列表 / 切回 / 新会话 / 历史加载 ----

async function loadSessions() {
  try {
    sessionMetas = await (await fetch("/api/sessions")).json();
    sessionListEl.innerHTML = "";
    if (sessionMetas.length === 0) {
      sessionListEl.innerHTML = '<li class="muted">暂无历史会话</li>';
      return [];
    }
    for (const s of sessionMetas) {
      const li = document.createElement("li");
      const isCurrent = s.id === currentSessionId;
      if (isCurrent) {
        li.classList.add("current");   // 当前会话高亮（纯前端概念，后端不存 active）
        li.title = "当前会话";
      } else {
        li.title = "点击切到此会话";
        li.addEventListener("click", () => selectSession(s.id));
      }
      const titleEl = document.createElement("div");
      titleEl.className = "session-title";
      // ▶ = 该会话有 Run 在跑（多会话并发：切走的那段可能还在后台跑）
      titleEl.textContent = (isCurrent ? "● " : "") + (s.running ? "▶ " : "") + s.title;
      li.appendChild(titleEl);
      if (s.time) {   // 时间标签由 id（时间戳）解析：小字第二行
        const timeEl = document.createElement("div");
        timeEl.className = "session-time";
        timeEl.textContent = s.time;
        li.appendChild(timeEl);
      }
      // 行内操作（2026-09-17 体验轮）：重命名、删除。S8a 起没有「active 不能删」
      // 的特例，但在跑的会话后端会 409（worker 独占这段对话）⇒ 按钮直接不给。
      // 按钮 stopPropagation，不触发 li 的切换
      if (!s.running) {
        const actions = document.createElement("div");
        actions.className = "session-actions";
        const renameBtn = document.createElement("button");
        renameBtn.type = "button";
        renameBtn.textContent = "✎";
        renameBtn.title = "重命名";
        renameBtn.addEventListener("click", (e) => { e.stopPropagation(); startSessionRename(li, s); });
        actions.appendChild(renameBtn);
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.textContent = "×";
        delBtn.title = "删除";
        delBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm(`删除会话「${s.title}」？不可恢复。`)) return;
          await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
          if (s.id === currentSessionId) {   // 删的是眼前这段：视图回空态
            detachStream();
            currentSessionId = null;
            emptyHint("开始新的对话吧");
          }
          loadSessions();
        });
        actions.appendChild(delBtn);
        li.appendChild(actions);
      }
      sessionListEl.appendChild(li);
    }
    return sessionMetas;
  } catch (_) {
    sessionListEl.innerHTML = '<li class="muted">加载会话失败</li>';
    return [];
  }
}

function startSessionRename(li, s) {
  // 行内编辑（与待办同款交互）：标题换成输入框，Enter 保存 / Esc 取消 / 失焦保存
  const titleEl = li.querySelector(".session-title");
  if (!titleEl) return;
  const editor = document.createElement("input");
  editor.type = "text";
  editor.className = "session-edit";
  editor.value = s.title;
  titleEl.replaceWith(editor);
  editor.focus();
  editor.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const value = editor.value.trim();
    if (save && value && value !== s.title) {
      await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: value }),
      });
    }
    loadSessions();   // 重新渲染（无论存否都还原为列表态）
  };
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  editor.addEventListener("blur", () => finish(true));
}

function emptyHint(text) {
  messagesEl.innerHTML = "";
  const el = document.createElement("div");
  el.className = "muted empty-hint";
  el.textContent = text;
  messagesEl.appendChild(el);
}

async function loadMessages(sid) {
  // 历史回放：指定会话的 user/assistant 轮（system/tool 轮后端已过滤）
  try {
    const msgs = await (await fetch(`/api/sessions/${encodeURIComponent(sid)}/messages`)).json();
    messagesEl.innerHTML = "";
    for (const m of msgs) {
      if (m.role === "user") addUser(m.content);
      else {
        const turn = addAssistantTurn();
        renderMarkdown(turn.querySelector(".bubble"), m.content);
      }
    }
    if (msgs.length === 0) emptyHint("开始新的对话吧");
    scrollBottom();
  } catch (_) {
    emptyHint("历史加载失败，可直接开始新对话");
  }
}

// 切换会话（S8a）：纯前端换视图，后端没有 switch 端点了——「当前会话」不再
// 是服务端状态，切走的那段若在跑就让它后台跑完（这就是多会话并发的意义）。
async function selectSession(sid) {
  if (sid === currentSessionId) return;
  if (sessionBusy) return;    // 防双击：回放是慢操作，连点会交错
  sessionBusy = true;
  try {
    detachStream();             // 旧流脱钩（Run 不受影响），新会话从零开始渲染
    currentSessionId = sid;
    await loadMessages(sid);
    await loadSessions();       // 高亮跟上，running 标记也刷新一次
    await attachIfRunning(sid);
  } finally {
    sessionBusy = false;
  }
}

// 切到一个仍在跑的会话：查它的 in-flight Run 并重挂事件流。新建的 EventSource
// 不带 Last-Event-ID ⇒ 后端从 seq 0 全量重放，本轮内容一件不落。
const IN_FLIGHT = ["pending", "running", "waiting_approval"];

async function attachIfRunning(sid) {
  const meta = sessionMetas.find((s) => s.id === sid);
  if (!meta || !meta.running) return;
  try {
    const runs = await (await fetch(`/api/runs?session_id=${encodeURIComponent(sid)}`)).json();
    const live = runs.find((r) => IN_FLIGHT.includes(r.status));
    if (!live) return;
    currentRunId = live.run_id;
    cancelEl.classList.remove("hidden");
    setupEventSource(live.run_id, addAssistantTurn());
  } catch (_) { /* 重挂失败：历史已在屏，不打断用户 */ }
}

async function newSession() {
  if (sessionBusy) return;
  sessionBusy = true;
  try {
    // 不再需要「有任务在跑就不许新开」的守卫：旧会话的 Run 后台照跑
    const resp = await fetch("/api/sessions", { method: "POST" });
    if (!resp.ok) { alert(await resp.text()); return; }
    const id = (await resp.json()).id;
    detachStream();
    currentSessionId = id;
    emptyHint("开始新的对话吧");
    await loadSessions();       // 空会话也进清单（刚点的新建不该凭空消失）
  } finally {
    sessionBusy = false;
  }
}

// ---- 键盘习惯 ----
// Enter 发送 / Shift+Enter 换行 / isComposing 护住输入法组合中的 Enter（中文场景）
// Esc 取消当前任务
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && currentRunId) cancelEl.click();
});

// 多行自适应（2026-09-17 体验轮）：随内容长高，封顶后内部滚动。
// 先 height:auto 再读 scrollHeight——不归零会取到旧高度的较大值，只增不减
inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
});

// ---- 待办（014：任务=个人待办；与 agent 工具共用同一 store） ----

const todoListEl = document.getElementById("todo-list");
const todoFormEl = document.getElementById("todo-form");
const todoInputEl = document.getElementById("todo-input");

async function loadTodos() {
  try {
    const todos = await (await fetch("/api/todos")).json();
    todoListEl.innerHTML = "";
    for (const t of todos) {
      const li = document.createElement("li");
      li.className = "todo-item" + (t.done ? " done" : "");
      li.dataset.id = t.id;
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = t.done;
      box.disabled = t.done;   // 已完成不可点（勾销语义单向，与工具一致）
      box.addEventListener("change", async () => {
        await fetch(`/api/todos/${t.id}/complete`, { method: "POST" });
        loadTodos();
      });
      const label = document.createElement("span");
      label.textContent = t.text;
      label.title = "双击编辑";
      label.addEventListener("dblclick", () => startTodoEdit(li, t));
      const del = document.createElement("button");
      del.className = "todo-del";
      del.textContent = "×";
      del.title = "删除";
      del.addEventListener("click", async () => {
        await fetch(`/api/todos/${t.id}`, { method: "DELETE" });
        loadTodos();
      });
      li.appendChild(box);
      li.appendChild(label);
      li.appendChild(del);
      todoListEl.appendChild(li);
    }
    if (!todos.length) {
      todoListEl.innerHTML = '<li class="muted" style="cursor:default;">（无待办）</li>';
    }
  } catch (_) { /* 面板加载失败静默——不该挡住聊天主功能 */ }
}

function startTodoEdit(li, todo) {
  // 双击进入行内编辑：span 换成输入框，Enter 保存 / Esc 取消 / 失焦保存
  const label = li.querySelector("span");
  if (!label) return;
  const editor = document.createElement("input");
  editor.type = "text";
  editor.className = "todo-edit";
  editor.value = todo.text;
  label.replaceWith(editor);
  editor.focus();
  editor.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (save && editor.value.trim() && editor.value.trim() !== todo.text) {
      await fetch(`/api/todos/${todo.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: editor.value.trim() }),
      });
    }
    loadTodos();   // 重新渲染（无论存否都还原为列表态）
  };
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  editor.addEventListener("blur", () => finish(true));
}

todoFormEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = todoInputEl.value.trim();
  if (!text) return;
  todoInputEl.value = "";
  await fetch("/api/todos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  loadTodos();
});

// Run 结束时刷新待办——agent 可能在对话里动了待办（add/complete）
const _origOnDone = onDone;
onDone = function () { _origOnDone(); loadTodos(); };

// ---- 初始化 ----

document.getElementById("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  send();
});

cancelEl.addEventListener("click", async () => {
  if (!currentRunId) return;
  cancelEl.disabled = true;          // 即时反馈：不等下一个检查点，按钮先变
  cancelEl.textContent = "取消中…";
  try {
    await fetch(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
  } catch (_) { /* 网络失败：onDone 兜底恢复按钮 */ }
});

newSessionBtn.addEventListener("click", newSession);

// 启动：默认落在最近改动的会话（清单已按最后修改时刻降序）——后端不再有
// active 概念，「打开看到哪一段」纯粹是前端的选择。
(async function init() {
  const sessions = await loadSessions();
  if (sessions.length) await selectSession(sessions[0].id);
  else emptyHint("开始新的对话吧");
  loadTodos();
})();
