// 知识图谱面板主组件（S7b）——FW 新栈第三入口。
//
// 把「知识地图」做成能探索的一等公民，而不是一张静态图：
// - 力导向布局：自研力模拟（force.js）跑收敛，节点聚合、关系近的靠拢
// - 单击节点 → 高亮其 1 跳邻居；再点第二个节点 → 高亮两概念最短路径
//   （前端 BFS ≤3 跳，与后端 query_graph 的 path 原语同语义）
// - 拖拽节点（钉住）、滚轮缩放、拖空白平移
// - 搜索框定位节点、按概念类型显隐、关系四色图例、孤岛/枢纽视觉标注
// - 「重建图谱」→ POST /api/graph/rebuild（force 全量重抽）
//
// 状态分层：数据归 App，拖拽/视口交互归 ref（高频不触发整树 diff），
// 力模拟每帧只加一帧计数器驱动坐标重渲染。

import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { fetchGraph, rebuildGraph } from "./api.js";
import { energy, initLayout, tick } from "./force.js";

const W = 1000;   // 力模拟世界坐标系（viewBox 窗口可缩放平移）
const H = 700;

// 关系四色（图例 + 边/箭头颜色）——与后端 graph.RELATIONS 一一对应
const RELATION_COLORS = {
  依赖: "#e05d44",
  包含: "#3b82f6",
  对比: "#8b5cf6",
  引用: "#10b981",
};

// marker id 用 ASCII（中文 fragment 在 url(#…) 引用里有无编码坑，避开）
const RELATION_MARKER_ID = { 依赖: "dep", 包含: "cont", 对比: "cmp", 引用: "ref" };

// 节点类型色（未知类型兜灰）
const TYPE_COLORS = {
  概念: "#64748b",
  技术: "#0ea5e9",
  政策: "#f59e0b",
  工具: "#22c55e",
  语言: "#ec4899",
  方法: "#a855f7",
};
const DEFAULT_TYPE = "#94a3b8";

// BFS 找两点间最短路径（≤3 跳无向），返回边的索引数组；不连通返回 null
function findPath(neighbors, start, goal) {
  if (start === goal) return [];
  const queue = [{ node: start, path: [] }];
  const visited = new Set([start]);
  while (queue.length) {
    const { node, path } = queue.shift();
    if (path.length >= 3) continue;
    for (const [edgeIdx, nxt] of neighbors.get(node) || []) {
      if (visited.has(nxt)) continue;
      const newPath = [...path, edgeIdx];
      if (nxt === goal) return newPath;
      visited.add(nxt);
      queue.push({ node: nxt, path: newPath });
    }
  }
  return null;
}

export default function App() {
  const [state, setState] = useState({ loading: true });
  const [selected, setSelected] = useState(null);   // 选中节点 id
  const [goal, setGoal] = useState(null);           // 路径终点 id（无=不在路径模式）
  const [search, setSearch] = useState("");
  const [hidden, setHidden] = useState(() => new Set());  // 被过滤的概念类型
  const [building, setBuilding] = useState(false);
  const [msg, setMsg] = useState(null);             // 重建结果/上一动作反馈
  const [view, setView] = useState({ x: -120, y: -100, w: 1240, h: 900 });
  const [, bump] = useState(0);                     // 力模拟帧驱动重渲染

  const positionsRef = useRef([]);
  const dragRef = useRef(null);                     // { kind:"node"|"pan", idx?, x, y, moved }
  const svgRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchGraph();
      positionsRef.current = initLayout(data.nodes.length, W, H);   // 数据一到位就初始化位置——渲染时已就绪（勿放 useEffect：渲染后才跑，会读空数组）
      setState({ data });
      setSelected(null);
      setGoal(null);
    } catch {
      setState({ error: true });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // 预处理：索引、邻接表、度——渲染与力模拟的共享派生数据
  const prep = useMemo(() => {
    const data = state.data;
    if (!data) return null;
    const idToIdx = new Map(data.nodes.map((n, i) => [n.id, i]));
    const edgeIndices = data.edges
      .map((e) => [idToIdx.get(e.source), idToIdx.get(e.target)])
      .filter(([a, b]) => a != null && b != null);
    const degree = new Array(data.nodes.length).fill(0);
    const neighbors = new Map();
    edgeIndices.forEach(([a, b], i) => {
      degree[a] += 1;
      degree[b] += 1;
      (neighbors.get(a) ?? neighbors.set(a, []).get(a)).push([i, b]);
      (neighbors.get(b) ?? neighbors.set(b, []).get(b)).push([i, a]);
    });
    return { idToIdx, edgeIndices, degree, neighbors };
  }, [state.data]);

  // 数据就位 → 启动力模拟（能量收敛自动停摆）；位置已在 load 里初始化
  useEffect(() => {
    if (!prep) return;
    const edgeIndices = prep.edgeIndices;
    let raf = 0;
    let running = true;
    const loop = () => {
      if (!running) return;
      const pos = positionsRef.current;
      tick(pos, edgeIndices, W, H);
      if (dragRef.current?.kind === "node") {   // 拖拽节点钉住：速度清零，位置随鼠标
        const p = pos[dragRef.current.idx];
        p.vx = 0;
        p.vy = 0;
      }
      bump((k) => k + 1);
      if (energy(pos) < 0.05 && !dragRef.current) { running = false; return; }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => { running = false; cancelAnimationFrame(raf); };
  }, [prep]);

  if (state.loading) return <p class="muted">加载中…</p>;
  if (state.error) return <p class="muted">加载失败</p>;

  const { nodes, edges, stats } = state.data;
  const { idToIdx, edgeIndices, degree, neighbors } = prep;

  const types = [...new Set(nodes.map((n) => n.type || "概念"))];
  const searchHits = search.trim()
    ? nodes.filter((n, i) => !hidden.has(n.type) && (n.id.includes(search) || (n.aliases || []).some((a) => a.includes(search))))
    : [];

  // 高亮集合（优先级：路径 > 邻居 > 全显示）
  let focusNodes = null;
  let focusEdges = null;
  if (selected != null && goal != null) {
    const s = idToIdx.get(selected);
    const g = idToIdx.get(goal);
    const path = s != null && g != null ? findPath(neighbors, s, g) : null;
    if (path) {
      focusEdges = new Set(path);
      focusNodes = new Set([s, g]);
      for (const ei of path) { focusNodes.add(edgeIndices[ei][0]); focusNodes.add(edgeIndices[ei][1]); }
    }
  } else if (selected != null) {
    const s = idToIdx.get(selected);
    if (s != null) {
      focusNodes = new Set([s]);
      focusEdges = new Set();
      for (const [ei, other] of neighbors.get(s) || []) { focusNodes.add(other); focusEdges.add(ei); }
    }
  }

  // ---- 视口 / 拖拽交互 ----
  const toWorld = (e) => {
    const rect = svgRef.current.getBoundingClientRect();
    return {
      x: view.x + ((e.clientX - rect.left) / rect.width) * view.w,
      y: view.y + ((e.clientY - rect.top) / rect.height) * view.h,
    };
  };
  const onWheel = (e) => {
    e.preventDefault();
    const factor = e.deltaY > 0 ? 1.1 : 1 / 1.1;
    const p = toWorld(e);
    setView((v) => {
      const w = Math.max(200, v.w * factor);
      const h = Math.max(150, v.h * factor);
      // 缩放围绕鼠标点：保持鼠标下的世界坐标不动
      return { x: p.x - (p.x - v.x) * (w / v.w), y: p.y - (p.y - v.y) * (h / v.h), w, h };
    });
  };
  const onSvgMouseDown = (e) => {
    if (e.target.closest(".g-node")) return;   // 节点拖拽由节点自己的 handler 管
    dragRef.current = { kind: "pan", x: e.clientX, y: e.clientY, moved: false, startView: { ...view } };
  };
  const onNodeMouseDown = (e, idx) => {
    e.stopPropagation();
    dragRef.current = { kind: "node", idx, x: e.clientX, y: e.clientY, moved: false };
  };
  const onWindowMove = (e) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    if (d.kind === "pan") {
      const rect = svgRef.current.getBoundingClientRect();
      const sx = (dx / rect.width) * view.w;
      const sy = (dy / rect.height) * view.h;
      setView({ ...d.startView, x: d.startView.x - sx, y: d.startView.y - sy });
    } else if (d.kind === "node") {
      const p = toWorld(e);
      const n = positionsRef.current[d.idx];
      n.x = p.x;
      n.y = p.y;
      bump((k) => k + 1);
    }
  };
  const onWindowUp = () => {
    const d = dragRef.current;
    if (!d) return;
    if (d.kind === "node" && !d.moved) {
      // 单击（未拖动）= 选中/路径，语义见文件头
      const nodeId = nodes[d.idx].id;
      if (goal != null) { setGoal(null); setSelected(null); }
      else if (selected != null && selected !== nodeId) setGoal(nodeId);
      else if (selected === nodeId) { setSelected(null); setGoal(null); }
      else setSelected(nodeId);
    }
    dragRef.current = null;
  };
  useEffect(() => {
    window.addEventListener("mousemove", onWindowMove);
    window.addEventListener("mouseup", onWindowUp);
    return () => {
      window.removeEventListener("mousemove", onWindowMove);
      window.removeEventListener("mouseup", onWindowUp);
    };
  });

  const rebuild = async () => {
    setBuilding(true);
    setMsg(null);
    try {
      const res = await rebuildGraph();
      setMsg(`抽取 ${res.report.extracted} 篇 / 不变 ${res.report.unchanged} 篇 / 删除 ${res.report.removed} 篇`);
      await load();
    } catch {
      setMsg("重建失败（服务端错误）");
    } finally {
      setBuilding(false);
    }
  };

  const focusOn = (idx) => {
    const p = positionsRef.current[idx];
    setView((v) => ({ x: p.x - v.w / 2, y: p.y - v.h / 2, w: v.w, h: v.h }));
    setSelected(nodes[idx].id);
    setGoal(null);
    setSearch("");
  };

  return (
    <div class="graph-panel">
      <div class="graph-toolbar">
        <div class="graph-stats">
          <strong>知识图谱</strong>
          <span class="muted">{stats.nodes} 概念 · {stats.edges} 关系</span>
        </div>
        <input
          class="graph-search"
          type="text"
          placeholder="搜索概念（回车定位第一个）…"
          value={search}
          onInput={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && searchHits.length) focusOn(idToIdx.get(searchHits[0].id));
          }}
        />
        <div class="graph-filters">
          {types.map((t) => (
            <button
              key={t}
              class={`graph-chip ${hidden.has(t) ? "off" : ""}`}
              onClick={() => {
                const next = new Set(hidden);
                next.has(t) ? next.delete(t) : next.add(t);
                setHidden(next);
              }}
            >
              <span class="chip-dot" style={{ background: TYPE_COLORS[t] || DEFAULT_TYPE }} />
              {t}
            </button>
          ))}
        </div>
        <button class="graph-rebuild" onClick={rebuild} disabled={building}>
          {building ? "重建中…" : "重建图谱"}
        </button>
      </div>

      <div class="graph-legend">
        {Object.entries(RELATION_COLORS).map(([rel, color]) => (
          <span key={rel} class="legend-item">
            <span class="legend-line" style={{ background: color }} />
            {rel}
          </span>
        ))}
        <span class="legend-item muted">○ 孤岛（无关系）</span>
      </div>

      {searchHits.length > 0 && (
        <div class="graph-search-drop">
          {searchHits.slice(0, 8).map((n) => (
            <div key={n.id} class="graph-search-hit" onClick={() => focusOn(idToIdx.get(n.id))}>
              {n.id}
            </div>
          ))}
        </div>
      )}

      {msg && <div class="graph-msg">{msg}</div>}

      <svg
        ref={svgRef}
        class="graph-svg"
        viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
        onWheel={onWheel}
        onMouseDown={onSvgMouseDown}
      >
        <defs>
          {Object.entries(RELATION_COLORS).map(([rel, color]) => (
            <marker key={rel} id={`arrow-${RELATION_MARKER_ID[rel]}`} viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
            </marker>
          ))}
        </defs>

        {edges.map((e, i) => {
          const [a, b] = edgeIndices[i];
          if (hidden.has(nodes[a].type) || hidden.has(nodes[b].type)) return null;
          const pa = positionsRef.current[a];
          const pb = positionsRef.current[b];
          const dim = focusEdges != null && !focusEdges.has(i);
          const focused = focusEdges != null && focusEdges.has(i);
          return (
            <line
              key={i}
              x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y}
              stroke={RELATION_COLORS[e.relation] || "#cbd5e1"}
              strokeWidth={focused ? 3 : 1.5}
              opacity={dim ? 0.12 : 0.75}
              markerEnd={`url(#arrow-${RELATION_MARKER_ID[e.relation] || "ref"})`}
            />
          );
        })}

        {nodes.map((n, i) => {
          if (hidden.has(n.type)) return null;
          const p = positionsRef.current[i];
          const r = Math.min(30, 12 + degree[i] * 1.5);
          const isIsland = degree[i] === 0;
          const dim = focusNodes != null && !focusNodes.has(i);
          const focused = focusNodes != null && focusNodes.has(i);
          const isSelected = selected === n.id || (goal != null && (selected === n.id || goal === n.id));
          return (
            <g
              key={n.id}
              class="g-node"
              transform={`translate(${p.x},${p.y})`}
              opacity={dim ? 0.15 : 1}
              onMouseDown={(e) => onNodeMouseDown(e, i)}
              style={{ cursor: "grab" }}
            >
              <circle
                r={r}
                fill={TYPE_COLORS[n.type] || DEFAULT_TYPE}
                stroke={isIsland ? "#94a3b8" : "none"}
                strokeWidth={isIsland ? 1.5 : 0}
                strokeDasharray={isIsland ? "4 3" : undefined}
              />
              <circle r={r + 5} fill="none" stroke={isSelected ? "#1f2937" : focused ? "#f59e0b" : "transparent"} strokeWidth={2} />
              <text y={r + 14} textAnchor="middle" class="graph-node-label">{n.id}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}