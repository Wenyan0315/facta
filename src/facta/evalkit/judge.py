"""LLM-as-judge 裁判输出解析（evalkit 内核）。

裁判也是模型——输出不可信是常态：先试整段 JSON，再正则抠 {..} 块；
都失败返回 None（调用方按「裁判失灵」处理，绝不猜分）。
"""

from __future__ import annotations

import json
import re


def parse_judge_json(text: str) -> dict | None:
    """解析裁判输出为 {"score": int, ...}；结构不符返回 None。"""
    text = (text or "").strip()
    match = re.search(r"\{[^{}]*\}", text)   # 抠出第一个 {..} 块（无则 match 为 None）
    for candidate in (text, match.group() if match else ""):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and isinstance(data.get("score"), int):
                return data
        except json.JSONDecodeError:
            continue
    return None
