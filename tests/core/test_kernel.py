"""Every kernel gate and outcome on the in-memory services (architecture.md 3)."""

import json

import pytest

from nx_mcp.core import catalogue, evidence, kernel
from tests.core import tools
from tests.core.fakes import FakeServices

SPECS = tools.specs()


@pytest.fixture
def svc(tmp_path):
    return FakeServices(tmp_path)


@pytest.fixture
def registered():
    for spec in SPECS.values():
        catalogue.register(spec)
    yield
    for name in SPECS:
        catalogue.unregister(name)


def run(svc, tool, operation_id=None, **args):
    return kernel.run(SPECS[tool], args, kernel.Context(svc, operation_id=operation_id))


# -- success ------------------------------------------------------------------


def test_read_tool_has_no_receipt_mark_or_changes(svc):
    svc.part.add("feature")
    ok = run(svc, "nx_get_count")
    assert ok["ok"] and ok["mutation_outcome"] == "not_applicable"
    assert ok["data"] == {"count": 1} and "changes" not in ok
    assert svc.mark_names == [] and not list((svc.root / ".nx-mcp" / "operations").glob("*"))
    assert ok["units"] == {"length": "mm", "angle": "deg"} and ok["frame"] == "work_part_absolute"


def test_model_commit_converts_units_marks_and_reports_changes(tmp_path):
    svc = FakeServices(tmp_path, units="in")
    ok = run(svc, "nx_create_block", size="25.4 mm", angle="3.141592653589793 rad")
    assert ok["mutation_outcome"] == "committed" and ok["units"]["length"] == "in"
    assert ok["data"]["size"] == pytest.approx(1.0)
    assert svc.mark_names == ["NX MCP: Create block"]
    assert {r["kind"] for r in ok["changes"]["created"]} == {"feature", "body"}
    assert ok["changes"]["references"] == "listed"  # invalidates="topology"
    assert ok["changes"]["stale_kinds"] == ["face", "edge"]
    assert ok["data"]["feature"]["id"].startswith("f:")
    assert ok["data"]["feature"]["anchor"].startswith("@a.prt:FEATURE(")
    assert svc.history[-1]["operation_id"] == ok["operation_id"]
    receipt = json.loads(
        (svc.root / ".nx-mcp" / "operations" / (ok["operation_id"] + ".json")).read_text()
    )
    assert receipt["state"] == "committed" and receipt["envelope"]["data"]["size"] == 1.0


def test_annotation_refresh_rows_join_modified(svc):
    spec = SPECS["nx_create_block"]
    assert spec.refresh_annotations  # model tool in a geometry domain
    svc.refreshed = [{"id": "a:1", "kind": "annotation"}]
    ok = run(svc, "nx_create_block", size=1)
    assert ok["changes"]["modified"] == [{"id": "a:1", "kind": "annotation"}]


# -- failures and outcomes ----------------------------------------------------------


def test_schema_failure_is_not_started_with_field(svc):
    bad = run(svc, "nx_create_block", size="30 deg")
    assert bad["ok"] is False and bad["mutation_outcome"] == "not_started"
    assert bad["error"]["category"] == "invalid_argument" and bad["error"]["field"] == "/size"
    assert svc.mark_names == []
    extra = run(svc, "nx_create_block", size=1, colour="red")
    assert extra["error"]["field"] == "/colour"


def test_untouched_handler_failure_deletes_mark_and_is_not_started(svc):
    bad = run(svc, "nx_create_block", size=-1)
    assert bad["mutation_outcome"] == "not_started" and bad["error"]["field"] == "/size"
    assert svc.undone == [] and svc.marks == {}
    assert bad["changes"] == {"references": "preserved"}


def test_touched_failure_rolls_back_and_invalidates_part(svc):
    keep = svc.part.add("feature", "KEEP")
    first = run(svc, "nx_get_count")  # nothing registered yet
    assert first["ok"]
    row = svc.references.row(keep, "feature")
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert (
        bad["mutation_outcome"] == "rolled_back" and bad["error"]["category"] == "geometry_failed"
    )
    assert bad["error"]["retry"] == "after_fix"
    assert svc.part.objects == [keep] and svc.marks == {}
    # Structure changed before undo -> every ref of the part is stale now.
    assert bad["changes"]["references"] == "part_invalidated"
    stale = svc.references
    with pytest.raises(Exception) as error:
        stale.resolve(row["id"], ("feature",))
    assert error.value.code == "NX_STALE_REFERENCE"
    assert error.value.details["current"]["name"] == "KEEP"  # anchor's unique match


def test_new_feature_in_error_is_update_failed_and_rolled_back(svc):
    bad = run(svc, "nx_create_block", size=1, error_feature=True)
    assert bad["error"]["code"] == "NX_UPDATE_FAILED"
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == []


def test_rollback_failure_is_partial(svc):
    svc.fail_undo = True
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "partial"
    assert bad["error"]["code"] == "NX_ROLLBACK_FAILED"
    assert bad["error"]["details"]["operation_error"]["code"] == "NX_BUILD_FAILED"


def test_python_exception_is_nx_error_with_type(svc):
    def broken(tx, *, size):
        tx.part.add("feature")
        raise ZeroDivisionError("boom")

    spec = SPECS["nx_create_block"]
    object.__setattr__(spec, "handler", broken)
    try:
        bad = run(svc, "nx_create_block", size=1)
    finally:
        object.__setattr__(spec, "handler", tools.nx_create_block)
    assert bad["error"]["code"] == "NX_ERROR" and "ZeroDivisionError" in bad["error"]["message"]
    assert bad["mutation_outcome"] == "rolled_back"


# -- receipts -------------------------------------------------------------------------


def test_same_operation_id_replays_and_conflicts(svc):
    ok = run(svc, "nx_create_block", operation_id="op_replay_1", size=2)
    again = run(svc, "nx_create_block", operation_id="op_replay_1", size=2)
    assert again["replayed"] is True and again["data"] == ok["data"]
    assert len(svc.part.Features) == 1
    clash = run(svc, "nx_create_block", operation_id="op_replay_1", size=3)
    assert clash["error"]["code"] == "NX_IDEMPOTENCY_CONFLICT"
    assert clash["error"]["category"] == "interrupted"
    assert clash["mutation_outcome"] == "not_started"


def test_bad_operation_id_is_invalid_argument(svc):
    bad = run(svc, "nx_create_block", operation_id="x", size=1)
    assert bad["error"]["field"] == "/operation_id" and bad["mutation_outcome"] == "not_started"


# -- evidence, requires, ownership ----------------------------------------------------


def test_evidence_gate_in_production(tmp_path, monkeypatch):
    svc = FakeServices(tmp_path, dev=False)
    svc.own(svc.part)
    monkeypatch.setattr(evidence, "_CACHE", {})
    unknown = run(svc, "nx_create_block", size=1)
    assert unknown["error"]["code"] == "NX_VERSION_UNSUPPORTED"
    assert unknown["mutation_outcome"] == "not_started"
    assert run(svc, "nx_get_count")["ok"]  # unknown build: read tools still run
    monkeypatch.setattr(evidence, "_CACHE", {"nx_get_count": {"v2406": {"variants": []}}})
    known = run(svc, "nx_create_block", size=1)
    assert known["error"]["code"] == "NX_VERSION_UNSUPPORTED"
    monkeypatch.setattr(
        evidence,
        "_CACHE",
        {"nx_create_block": {"v2406": {"variants": []}}, "nx_get_count": {"v2406": {}}},
    )
    assert run(svc, "nx_create_block", size=1)["ok"]


def test_requires_gate_names_the_tool_to_run(svc):
    svc.session.ActiveSketch = object()
    bad = run(svc, "nx_create_block", size=1)
    assert bad["error"]["code"] == "NX_SKETCH_ACTIVE" and "nx_finish_sketch" in bad["error"]["hint"]
    svc.session.ActiveSketch = None
    svc.part = None
    bad = run(svc, "nx_create_block", size=1)
    assert bad["error"]["code"] == "NX_NO_WORK_PART" and bad["mutation_outcome"] == "not_started"


def test_ownership_gate_in_production(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_CACHE", {"nx_create_block": {"v2406": {}}})
    svc = FakeServices(tmp_path, dev=False)
    bad = run(svc, "nx_create_block", size=1)
    assert bad["error"]["code"] == "NX_PART_NOT_GRANTED"
    assert bad["error"]["category"] == "engineer_control"
    svc.own(svc.part)
    assert run(svc, "nx_create_block", size=1)["ok"]


def test_change_to_a_foreign_part_forces_rollback(tmp_path, monkeypatch):
    from tests.core.fakes import FakePart

    monkeypatch.setattr(evidence, "_CACHE", {"nx_create_block": {"v2406": {}}})
    svc = FakeServices(tmp_path, dev=False)
    svc.own(svc.part)
    foreign = FakePart(tmp_path / "engineer.prt")
    svc.extra_parts.append(foreign)

    def sneaky(tx, *, size):
        foreign.add("feature")
        return {"feature": {}, "size": size}

    spec = SPECS["nx_create_block"]
    object.__setattr__(spec, "handler", sneaky)
    try:
        bad = run(svc, "nx_create_block", size=1)
    finally:
        object.__setattr__(spec, "handler", tools.nx_create_block)
    assert bad["error"]["code"] == "NX_PART_NOT_GRANTED"
    assert bad["mutation_outcome"] == "rolled_back" and foreign.objects == []


# -- paths and files ------------------------------------------------------------------


def test_path_gate(svc):
    outside = run(svc, "nx_export_note", path="../escape.txt")
    assert outside["error"]["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    reserved = run(svc, "nx_export_note", path=".nx-mcp/x.txt")
    assert reserved["error"]["code"] == "NX_PATH_RESERVED"
    wrong = run(svc, "nx_export_note", path="a.csv")
    assert wrong["error"]["code"] == "NX_UNSUPPORTED_FILE_TYPE"
    (svc.root / "there.txt").write_text("x")
    exists = run(svc, "nx_export_note", path="there.txt")
    assert exists["error"]["code"] == "NX_ALREADY_EXISTS" and exists["error"]["field"] == "/path"
    for bad in (outside, reserved, wrong, exists):
        assert bad["mutation_outcome"] == "not_started"


def test_file_staging_rolls_back_or_reports_partial(svc):
    ok = run(svc, "nx_export_note", path="out/a.txt")
    assert ok["ok"] and (svc.root / "out" / "a.txt").read_text() == "hello"
    assert ok["data"]["path"] == str((svc.root / "out" / "a.txt").resolve())
    bad = run(svc, "nx_export_note", path="out/b.txt", fail_after=True)
    assert bad["mutation_outcome"] == "rolled_back" and not (svc.root / "out" / "b.txt").exists()
    late = run(svc, "nx_export_note", path="out/c.txt", publish_then_fail=True)
    assert late["mutation_outcome"] == "partial"


# -- children ---------------------------------------------------------------------------


def test_batch_children_share_one_mark_and_resolve_placeholders(svc, registered):
    ok = run(svc, "nx_run_blocks", sizes=["1 in", 2], then_delete=True)
    assert ok["ok"], ok
    results = ok["data"]["results"]
    assert results[0]["data"]["size"] == pytest.approx(25.4)
    assert results[2]["tool"] == "nx_delete_first"
    assert svc.mark_names == ["NX MCP: Run blocks"]  # children add no marks
    assert len(svc.part.Features) == 1


def test_failing_child_rolls_back_the_whole_batch(svc, registered):
    svc.part.add("feature", "OLD")
    bad = run(svc, "nx_run_blocks", sizes=[1, -1])
    assert bad["mutation_outcome"] == "rolled_back"
    assert bad["error"]["details"]["child_index"] == 1
    assert bad["error"]["field"] == "/operations/1/arguments/size"
    assert [o.Name for o in svc.part.objects] == ["OLD"]


def test_batch_admission_and_cancel(svc, registered):
    spec = SPECS["nx_create_block"]
    object.__setattr__(spec, "batchable", False)
    try:
        bad = run(svc, "nx_run_blocks", sizes=[1])
    finally:
        object.__setattr__(spec, "batchable", True)
    assert bad["error"]["code"] == "NX_UNSUPPORTED_OPTION"
    assert bad["error"]["field"] == "/operations/0/tool"
    (svc.root / ".nx-mcp" / "operations" / "op_cancel_1.cancel").touch()
    cancelled = run(svc, "nx_run_blocks", operation_id="op_cancel_1", sizes=[1, 2])
    assert cancelled["error"]["code"] == "NX_CANCELLED"
    # Cancelled at the first checkpoint, before any child touched NX.
    assert cancelled["mutation_outcome"] == "not_started" and svc.part.objects == []


def test_cancel_between_children_rolls_back(svc, registered):
    def cancel_after_first(tx, *, size, **_):
        out = tools.nx_create_block(tx, size=size)
        (svc.root / ".nx-mcp" / "operations" / "op_cancel_2.cancel").touch()
        return out

    spec = SPECS["nx_create_block"]
    object.__setattr__(spec, "handler", cancel_after_first)
    try:
        bad = run(svc, "nx_run_blocks", operation_id="op_cancel_2", sizes=[1, 2])
    finally:
        object.__setattr__(spec, "handler", tools.nx_create_block)
    assert bad["error"]["code"] == "NX_CANCELLED" and bad["error"]["details"]["child_index"] == 1
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == []
    receipt = json.loads((svc.root / ".nx-mcp" / "operations" / "op_cancel_2.json").read_text())
    assert receipt["progress"]["completed"] == 1 and receipt["state"] == "failed"


def test_preview_always_undoes_and_invalidates(svc, registered):
    ok = run(svc, "nx_preview_blocks", sizes=[1, 2])
    assert ok["ok"] and ok["mutation_outcome"] == "previewed"
    assert svc.part.objects == [] and svc.marks == {}
    ref = ok["data"]["results"][0]["data"]["feature"]["id"]
    with pytest.raises(Exception) as error:
        svc.references.resolve(ref, ("feature",))
    assert error.value.code == "NX_STALE_REFERENCE"
