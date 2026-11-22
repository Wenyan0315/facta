"""个人 AI Agent 助手。

阶段一：学习助手（聊天答疑 / 论文推送 / 知识库 / 知识图谱）。
阶段二：个人 coding agent。

版本号单一真值源（ADR 118 §6）：pyproject 用 `dynamic = ["version"]` 取这里的
`__version__`，`__main__.VERSION` 也 import 自这里——三处一致靠结构而非人肉纪律。
本模块**保持零重导入**：setuptools 的 attr 读取会 import 本模块，拖进 openai /
chromadb 等重依赖会让打包与 `/api/status` 变慢甚至因缺可选依赖而失败。
"""

__version__ = "2.0.0"
