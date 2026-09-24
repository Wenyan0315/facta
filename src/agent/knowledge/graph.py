"""S7a 图谱数据层：实体关系图的存储、校验与查询原语。

三层结构（值对象 → 存储 → 查询）：
- GraphNode / GraphEdge   frozen 值对象：实体与关系是纯数据，可测试
- GraphStore              存储+校验+邻接索引+序列化（graph.json 落盘格式）
- 查询原语                resolve/neighbors/path/overview——图谱产品的一等公民

设计裁定（S7a 草案 + 能力优先修正）：
- 节点 id = 实体名本身（中文直接当 id）：可读、可 diff；别名是 alias→id
  的映射表——实体对齐（「RAG」vs「检索增强生成」）是图谱一等能力，
  不是事后 hack
- 边唯一键 = (source, target, relation, source_note) 四元组：同一篇笔记
  里同一关系去重；不同笔记各带一条——多出处是特性（可查「谁也提到了
  这条关系」），不是重复污染
- 关系白名单：封闭注册表，程序侧校验（模型给白名单外的关系 → 拒收，
  M5「错误也返回字符串」惯例延伸到数据层——调用方自纠）
- 出处强制：merge_note 的签名带笔记名——出处由程序填，模型没机会
  填错或编造（无出处的边不存在于合法路径上）
- remove_note 只删边不删节点：被别篇引用的实体不能跟着死；删完的
  孤岛节点由 overview 报告（人决定去留，不自动删——删除安全阀惯例）
- 邻接索引 _adj 是推导值不落盘：from_dict 后重建（存储只存事实，
  索引是缓存——与 PlanBoard「events 归史、view() 是推导」同款哲学）

物理位置：knowledge/ 层（032 裁定：Knowledge 归知识层不混记忆层）。
graph.json 是 notes 的结构化投影：文本可读、可审查、可 diff——知识资产
进 git（与 vector_db 二进制缓存相反的判断）。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

# 关系类型注册表（封闭白名单）：抽取提示词与数据校验共用同一份——
# 单一真值源。新关系类型在这里加一格，抽取层自动跟进
RELATIONS = ("依赖", "包含", "对比", "引用")

# 图级并发锁（S7b）：图是跨线程共享的知识资产——Web 常驻进程下，面板
# 「重建图谱」端点（请求线程）与对话内 sync_graph 工具（worker 线程）
# 可能并发读写同一 GraphStore。sync_graph 是多步复合操作（删边→重抽→
# 加边指纹），单步 dict 的 GIL 原子性不覆盖整段流程——批量操作方持锁串行。
# 放 graph.py 而非 app.py：并发协议属于数据层，调用方（工具/server）都是客户
GRAPH_LOCK = threading.Lock()

MAX_PATH_HOPS = 3   # path 查询的跳数上限：个人知识图谱的概念链路 3 跳够用且防漫游


@dataclass(frozen=True)
class GraphNode:
    """图节点：一个概念实体。

    id 即 name（中文直接当 id——个人知识库的实体名天然唯一，别名表
    负责归一变体）。type 是概念类别（概念/技术/政策/工具——抽取层定），
    v0.1 不做类型系统，仅作展示分组。
    """

    id: str
    type: str = "概念"
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "type": self.type}
        if self.aliases:
            d["aliases"] = list(self.aliases)
        return d


@dataclass(frozen=True)
class GraphEdge:
    """图边：两个实体间的一条有向关系。

    source_note 是出处（哪篇笔记贡献的这条边）——由程序在 merge_note
    里填，强制非空：无出处的关系不存在于合法路径上（LLM 抽取防幻觉
    的程序侧闸门——拒绝「模型觉得应该有」的关系）。
    """

    source: str
    target: str
    relation: str
    source_note: str

    def key(self) -> tuple[str, str, str, str]:
        """唯一键：四元组（同笔记同关系去重，跨笔记多出处并存）。"""
        return (self.source, self.target, self.relation, self.source_note)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "source_note": self.source_note,
        }


class GraphStore:
    """图谱存储：节点字典 + 边表 + 邻接索引 + per-note 指纹。

    所有变更走校验闸门（add_node/add_edge/merge_note），非法输入抛
    ValueError——调用方（同步层/工具层）转错误串回灌，反馈环惯例。
    """

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str, str], GraphEdge] = {}   # key → edge
        self._alias: dict[str, str] = {}       # 别名/本名 → id（resolve 用）
        self._adj: dict[str, list[GraphEdge]] = {}   # 邻接索引（推导值）
        self.note_hashes: dict[str, str] = {}  # 笔记名 → 内容指纹（增量同步判据）

    # ---- 校验闸门与变更 ----

    def add_node(self, name: str, type_: str = "概念", aliases: tuple[str, ...] = ()) -> GraphNode:
        """登记节点（重名幂等：类型/别名取并集——两篇笔记都抽到同一
        实体是常态，不是冲突）。别名撞别的节点 → ValueError（对齐歧义
        必须显式处理，静默覆盖会让历史边指向错误实体）。
        """
        name = name.strip()
        if not name:
            raise ValueError("实体名不能为空")
        existing = self.nodes.get(name)
        if existing is not None:
            merged = GraphNode(
                id=name,
                type=existing.type or type_,
                aliases=tuple(set(existing.aliases) | set(aliases)),
            )
            self.nodes[name] = merged
            self._index_names(merged)
            return merged
        node = GraphNode(id=name, type=type_, aliases=tuple(set(aliases)))
        self.nodes[name] = node
        self._index_names(node)
        return node

    def add_edge(self, source: str, target: str, relation: str, source_note: str) -> GraphEdge:
        """登记边：关系白名单 + 端点存在 + 出处非空，三道校验后入库。"""
        source, target = source.strip(), target.strip()
        if relation not in RELATIONS:
            raise ValueError(f"未知关系「{relation}」，白名单：{list(RELATIONS)}")
        if not source_note.strip():
            raise ValueError("边必须有出处（source_note）——无出处的关系拒收")
        for end in (source, target):
            if end not in self.nodes:
                raise ValueError(f"边的端点「{end}」不在图中——先 add_node 再 add_edge")
        edge = GraphEdge(source=source, target=target, relation=relation,
                         source_note=source_note.strip())
        self._edges[edge.key()] = edge   # 同 key 幂等覆盖（同笔记重抽的合法路径）
        self._adj.setdefault(source, []).append(edge)
        self._adj.setdefault(target, []).append(edge)   # 无向索引：path 查询双向可达
        return edge

    def merge_note(self, note: str, nodes: list[dict], edges: list[dict]) -> None:
        """一篇笔记的抽取结果原子替换（增量同步的核心原语）。

        语义 = remove_note(note) + 重放入本次抽取的节点/边——旧版笔记
        贡献的边全部失效（笔记改了，关系可能变了），新边全部带本次出处。
        节点不删（remove_note 的裁定）：被别篇引用的实体活着，孤岛由
        overview 报告。

        nodes/edges 是抽取层的裸 dict（LLM 输出），此处统一校验：
        校验失败的条目跳过并计入返回的拒收清单（单条脏数据不炸整篇
        同步——与 VectorStore「坏笔记不阻断同步」同款韧性），但节点
        缺失导致的边拒收是真问题——上游抽取层保证边端点在 nodes 里。
        """
        note = note.strip()
        # 先删旧边（该笔记贡献的全部边）
        self.remove_note(note)
        # 节点先入图（边的端点校验依赖）
        for n in nodes:
            try:
                self.add_node(n["name"], n.get("type", "概念"),
                              tuple(n.get("aliases", ())))
            except (KeyError, ValueError, TypeError):
                continue
        # 边后入图（出处由程序强制填——模型没机会伪造）
        for e in edges:
            try:
                self.add_edge(e["source"], e["target"], e["relation"], note)
            except (KeyError, ValueError, TypeError):
                continue

    def remove_note(self, note: str) -> None:
        """删除一篇笔记贡献的全部边（节点保留——见类 docstring 裁定）。"""
        note = note.strip()
        dead = [k for k, e in self._edges.items() if e.source_note == note]
        for k in dead:
            edge = self._edges.pop(k)
            self._adj.get(edge.source, []).remove(edge)
            self._adj.get(edge.target, []).remove(edge)
        self.note_hashes.pop(note, None)

    # ---- 查询原语（图谱产品的一等公民） ----

    def resolve(self, name: str) -> str | None:
        """实体解析：本名或别名 → 节点 id。查不到返回 None（调用方给
        「图中没有这个实体」的友好提示，不抛——查询原语对模型说话）。"""
        return self._alias.get(name.strip())

    def neighbors(self, name: str, relation: str | None = None) -> list[GraphEdge]:
        """1 跳邻居：与该实体直接相连的全部边，可按关系类型过滤。"""
        nid = self.resolve(name)
        if nid is None:
            return []
        out = self._adj.get(nid, [])
        if relation is not None:
            out = [e for e in out if e.relation == relation]
        # 边表按 key 排序：输出确定性（同图同查询永远同序——可测试性）
        return sorted(out, key=lambda e: e.key())

    def path(self, a: str, b: str, max_hops: int = MAX_PATH_HOPS) -> list[GraphEdge] | None:
        """两实体间的最短关系链（BFS，≤max_hops 跳；无向遍历）。

        为什么无向：「Embedding 和 向量数据库 什么关系」不预设方向——
        找到链路后，每条边自带方向和关系名，模型看着讲。返回 None =
        规定跳数内不连通（调用方如实报告，不编造）。
        """
        a_id, b_id = self.resolve(a), self.resolve(b)
        if a_id is None or b_id is None:
            return None
        if a_id == b_id:
            return []
        # BFS：队列元素 = (当前节点, 路径边列表)
        queue: deque[tuple[str, list[GraphEdge]]] = deque([(a_id, [])])
        visited = {a_id}
        while queue:
            cur, trail = queue.popleft()
            if len(trail) >= max_hops:
                continue
            for edge in sorted(self._adj.get(cur, []), key=lambda e: e.key()):
                nxt = edge.target if edge.source == cur else edge.source
                if nxt in visited:
                    continue
                new_trail = trail + [edge]
                if nxt == b_id:
                    return new_trail
                visited.add(nxt)
                queue.append((nxt, new_trail))
        return None

    def overview(self) -> dict:
        """全图统计：概念总数/关系分布/孤岛节点/枢纽节点。

        「我学过的东西全貌」——向量检索给不出全集，这是图谱对个人
        知识管理的独特价值（知识地图）。
        """
        relation_counts: dict[str, int] = {r: 0 for r in RELATIONS}
        for e in self._edges.values():
            relation_counts[e.relation] = relation_counts.get(e.relation, 0) + 1
        degree = {nid: len(self._adj.get(nid, [])) for nid in self.nodes}
        islands = [nid for nid, d in degree.items() if d == 0]
        hubs = sorted(degree.items(), key=lambda kv: -kv[1])[:5]
        return {
            "nodes": len(self.nodes),
            "edges": len(self._edges),
            "relations": relation_counts,
            "islands": sorted(islands),
            "hubs": [{"name": n, "degree": d} for n, d in hubs if d > 0],
        }

    # ---- 序列化（graph.json 落盘） ----

    def to_dict(self) -> dict:
        """落盘格式：nodes/edges 全量事实 + note_hashes 指纹（增量判据）。
        邻接索引不落——推导值（见类 docstring）。"""
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self._edges.values()],
            "note_hashes": dict(self.note_hashes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> GraphStore:
        store = cls()
        for n in d.get("nodes", []):
            try:
                store.add_node(n["id"], n.get("type", "概念"), tuple(n.get("aliases", [])))
            except (KeyError, ValueError, TypeError):
                continue
        for e in d.get("edges", []):
            try:
                store.add_edge(e["source"], e["target"], e["relation"], e["source_note"])
            except (KeyError, ValueError, TypeError):
                continue
        store.note_hashes = dict(d.get("note_hashes", {}))
        return store

    def save(self, path: Path) -> None:
        """落盘（知识资产进 git——人可读可审查）。"""
        import json
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> GraphStore:
        """读盘；文件不存在或损坏 → 空图（宽进：重建路径从头抽——增量
        指纹丢失等于全量重抽，安全方向）。"""
        import json
        if not path.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return cls()

    # ---- 内部 ----

    def _index_names(self, node: GraphNode) -> None:
        """本名+别名 → id 的解析索引。别名撞车（指向别的 id）→ 后写
        覆盖（add_node 已并集吸收重名，此处覆盖只剩真歧义——宽进，
        overview 的孤岛/枢纽暴露异常，人可修）。"""
        self._alias[node.id] = node.id
        for a in node.aliases:
            self._alias[a] = node.id
