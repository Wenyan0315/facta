"""S7a 图谱抽取层：LLM 封闭抽取 + 增量同步管线。

与记忆固化管线（M6.4）同构的三道防线（防幻觉是抽取层的生命线）：
1. 封闭抽取：关系只用白名单（graph.RELATIONS 单一真值源，提示词与
   数据校验共用一份）；实体类型给建议清单但不封死（新概念要能进图）
2. 挂靠优先：抽取前把现有实体清单（含别名）传入提示词——「RAG」和
   「检索增强生成」归一到已有实体，不做平行节点（记忆固化把 learned
   全文当已知清单传入的同款手法）
3. 依据强制：只抽原文明确表达的关系，禁止推断/联想——每条边必须
   能在笔记里找到句子级依据

出处防线不在这层：merge_note 的签名带笔记名，程序侧强制填（模型
没机会伪造出处——见 graph.py）。

失败语义：解析失败/超时返回 (None, 错误串)，调用方（同步层）跳过
该篇不阻断其他篇——与 MCP 单台失败只警告同款韧性。

sync_graph（增量同步）：与向量侧 sync_notes 同款「内容指纹差集」
哲学——笔记没改不重抽（省 LLM 的钱），改了只重抽那篇（merge_note
原子替换），删了的笔记边清掉（节点保留）。假模型路径（llm=None）
不抽也不更新指纹——切真模型后自动补抽，教学组合零外部依赖。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from facta.core.llm import LLM
from facta.core.types import Message
from facta.knowledge.graph import RELATIONS, GraphStore
from facta.knowledge.loader import scan_notes
from facta.knowledge.sync import DELETE_GUARD_RATIO, GUARD_MIN_FILES, file_hash

logger = logging.getLogger(__name__)

_EXTRACT_TEMPLATE = """你是知识图谱的「档案员」。从下面这篇笔记中抽取实体和实体间的关系。

铁律（违反任何一条的条目必须丢弃）：
1. 关系类型只能用白名单：{relations}——白名单外的关系一律不抽
2. 只抽笔记原文【明确表达】的关系，禁止推断、联想、补全（「A 依赖 B」
   必须能在原文找到依据；原文只并列提到两个概念 ≠ 有关系）
3. 实体名优先从「已有实体清单」里选（含别名对照）——同一概念绝不建
   平行节点；清单外的全新概念才新建，名字用原文的原词，不要自创缩写
4. 一条关系都没找到就输出空 edges（宁缺毋滥，单概念笔记是合法的）
5. 每个实体给一个类型：概念/技术/政策/工具/语言/方法（拿不准填 概念）

已有实体清单（名字（别名）——优先挂靠）：
{existing}

笔记《{note}》全文：
{text}

只输出 JSON 对象（不要围栏不要解释），格式：
{{"nodes": [{{"name": "...", "type": "...", "aliases": ["..."]}}],
"edges": [{{"source": "...", "target": "...", "relation": "..."}}]}}"""


def _strip_fences(text: str) -> str:
    """剥 ```json 围栏（模型常见坏习惯，consolidate 同款容错）。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json")
    return stripped.strip()


def _parse_payload(text: str) -> dict | None:
    """解析抽取输出；坏 JSON 返回 None（调用方跳过该篇）。"""
    raw = _strip_fences(text)
    # 容错：模型偶尔在 JSON 前后带说明文字——抓第一个 { 到最后一个 }
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _format_existing(existing: list[tuple[str, tuple[str, ...]]]) -> str:
    """已有实体（name, aliases）渲染成清单文本；空清单给引导语。"""
    if not existing:
        return "（图还是空的，全部新建）"
    lines = []
    for name, aliases in existing:
        if aliases:
            lines.append(f"- {name}（别名：{'、'.join(aliases)}）")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def extract_note(
    llm: LLM,
    note: str,
    text: str,
    existing: list[tuple[str, tuple[str, ...]]],
) -> tuple[tuple[list[dict], list[dict]] | None, str]:
    """抽一篇笔记的实体/关系候选。

    返回 ((nodes, edges), "") 成功；(None, 错误串) 失败（调用方跳过）。
    输出只做解析，不做语义校验——白名单/端点/出处校验收口在
    GraphStore.merge_note（单一闸门，本层不重复设防）。
    """
    prompt = _EXTRACT_TEMPLATE.format(
        relations="/".join(RELATIONS),
        existing=_format_existing(existing),
        note=note,
        text=text,
    )
    try:
        reply = llm.generate([Message(role="user", content=prompt)])
    except Exception as exc:   # LLM 不可用/超时——同步层跳过该篇的信号
        return None, f"抽取调用失败：{exc}"
    payload = _parse_payload(reply.content or "")
    if payload is None:
        return None, "抽取输出不是合法 JSON"
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None, "抽取输出结构不对（nodes/edges 不是数组）"
    return (nodes, edges), ""


# ---------- 增量同步管线 ----------


@dataclass
class GraphSyncReport:
    """一次图谱同步的账本。"""

    extracted: int = 0    # 改动笔记 → 重抽（花 LLM 钱的唯一环节）
    unchanged: int = 0    # 指纹相同 → 零成本跳过
    removed: int = 0      # 文件消失 → 该篇边清除（节点保留）
    skipped: int = 0      # 无 LLM 通道（教学路径）→ 留待真模型补抽
    failed: int = 0       # 单篇抽取失败（不阻断其他篇）


def sync_graph(store: GraphStore, notes_dir: Path | str, llm: LLM | None) -> GraphSyncReport:
    """把图谱与笔记目录对齐：指纹差集款式（向量侧 sync_notes 同构）。

    删除安全阀复用向量侧同款阈值（GUARD_MIN_FILES/DELETE_GUARD_RATIO
    单一真值源）——图谱删除更温和（只删边不删节点，重抽可恢复），但
    「目录被误移动导致全库边消失」的环境事故防护同样需要。
    """
    report = GraphSyncReport()
    disk: dict[str, str] = {}
    for name, text in scan_notes(notes_dir):
        if text.strip():
            disk[name] = text

    # 删：图里有指纹、磁盘没有的笔记（改名/删除都走这里——改名=删旧+抽新）
    vanished = [n for n in store.note_hashes if n not in disk]
    if len(vanished) >= GUARD_MIN_FILES and (
        len(vanished) / max(len(store.note_hashes), 1) > DELETE_GUARD_RATIO
    ):
        raise RuntimeError(
            f"疑似笔记目录异常：{len(vanished)} 篇笔记同时消失"
            f"（占比 {len(vanished) / max(len(store.note_hashes), 1):.0%}），已中止图谱同步。"
        )
    for name in vanished:
        store.remove_note(name)
        report.removed += 1

    # 增/改：指纹不同才重抽；llm=None（教学路径）不抽也不记指纹
    for name, text in sorted(disk.items()):
        fp = file_hash(text)
        if store.note_hashes.get(name) == fp:
            report.unchanged += 1
            continue
        if llm is None:
            report.skipped += 1
            continue
        # 挂靠清单：现有全部实体（含别名）传入——封闭对齐
        existing = [(n.id, n.aliases) for n in store.nodes.values()]
        result, err = extract_note(llm, name, text, existing)
        if result is None:
            report.failed += 1
            logger.warning("图谱抽取失败，跳过《%s》：%s", name, err)
            continue
        store.merge_note(name, result[0], result[1])
        store.note_hashes[name] = fp
        report.extracted += 1

    return report
