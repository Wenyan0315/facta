"""P1 审批持久化原语（ApprovalStore）单测。

不变量：
- 审批项落单一 JSON 快照（路径注入 tmp_path），读写即存盘：第二个实例同路径
  读到全量（重启存活是这套原语存在的全部意义）；
- 裁决按审批项 id 幂等：重复裁决同参 → 原裁决原样返回（状态/执行标记不动，
  不重复执行）；异参 → 明确拒绝（不许借重放偷换参数「追认」）；
- 编辑不落裁决：pending 项参数/目标变了 → 旧项 superseded 留档、新项 pending
  重新走审（版本链 supersedes/superseded_by 可追溯）；
- match_decided 只命中「已决且已执行」的同参项——未决/未执行/结果未知一律
  不自动套用（宁可重新走审，不可重复执行）；
- 读档损坏保守重建空档 + 告警，不炸启动。
"""

from facta.server.run_store import ApprovalStore


def _store(tmp_path) -> ApprovalStore:
    return ApprovalStore(tmp_path / "approvals.json")


def test_create_get_pending_for(tmp_path):
    store = _store(tmp_path)
    a = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"})
    b = store.create(task_id="t1", run_id="r2", tool="write_file", arguments={"path": "x"}, target="x.md")
    c = store.create(task_id="t2", run_id="r3", tool="run_command", arguments={"command": "pwd"})

    assert store.get(a.id) == a and store.get("nope") is None
    assert a.status == "pending" and a.approved is None and a.created_at
    assert [it.id for it in store.pending_for(run_id="r1")] == [a.id]
    assert [it.id for it in store.pending_for(task_id="t1")] == [a.id, b.id]
    assert [it.id for it in store.pending_for()] == [a.id, b.id, c.id]
    assert c.target == ""


def test_decide_plain_and_idempotent_repeat(tmp_path):
    # 幂等核心：重复裁决同参 → 原裁决原样返回，状态/执行标记不动（bug 修法）
    store = _store(tmp_path)
    item = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"})

    got, msg = store.decide(item.id, approved=True)
    assert msg == "" and got is not None
    assert got.status == "approved" and got.approved is True and got.decided_at

    again, msg2 = store.decide(item.id, approved=True)
    assert again is not None and again.id == item.id
    assert "批准" in msg2 and msg2.startswith("重复请求")
    assert again.status == "approved" and again.decided_at == got.decided_at   # 状态不变
    assert again.executed is False   # 不重复执行


def test_decide_mismatched_args_refused(tmp_path):
    # 已决项被换参/换目标「追认」→ 明确拒绝（原裁决不动）
    store = _store(tmp_path)
    item = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"})
    store.decide(item.id, approved=False)

    got, msg = store.decide(item.id, approved=True, arguments={"command": "rm -rf /"})
    assert got is None and msg == "参数或目标与已决审批不一致，已明确拒绝"
    got2, msg2 = store.decide(item.id, approved=True, target="other.md")
    assert got2 is None and msg2 == "参数或目标与已决审批不一致，已明确拒绝"

    cur = store.get(item.id)
    assert cur.status == "rejected" and cur.approved is False


def test_decide_edit_supersedes_and_creates_pending_version(tmp_path):
    # 编辑不落裁决：旧项 superseded 留档，新项 pending 重新走审（版本链可追溯）
    store = _store(tmp_path)
    old = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"})

    new, msg = store.decide(old.id, approved=True, arguments={"command": "ls -la"})
    assert new is not None and msg == "参数/目标已编辑，生成新的待审版本"
    assert new.id != old.id and new.status == "pending" and new.approved is None   # 不裁决
    assert new.supersedes == old.id and new.arguments == {"command": "ls -la"}
    assert new.task_id == old.task_id and new.run_id == old.run_id and new.tool == old.tool

    cur_old = store.get(old.id)
    assert cur_old.status == "superseded" and cur_old.superseded_by == new.id

    # target 编辑同理（None=未带沿用原值，带了且不同=编辑）
    newer, msg3 = store.decide(new.id, approved=False, target="x.md")
    assert newer is not None and newer.status == "pending" and newer.target == "x.md"
    assert newer.supersedes == new.id and store.get(new.id).superseded_by == newer.id


def test_decide_superseded_refused(tmp_path):
    store = _store(tmp_path)
    old = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"})
    store.decide(old.id, approved=True, arguments={"command": "ls -la"})   # 编辑出新版本

    got, msg = store.decide(old.id, approved=True)
    assert got is None and msg == "已被新版本取代"
    assert store.get(old.id).approved is None   # superseded 项不落裁决


def test_match_decided_only_hits_executed_decided(tmp_path):
    args = {"command": "ls"}
    store = _store(tmp_path)
    item = store.create(task_id="t1", run_id="r1", tool="run_command", arguments=args)
    assert store.match_decided("t1", "run_command", args) is None       # 未决不命中

    store.decide(item.id, approved=True)
    assert store.match_decided("t1", "run_command", args) is None       # 已决未执行不命中

    assert store.record_result(item.id, "ok") is True
    hit = store.match_decided("t1", "run_command", args)
    assert hit is not None and hit.id == item.id and hit.result == "ok"
    assert store.match_decided("t2", "run_command", args) is None                        # 归属不同
    assert store.match_decided("t1", "run_command", {"command": "la"}) is None           # 参数不同
    assert store.match_decided("t1", "run_command", args, target="x.md") is None         # 目标不同
    assert store.match_decided("t1", "write_file", args) is None                         # 工具不同


def test_record_result_and_mark_unknown(tmp_path):
    store = _store(tmp_path)
    item = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"})
    assert store.record_result("nope", "x") is False

    store.decide(item.id, approved=False)
    assert store.record_result(item.id, "exit 1") is True
    cur = store.get(item.id)
    assert cur.result == "exit 1" and cur.executed is True and cur.status == "rejected"
    assert store.match_decided("t1", "run_command", {"command": "ls"}) is not None   # rejected 也命中

    # 执行结果不明（如裁决后服务被杀）→ unknown_result，match_decided 不再命中
    assert store.mark_unknown(item.id) is True
    assert store.mark_unknown("nope") is False
    cur = store.get(item.id)
    assert cur.status == "unknown_result" and cur.approved is False   # 裁决记录仍留档
    assert store.match_decided("t1", "run_command", {"command": "ls"}) is None


def test_decide_unknown_id(tmp_path):
    store = _store(tmp_path)
    got, msg = store.decide("nope", approved=True)
    assert got is None and msg == "审批项不存在"


def test_survives_restart(tmp_path):
    # 重启存活：第二个实例同路径读到全量（含裁决/结果/版本链）
    path = tmp_path / "approvals.json"
    store = ApprovalStore(path)
    a = store.create(task_id="t1", run_id="r1", tool="run_command", arguments={"command": "ls"}, target=".")
    store.decide(a.id, approved=True)
    store.record_result(a.id, "ok")
    b = store.create(task_id="t1", run_id="r1", tool="write_file", arguments={"path": "x"})

    fresh = ApprovalStore(path)   # 重启
    assert [it.id for it in fresh.all()] == [a.id, b.id]
    got = fresh.get(a.id)
    assert got is not None and got.status == "approved" and got.approved is True
    assert got.result == "ok" and got.executed is True
    assert [it.id for it in fresh.pending_for(run_id="r1")] == [b.id]
    hit = fresh.match_decided("t1", "run_command", {"command": "ls"}, ".")
    assert hit is not None and hit.id == a.id


def test_corrupt_file_rebuilds_empty(tmp_path):
    # 读档损坏 → 保守重建空档 + 告警，不炸启动；之后照常读写
    path = tmp_path / "approvals.json"
    path.write_text("{ 半截 json", encoding="utf-8")

    store = ApprovalStore(path)
    assert store.all() == [] and store.get("x") is None
    item = store.create(task_id="t1", run_id="r1", tool="t", arguments={})
    assert store.get(item.id) is not None
