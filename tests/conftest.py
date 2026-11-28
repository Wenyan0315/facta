"""测试隔离总闸：data/tasks/（含 M5 的 runs/ Run 明细落盘）一律进 tmp_path。

此前只有部分用例文件各自 monkeypatch `facta.orchestrator.task.TASKS_DIR`，
漏patch的用例会把任务快照/Run 元快照写进仓库真目录（RunLog 让每个 Run
创建都落盘，泄漏面从「偶尔写任务」扩大到「每跑必写」）。集中在这里：
autouse + tmp_path，一把钥匙全测生效；个别用例自己的 patch 照旧可覆盖
（同为 tmp 路径，殊途同归）。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("facta.orchestrator.task.TASKS_DIR", tmp_path / "tasks")
