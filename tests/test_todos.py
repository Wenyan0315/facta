"""个人待办验收：TodoStore 存取 / 工具三件 / 并发与幂等（2026-09-17）。

语义裁定（014）：任务=个人待办——UI 创建/勾销，agent 可读写；
跨会话资产，独立于 session（不随归档走）。
"""

from agent.memory.todos import TodoStore
from agent.tools.registry import ToolRegistry
from agent.tools.todo import register_todo_tools


def _store(tmp_path) -> TodoStore:
    return TodoStore(tmp_path / "todos.json")


def test_add_assigns_incrementing_ids(tmp_path):
    store = _store(tmp_path)
    t1 = store.add("查阳澄湖天气")
    t2 = store.add("订大闸蟹")
    assert (t1.id, t2.id) == (1, 2)   # id 自增（勾销要引用，对模型友好）
    assert t1.done is False and t1.created   # 时间戳程序加


def test_complete_marks_done_and_survives_reload(tmp_path):
    store = _store(tmp_path)
    t = store.add("做完了的事")
    assert store.complete(t.id).done is True
    assert store.complete(t.id).done_at == store.complete(t.id).done_at   # 重复勾销幂等，不覆盖首次时间

    reloaded = _store(tmp_path)   # 新实例 = 从盘上重读（持久化验证）
    done = reloaded.list()[0]
    assert done.done and done.done_at


def test_complete_unknown_id_returns_none(tmp_path):
    assert _store(tmp_path).complete(99) is None


def test_delete_removes_and_keeps_id_gaps(tmp_path):
    store = _store(tmp_path)
    store.add("第一条")
    store.add("要删的")
    store.add("第三条")

    deleted = store.delete(2)
    assert deleted.text == "要删的"

    remaining = store.list()
    assert [t.id for t in remaining] == [1, 3]   # id 空洞不复用（引用键防悬垂）
    next_one = store.add("新的")
    assert next_one.id == 4                       # max+1 继续，不回填 2
    assert store.delete(99) is None               # 删不存在的返回 None


def test_update_text_keeps_status(tmp_path):
    store = _store(tmp_path)
    t = store.add("原文本")
    store.complete(t.id)

    updated = store.update_text(t.id, "改后的文本")
    assert updated.text == "改后的文本"
    assert updated.done is True                   # 改文本不动完成状态
    assert _store(tmp_path).list()[0].text == "改后的文本"   # 落盘验证
    assert store.update_text(99, "x") is None


def test_list_filters_pending(tmp_path):
    store = _store(tmp_path)
    store.add("没做的")
    done_one = store.add("做了的")
    store.complete(done_one.id)

    pending = store.list(only_pending=True)
    assert [t.text for t in pending] == ["没做的"]        # 完成项被滤掉
    assert [t.text for t in store.list()] == ["没做的", "做了的"]   # 全量含完成


def test_corrupt_file_treated_as_empty(tmp_path):
    (tmp_path / "todos.json").write_text("{半截", encoding="utf-8")
    assert _store(tmp_path).list() == []   # 损坏当空仓，不炸入口


# ---------- 工具三件（agent 视角） ----------

def test_tools_registered_and_usable(tmp_path):
    store = _store(tmp_path)
    registry = ToolRegistry()
    register_todo_tools(registry, store)
    names = registry.names()
    assert {"add_todo", "list_todos", "complete_todo"} <= set(names)

    assert "#1" in registry.execute("add_todo", '{"text": "9/20 查天气"}')
    listing = registry.execute("list_todos", "{}")
    assert "9/20 查天气" in listing and "○" in listing
    assert "已勾销 #1" in registry.execute("complete_todo", '{"todo_id": 1}')
    assert "不exist".replace("exist", "存在") in registry.execute("complete_todo", '{"todo_id": 9}')
    # 修改与删除（2026-09-17 补）
    assert "已修改 #1" in registry.execute("update_todo", '{"todo_id": 1, "text": "9/21 查天气"}')
    assert "已删除 #1" in registry.execute("delete_todo", '{"todo_id": 1}')
    assert "（无待办）" in registry.execute("list_todos", "{}")


def test_boolean_param_passes_validation(tmp_path):
    # 回归（真实事故：模型调 list_todos 传 only_pending:true 被「应为数字」误拒）
    # 根因：registry 校验器 and/or 优先级漏括号——bool 值一律命中 number 分支。
    # 用事故现场的真实工具+真实参数钉死。
    store = _store(tmp_path)
    registry = ToolRegistry()
    register_todo_tools(registry, store)
    store.add("没做的")

    out = registry.execute("list_todos", '{"only_pending": true}')
    assert "没做的" in out and "错误" not in out   # bool 放行，正常列出
    out_false = registry.execute("list_todos", '{"only_pending": false}')
    assert "错误" not in out_false


def test_agent_and_ui_share_one_store(tmp_path):
    # 工具（agent）与 API 共用同一实例——UI 添加的，agent 能看到；反之亦然
    store = _store(tmp_path)
    store.add("UI 加的")
    registry = ToolRegistry()
    register_todo_tools(registry, store)
    assert "UI 加的" in registry.execute("list_todos", "{}")
