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

function onDone() {
  if (currentSource) { currentSource.close(); currentSource = null; }
  currentRunId = null;
  hideConfirm();
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
  inputEl.style.height = "";   // 高度复位到默认两行（auto-grow 的内联样式清掉）

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
      // 行内操作（2026-09-17 体验轮）：重命名（active/归档都可）、删除（仅归档——
      // 活会话先归档再删）。按钮 stopPropagation，不触发 li 的切回
      const actions = document.createElement("div");
      actions.className = "session-actions";
      const renameBtn = document.createElement("button");
      renameBtn.type = "button";
      renameBtn.textContent = "✎";
      renameBtn.title = "重命名";
      renameBtn.addEventListener("click", (e) => { e.stopPropagation(); startSessionRename(li, s); });
      actions.appendChild(renameBtn);
      if (!s.current) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.textContent = "×";
        delBtn.title = "删除";
        delBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm(`删除会话「${s.title}」？不可恢复。`)) return;
          await fetch(`/api/sessions/${encodeURIComponent(s.name)}`, { method: "DELETE" });
          loadSessions();
        });
        actions.appendChild(delBtn);
      }
      li.appendChild(actions);
      sessionListEl.appendChild(li);
    }
  } catch (_) {
    sessionListEl.innerHTML = '<li class="muted">加载会话失败</li>';
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
      await fetch(`/api/sessions/${encodeURIComponent(s.name)}`, {
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
loadSessions();
loadMessages();
loadTodos();
