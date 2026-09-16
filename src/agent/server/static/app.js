// Personal Agent Web 壳（S2b v1）——对话视图前端，零构建链

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const cancelEl = document.getElementById("cancel");
const sessionListEl = document.getElementById("session-list");
const newSessionBtn = document.getElementById("new-session");

let currentRunId = null;      // 单 in-flight：同一时刻只允许一个 Run
let currentSource = null;     // 当前 EventSource
let sessionBusy = false;      // 会话切换/新开 in-flight：防双击造成后端交错
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
// 只渲染 assistant 消息（内容来自自家模型）；用户输入与工具结果永远
// textContent——不可信内容不进 innerHTML（注入防线）。
if (window.marked) marked.setOptions({ gfm: true, breaks: true });

function renderMarkdown(el, text) {
  if (window.marked && text) {
    el.innerHTML = marked.parse(text);
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

function onDone() {
  if (currentSource) { currentSource.close(); currentSource = null; }
  currentRunId = null;
  cancelEl.disabled = false;
  cancelEl.textContent = "取消";
  cancelEl.classList.add("hidden");
}

function setupEventSource(runId, turn) {
  const source = new EventSource(`/api/runs/${runId}/events`);
  currentSource = source;
  let pendingResultEl = null;

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

  source.addEventListener("run.completed", () => {
    // 终态渲染 markdown：流式中纯文本，收尾一次性成稿
    if (currentTextEl && pendingText) renderMarkdown(currentTextEl, pendingText);
    onDone();
  });
  source.addEventListener("run.failed", onDone);
  source.addEventListener("run.cancelled", () => {
    const el = document.createElement("div");
    el.className = "tool-card";
    el.textContent = "已取消本轮";
    turn.appendChild(el);
    scrollBottom();
    onDone();
  });
  source.onerror = onDone;   // EventSource 网络层错误（与业务 error 事件区分）
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || currentRunId) return;
  inputEl.value = "";

  addUser(text);
  const turn = addAssistantTurn();
  cancelEl.classList.remove("hidden");

  let resp;
  try {
    resp = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
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

  const runId = (await resp.json()).run_id;
  currentRunId = runId;
  setupEventSource(runId, turn);
}

// ---- 会话列表 / 切回 / 新会话 / 历史加载 ----

async function loadSessions() {
  try {
    const sessions = await (await fetch("/api/sessions")).json();
    sessionListEl.innerHTML = "";
    if (sessions.length === 0) {
      sessionListEl.innerHTML = '<li class="muted">暂无历史会话</li>';
      return;
    }
    for (const s of sessions) {
      const li = document.createElement("li");
      if (s.current) {
        li.classList.add("current");   // 当前会话：高亮常驻，不触发切回（它就在前台）
        li.title = "当前会话";
      } else {
        li.title = "点击切回此会话";
        li.addEventListener("click", () => switchTo(s.name));
      }
      const titleEl = document.createElement("div");
      titleEl.className = "session-title";
      titleEl.textContent = s.current ? "● " + s.title : s.title;
      li.appendChild(titleEl);
      if (s.time) {   // 归档时间（文件名解析）：小字第二行
        const timeEl = document.createElement("div");
        timeEl.className = "session-time";
        timeEl.textContent = s.time;
        li.appendChild(timeEl);
      }
      sessionListEl.appendChild(li);
    }
  } catch (_) {
    sessionListEl.innerHTML = '<li class="muted">加载会话失败</li>';
  }
}

function emptyHint(text) {
  messagesEl.innerHTML = "";
  const el = document.createElement("div");
  el.className = "muted empty-hint";
  el.textContent = text;
  messagesEl.appendChild(el);
}

async function loadMessages() {
  // 历史回放：active 会话的 user/assistant 轮（system/tool 轮后端已过滤）
  try {
    const msgs = await (await fetch("/api/messages")).json();
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

async function switchTo(name) {
  if (currentRunId) return;   // 有任务时不能切回（后端也会 409）
  if (sessionBusy) return;    // 防双击：切换是慢操作（提标题+固化），连点会交错
  sessionBusy = true;
  try {
    const resp = await fetch(`/api/sessions/${encodeURIComponent(name)}/switch`, { method: "POST" });
    if (!resp.ok) { alert(await resp.text()); return; }
    await loadMessages();       // 切回后回放完整历史，而不是只显示一句提示
    loadSessions();             // 目标会话已移回 active，刷新列表
  } finally {
    sessionBusy = false;
  }
}

async function newSession() {
  if (currentRunId) return;
  if (sessionBusy) return;
  sessionBusy = true;
  try {
    const resp = await fetch("/api/sessions/new", { method: "POST" });
    if (!resp.ok) { alert(await resp.text()); return; }
    emptyHint("开始新的对话吧");
    loadSessions();             // 当前会话已归档，刷新列表
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
loadSessions();
loadMessages();
loadTodos();
