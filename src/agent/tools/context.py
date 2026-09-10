"""工具上下文：工具运行所需的全部依赖，打包成一个对象（P1-2/P1-3）。

治两个老毛病：
  依赖发散 —— register_builtin(registry, kb, llm, history) 每加一个工具
    依赖就膨胀一个参数，签名永不稳定；收进 ctx 后签名固定为 (registry, ctx)。
  路径写死 —— builtin.py 的 NOTES_DIR 和 loader.py 的默认参数各藏一份
    "data/notes"，两个真值源迟早打架；现在路径唯一真值源是 agent/paths.py，
    __main__（组装层）import 后装进 ctx 流下去。

进 ctx 的准入标准：工具运行时需要、但工具自己无权决定的东西。
kb / llm / history / notes_dir 合格；MEMORY_PATH 没有任何工具用
（只有 __main__ 自己 load/save_session），所以不进 ctx。

依赖方向：本文件 import knowledge 层——tools 在 knowledge 之上，
向下依赖合法；但 ToolContext 绝不能放 core/types.py，那会让最底层
反向认识上层（P1-1 刚矫正过的病）。

命名澄清：这里的 Context 与 RAG 里检索出来的 context（资料文本）
无关——一个是依赖包（工牌），一个是内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.core.llm import LLM
from agent.core.types import Message
from agent.knowledge.knowledge_base import KnowledgeBase


@dataclass
class ToolContext:
    """工具的依赖包：组装层（__main__）构造，register_builtin 消费。

    kb / llm / history 允许 None：缺席 = 相应工具不上菜单（条件注册，
    语义沿用旧签名）。notes_dir 不给默认值——默认值就是第二个真值源，
    路径必须由组装层显式给出，绝不藏在类型定义里。

    ⚠ List identity trap：history 必须传 session.messages 对象本身，
    绝不 .copy()——run_chat 在原列表上原地 append，工具闭包抓的是同一
    个对象才能实时看到全部历史；传副本 = 工具安静变瞎，不报错。
    """

    notes_dir: Path
    kb: KnowledgeBase | None = None
    llm: LLM | None = None
    history: list[Message] | None = None
