"""Every kernel gate and outcome on the in-memory services (architecture.md 3)."""

import dataclasses
import json
from pathlib import Path

import pytest

from nx_mcp.core import catalogue, evidence, kernel
from nx_mcp.core import tx as tx_module
from nx_mcp.core.errors import fail
from tests.core import tools
from tests.core.fakes import FakePart, FakeServices, Obj

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


def ref(svc, obj, kind):
    return svc.references.row(obj, kind)["id"]


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
    # invalidates="topology": only faces and edges NX no longer has go stale (none here)
    assert ok["changes"]["references"] == "preserved" and "stale_kinds" not in ok["changes"]
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
    svc.refreshers = lambda: {
        "annotation": lambda tx, objects: [{"id": "a:1", "kind": "annotation"}]
    }
    ok = run(svc, "nx_create_block", size=1)
    assert ok["changes"]["modified"] == [{"id": "a:1", "kind": "annotation"}]


def test_gate_13_runs_every_refresher_hook_under_the_calls_mark(svc):
    calls = []

    def tables(tx, objects):
        calls.append((tx.spec.name, objects, tx.mark is not None))
        return [{"id": "t:1", "kind": "drawing_table"}]

    svc.refreshers = lambda: {"drawing_table": tables}
    ok = run(svc, "nx_create_block", size=1)
    assert calls == [("nx_create_block", None, True)]
    assert ok["changes"]["modified"] == [{"id": "t:1", "kind": "drawing_table"}]

    def broken(tx, objects):
        fail("NX_STALE_REFERENCE", "a managed table lost its source")

    svc.refreshers = lambda: {"drawing_table": broken}
    bad = run(svc, "nx_create_block", size=2)
    assert bad["error"]["code"] == "NX_STALE_REFERENCE"
    assert bad["mutation_outcome"] == "rolled_back"


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


def test_checks_refusal_is_not_started_unless_something_changed(svc):
    spec = SPECS["nx_create_block"]

    def refused(tx, *, size):
        with tx.checks():
            _ = tx.session.ActiveSketch  # reading NX marks the call touched
            fail("NX_NAME_CONFLICT", "name taken")

    def misplaced(tx, *, size):
        tx.part.add("feature", "MADE")
        with tx.checks():
            fail("NX_NAME_CONFLICT", "name taken")

    try:
        object.__setattr__(spec, "handler", refused)
        early = run(svc, "nx_create_block", size=1)
        object.__setattr__(spec, "handler", misplaced)
        late = run(svc, "nx_create_block", size=1)
    finally:
        object.__setattr__(spec, "handler", tools.nx_create_block)
    assert early["mutation_outcome"] == "not_started" and svc.marks == {}
    assert late["mutation_outcome"] == "rolled_back" and svc.part.Features == []


def test_touched_failure_rolls_back_and_keeps_restored_references(svc):
    keep = svc.part.add("feature", "KEEP")
    body = svc.part.add("body", "")
    first = run(svc, "nx_get_count")  # nothing registered yet
    assert first["ok"]
    row = svc.references.row(keep, "feature")
    body_ref = ref(svc, body, "body")
    part_ref = row["part"]
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert (
        bad["mutation_outcome"] == "rolled_back" and bad["error"]["category"] == "geometry_failed"
    )
    assert bad["error"]["retry"] == "after_fix"
    assert svc.part.objects == [keep, body] and svc.marks == {}
    # NX undo restores the same objects: the part, body and feature refs keep working.
    assert bad["changes"]["references"] == "preserved"
    assert "stale_references" not in bad["error"]["details"]
    assert svc.references.resolve(row["id"], ("feature",)) is keep
    assert svc.references.resolve(body_ref, ("body",)) is body
    assert svc.references.resolve(part_ref, ("part",)) is svc.part
    again = svc.references.row(keep, "feature")
    assert again["id"] == row["id"] and again["part"] == part_ref  # no renumbering


def _undo_replacing(svc, monkeypatch, old, new):
    """An undo after which ``old`` came back as ``new`` (another tag)."""
    undo = svc.undo_to

    def undo_to(mark):
        undo(mark)
        objects = svc.part.objects
        objects[objects.index(old)] = new
        old.retagged = True

    monkeypatch.setattr(svc, "undo_to", undo_to)


def test_rollback_rebinds_a_ref_whose_object_came_back_under_a_new_tag(svc, monkeypatch):
    keep = svc.part.add("feature", "KEEP", jid="EXTRUDE(2)")
    row = svc.references.row(keep, "feature")
    twin = Obj("feature", "KEEP", svc.part, "EXTRUDE(2)")
    _undo_replacing(svc, monkeypatch, keep, twin)
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "rolled_back"
    assert bad["changes"]["references"] == "preserved"
    assert bad["error"]["details"]["rebound_references"] == 1
    # Same short ref, now on the restored object (its anchor's unique match).
    assert svc.references.resolve(row["id"], ("feature",)) is twin
    assert svc.references.row(twin, "feature")["id"] == row["id"]


def test_rollback_reports_refs_that_went_stale(svc, monkeypatch):
    keep = svc.part.add("feature", "KEEP", jid="EXTRUDE(2)")
    row = svc.references.row(keep, "feature")
    other = Obj("feature", "OTHER", svc.part, "EXTRUDE(9)")
    _undo_replacing(svc, monkeypatch, keep, other)
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "rolled_back"
    assert bad["changes"] == {"references": "part_invalidated", "stale_references": 1}
    details = bad["error"]["details"]
    assert details["references"] == "renumbered" and details["stale_references"] == 1
    assert details["stale_refs"] == [row["id"]]  # refs made inside the call are not counted
    assert "list the objects again" in bad["error"]["hint"]
    with pytest.raises(Exception) as error:
        svc.references.resolve(row["id"], ("feature",))
    assert error.value.code == "NX_STALE_REFERENCE"


def test_rollback_of_one_part_keeps_refs_of_other_parts(svc, tmp_path):
    other = FakePart(tmp_path / "b.prt")
    svc.extra_parts.append(other)
    feature = other.add("feature", "THERE")
    there = ref(svc, feature, "feature")
    other_part = svc.references.row(other, "part")["id"]
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "rolled_back"
    assert svc.references.resolve(there, ("feature",)) is feature
    assert svc.references.resolve(other_part, ("part",)) is other


def test_non_topology_failure_restored_by_undo_preserves_references(svc):
    keep = svc.part.add("feature", "KEEP")
    assert run(svc, "nx_get_count")["ok"]
    row = svc.references.row(keep, "feature")
    spec = dataclasses.replace(SPECS["nx_create_block"], invalidates="deleted")
    bad = kernel.run(
        spec, {"size": 1, "fail_with": "NX_CONSTRAINT_UNSATISFIED"}, kernel.Context(svc)
    )
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == [keep]
    # The undo restored the same objects: old refs resolve, the created ones are retired.
    assert bad["changes"]["references"] == "preserved"
    assert svc.references.resolve(row["id"], ("feature",)) is keep
    stale = [k for k in svc.registry._stale_ids if k != row["id"]]
    assert stale  # the rolled-back feature and body


def test_new_feature_in_error_is_update_failed_and_rolled_back(svc):
    bad = run(svc, "nx_create_block", size=1, error_feature=True)
    assert bad["error"]["code"] == "NX_UPDATE_FAILED"
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == []


def test_a_touched_part_is_read_back_like_the_work_part(svc, tmp_path):
    other = FakePart(tmp_path / "component.prt")
    svc.extra_parts.append(other)

    def in_component(tx, *, size, error_feature=False, **_):
        tx.touch_part(other)
        tx.touch_part(tx.part)  # the work part is read back anyway: ignored
        feature = other.add("feature", "LINK")
        if error_feature:
            feature.errors.append("link broken")
        return {"feature": tx.ref(feature), "size": size}

    ok = _with_handler(svc, in_component, size=1)
    assert ok["ok"] and [r["name"] for r in ok["changes"]["created"]] == ["LINK"], ok
    bad = _with_handler(svc, in_component, size=1, error_feature=True)
    assert bad["error"]["code"] == "NX_UPDATE_FAILED" and "link broken" in bad["error"]["message"]
    assert bad["mutation_outcome"] == "rolled_back" and [o.Name for o in other.objects] == ["LINK"]


def test_rollback_failure_is_partial(svc):
    svc.fail_undo = True
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "partial"
    assert bad["error"]["code"] == "NX_ROLLBACK_FAILED"
    assert bad["error"]["details"]["operation_error"]["code"] == "NX_BUILD_FAILED"


class NativeLicenseError(Exception):
    """NXOpen's license refusal (NX 2406: 948801, no token feature)."""

    ErrorCode = 948801


def _with_handler(svc, handler, **args):
    spec = SPECS["nx_create_block"]
    object.__setattr__(spec, "handler", handler)
    try:
        return run(svc, "nx_create_block", **args)
    finally:
        object.__setattr__(spec, "handler", tools.nx_create_block)


def test_native_license_failure_in_a_model_tool_rolls_back_cleanly(svc):
    def refused(tx, *, size):
        tx.part.add("feature")
        raise NativeLicenseError("License Error, the check out of a feature failed")

    bad = _with_handler(svc, refused, size=1)
    assert bad["error"]["code"] == "NX_LICENSE_UNAVAILABLE" and bad["error"]["nx_code"] == 948801
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == []
    assert not svc.marks  # the call's mark is gone after the undo


def test_license_failure_nx_undid_itself_is_rolled_back_not_rollback_failed(svc):
    # Live NX 2406 (a session in "protected" error recovery): NX undid the refused call
    # back to the call's mark and removed the mark; UndoToMark would fail with "Undo mark
    # is missing". The model is as before, so the call is rolled back with its own error.
    def refused(tx, *, size):
        tx.part.add("feature")
        svc.undo_to(tx.mark)
        svc.delete_mark(tx.mark)
        raise NativeLicenseError("License Error, the check out of a feature failed")

    keep = svc.part.add("feature")
    kept = svc.references.row(keep, "feature")["id"]
    svc.undone.clear()
    bad = _with_handler(svc, refused, size=1)
    assert bad["error"]["code"] == "NX_LICENSE_UNAVAILABLE"
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == [keep]
    assert bad["changes"]["references"] == "preserved"
    assert svc.references.resolve(kept, ("feature",)) is keep
    assert len(svc.undone) == 1  # only NX's own undo: the kernel had nothing left to undo


def test_a_missing_mark_with_a_changed_model_is_rollback_failed(svc):
    def dropped(tx, *, size):
        tx.part.add("feature")
        svc.delete_mark(tx.mark)  # the mark is gone but the change stays
        raise NativeLicenseError("License Error")

    bad = _with_handler(svc, dropped, size=1)
    assert bad["mutation_outcome"] == "partial" and bad["error"]["code"] == "NX_ROLLBACK_FAILED"
    assert bad["error"]["details"]["operation_error"]["code"] == "NX_LICENSE_UNAVAILABLE"


def test_python_exception_is_nx_error_with_type(svc, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")

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
    # Even in development the stack stays out of the envelope: receipt and trace log only.
    assert "traceback" not in bad["error"]["details"]
    log = svc.root / ".nx-mcp" / "logs" / "tracebacks.log"
    assert "ZeroDivisionError" in log.read_text(encoding="utf-8")
    assert "ZeroDivisionError" in svc.store.get(bad["operation_id"])["traceback"]


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


def test_a_call_in_a_new_part_skips_the_work_part_check(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_CACHE", {"nx_import_block": {"v2406": {}}})
    svc = FakeServices(tmp_path, dev=False)  # the engineer's part is the work part
    engineer = svc.part
    into_work = run(svc, "nx_import_block")
    assert into_work["error"]["code"] == "NX_PART_NOT_GRANTED"
    into_new = run(svc, "nx_import_block", target="new_part", new_part_path="new/imp.prt")
    assert into_new["ok"], into_new
    assert engineer.objects == [] and svc.owns(svc.part)


def test_new_part_must_name_a_new_prt_path():
    from nx_mcp.core.catalogue import CatalogueError, make_spec

    with pytest.raises(CatalogueError, match="new_part"):
        make_spec(
            tools.nx_create_block,
            effect="model",
            label="x",
            keywords=tools.KEYWORDS,
            domain="modeling",
            new_part="size",
        )


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


# -- receipts before paths, never-overwrite publish (core_fix) -------------------------


def test_repeating_a_committed_call_replays_before_the_path_gate(svc):
    first = run(svc, "nx_export_note", operation_id="op_note_once", path="out/n.txt")
    assert first["ok"] and (svc.root / "out" / "n.txt").exists()
    again = run(svc, "nx_export_note", operation_id="op_note_once", path="out/n.txt")
    assert again["ok"] and again["replayed"] is True and again["data"] == first["data"]
    fresh = run(svc, "nx_export_note", operation_id="op_note_twice", path="out/n.txt")
    assert fresh["error"]["code"] == "NX_ALREADY_EXISTS"
    clash = run(svc, "nx_export_note", operation_id="op_note_once", path="out/m.txt")
    assert clash["error"]["code"] == "NX_IDEMPOTENCY_CONFLICT"
    assert clash["mutation_outcome"] == "not_started"


def test_publish_never_overwrites_and_staging_is_removed(svc, monkeypatch):
    from nx_mcp.core import tx as core_tx

    staging = svc.root / ".nx-mcp" / "staging"
    ok = run(svc, "nx_export_note", path="out/a.txt")
    assert ok["ok"] and not staging.exists()
    bad = run(svc, "nx_export_note", path="out/b.txt", fail_after=True)
    assert bad["mutation_outcome"] == "rolled_back" and not staging.exists()

    # Another writer creates the target between the path gate and publish().
    real_link = core_tx.os.link

    def racing_link(src, dst):
        Path(dst).write_text("theirs", encoding="utf-8")
        return real_link(src, dst)

    monkeypatch.setattr(core_tx.os, "link", racing_link)
    raced = run(svc, "nx_export_note", path="out/c.txt")
    assert raced["error"]["code"] == "NX_ALREADY_EXISTS"
    assert (svc.root / "out" / "c.txt").read_text(encoding="utf-8") == "theirs"
    assert raced["mutation_outcome"] == "rolled_back" and not staging.exists()


def test_publish_copies_exclusively_where_hard_links_fail(svc, monkeypatch):
    from nx_mcp.core import tx as core_tx

    def no_links(src, dst):
        raise OSError("hard links are not supported")

    monkeypatch.setattr(core_tx.os, "link", no_links)
    ok = run(svc, "nx_export_note", path="out/d.txt")
    assert ok["ok"] and (svc.root / "out" / "d.txt").read_text(encoding="utf-8") == "hello"
    assert not (svc.root / ".nx-mcp" / "staging").exists()


def test_replace_path_mode_replaces_an_existing_file_only_when_asked(svc):
    target = svc.root / "out" / "r.txt"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    kept = run(svc, "nx_export_text", path="out/r.txt")
    assert kept["error"]["code"] == "NX_ALREADY_EXISTS"
    assert target.read_text(encoding="utf-8") == "old"
    ok = run(svc, "nx_export_text", path="out/r.txt", replace=True)
    assert ok["ok"] and target.read_text(encoding="utf-8") == "new"
    assert not (svc.root / ".nx-mcp" / "staging").exists()
    fresh = run(svc, "nx_export_text", path="out/s.txt", replace=True)
    assert fresh["ok"] and (svc.root / "out" / "s.txt").read_text(encoding="utf-8") == "new"
    (svc.root / "out" / "d.txt").mkdir()
    folder = run(svc, "nx_export_text", path="out/d.txt", replace=True)
    assert folder["error"]["code"] == "NX_WRONG_KIND" and folder["error"]["field"] == "/path"
    assert folder["mutation_outcome"] == "not_started"
    outside = run(svc, "nx_export_text", path="../x.txt", replace=True)
    assert outside["error"]["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    wrong = run(svc, "nx_export_text", path="out/r.png", replace=True)
    assert wrong["error"]["code"] == "NX_UNSUPPORTED_FILE_TYPE"


# -- session license memory ---------------------------------------------------------


def test_license_failure_of_a_tested_tool_is_remembered_until_restart(svc, monkeypatch):
    monkeypatch.setattr(evidence, "_CACHE", {"nx_create_block": {"v2406": {}}})
    first = run(svc, "nx_create_block", size=1, fail_with="NX_LICENSE_UNAVAILABLE")
    assert first["error"]["code"] == "NX_LICENSE_UNAVAILABLE"
    assert first["mutation_outcome"] == "rolled_back"
    [entry] = evidence.licenses()
    assert entry["tool"] == "nx_create_block" and entry["variant"] is None and entry["since"]
    again = run(svc, "nx_create_block", size=1)
    assert again["error"]["code"] == "NX_LICENSE_UNAVAILABLE"
    assert again["mutation_outcome"] == "not_started"
    assert f"remembered from {entry['since']}" in again["error"]["message"]
    assert again["error"]["details"]["remembered_from"] == entry["since"]
    assert len(evidence.licenses()) == 1
    evidence.forget_licenses()  # an NX restart
    assert run(svc, "nx_create_block", size=1)["ok"]


def test_license_failure_of_an_untested_tool_is_not_remembered(svc, monkeypatch):
    monkeypatch.setattr(evidence, "_CACHE", {})  # development mode runs untested tools
    run(svc, "nx_create_block", size=1, fail_with="NX_LICENSE_UNAVAILABLE")
    assert evidence.licenses() == []
    assert run(svc, "nx_create_block", size=1)["ok"]


# -- sheet units -------------------------------------------------------------------


def test_sheet_quantities_use_the_named_sheets_units(svc):
    inch = svc.add_sheet("in")
    ok = run(
        svc,
        "nx_create_label",
        origin=[4, 3],
        height="12.7 mm",
        sheet=ref(svc, inch, "drawing_sheet"),
    )
    assert ok["ok"], ok
    assert ok["data"]["height"] == pytest.approx(0.5)  # converted to the sheet's inches
    assert ok["data"]["origin"] == [4.0, 3.0]  # SheetPoint: plain numbers in sheet units
    assert ok["data"]["units"] == {"length": "mm", "angle": "deg", "sheet_length": "in"}
    assert ok["units"]["sheet_length"] == "in" and ok["units"]["length"] == "mm"
    plain = run(
        svc, "nx_create_label", origin=[1, 1], height=0.25, sheet=ref(svc, inch, "drawing_sheet")
    )
    assert plain["data"]["height"] == 0.25  # a plain number is already in sheet units


def test_a_view_names_its_sheet_and_the_current_sheet_is_the_fallback(tmp_path):
    svc = FakeServices(tmp_path, units="in")
    mm_sheet = svc.add_sheet("mm", current=True)
    inch_sheet = svc.add_sheet("in")
    view = svc.add_view(inch_sheet)
    by_view = run(
        svc, "nx_create_label", origin=[0, 0], height="1 in", view=ref(svc, view, "drawing_view")
    )
    assert by_view["data"]["height"] == 1.0 and by_view["data"]["units"]["sheet_length"] == "in"
    current = run(svc, "nx_create_label", origin=[0, 0], height="1 in")
    assert current["data"]["height"] == pytest.approx(25.4)  # the current mm sheet
    assert current["data"]["sheet"] == mm_sheet.Name
    svc.current = None  # no sheet at all: part units (inches here)
    none = run(svc, "nx_create_label", origin=[0, 0], height="25.4 mm")
    assert none["data"]["height"] == pytest.approx(1.0)
    assert none["data"]["units"]["sheet_length"] == "in" and none["data"]["sheet"] is None


def test_a_new_sheet_uses_its_units_argument_and_stale_refs_are_left_to_the_handler(svc):
    made = run(svc, "nx_create_sheet", width="11 in", units="in")
    assert made["data"]["width"] == pytest.approx(11.0) and made["units"]["sheet_length"] == "in"
    metric = run(svc, "nx_create_sheet", width="11 in")
    assert metric["data"]["width"] == pytest.approx(279.4)
    # Gate 9 never fails on an unresolvable reference; the handler reports it.
    stale = run(svc, "nx_create_label", origin=[0, 0], sheet="sh:999999")
    assert stale["ok"] and stale["data"]["sheet"] is None


def test_tx_given_tells_a_passed_argument_from_its_default(svc):
    seen = []
    block = SPECS["nx_create_block"]

    def handler(tx, **args):
        seen.append((tx.given("size"), tx.given("angle"), tx.given("name")))
        return block.handler(tx, **args)

    spec = dataclasses.replace(block, handler=handler)
    assert kernel.run(spec, {"size": 1}, kernel.Context(svc))["ok"]
    assert kernel.run(spec, {"size": 1, "angle": 0}, kernel.Context(svc))["ok"]
    assert seen == [(True, False, False), (True, True, False)]  # angle=0 equals the default
    assert not tx_module.Tx(svc, block, operation_id=None).given("size")  # arguments unknown


def test_non_sheet_tools_do_not_get_sheet_units(svc):
    ok = run(svc, "nx_create_block", size=1)
    assert "sheet_length" not in ok["units"] and not SPECS["nx_create_block"].sheet


def test_panel_summary_states_sheet_units():
    from nx_mcp.core import summary

    spec = SPECS["nx_create_label"]
    line = summary.render(spec, {"origin": [1, 2], "height": 0.2}, units={"sheet_length": "in"})
    assert line == "Label 0.2 in at (1, 2)"


# -- view rollback and restore_id ------------------------------------------------------


def test_view_tool_returns_restore_id_for_what_it_kept(svc):
    body = svc.part.add("body", "B")
    ok = run(svc, "nx_set_look", objects=[ref(svc, body, "body")])
    assert ok["ok"] and ok["mutation_outcome"] == "committed"
    snapshot = svc.snapshots[ok["restore_id"]]
    assert snapshot["camera"] == {"scale": 1.0}
    assert snapshot["records"] == [(body, {"blanked": False, "color": 7})]
    assert svc.mark_names == []  # view tools set no undo mark


def test_failed_view_tool_restores_display_camera_and_highlights(svc):
    body = svc.part.add("body", "B")
    other = svc.part.add("body", "C")
    svc.highlighted = [other]
    bad = run(svc, "nx_set_look", objects=[ref(svc, body, "body")], fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "rolled_back" and bad["error"]["code"] == "NX_BUILD_FAILED"
    assert (body.blanked, body.color) == (False, 7)
    assert svc.camera == {"scale": 1.0} and svc.highlighted == [other]
    assert "restore_id" not in bad and svc.snapshots == {}


def test_view_rollback_failure_is_restore_failed_partial(svc):
    body = svc.part.add("body", "B")
    svc.fail_restore = True
    bad = run(svc, "nx_set_look", objects=[ref(svc, body, "body")], fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "partial"
    assert bad["error"]["code"] == "NX_RESTORE_FAILED"
    assert bad["error"]["category"] == "rollback_failed"
    assert bad["error"]["details"]["operation_error"]["code"] == "NX_BUILD_FAILED"


def test_view_tool_that_kept_nothing_reports_unknown(svc):
    body = svc.part.add("body", "B")
    bad = run(
        svc,
        "nx_set_look",
        objects=[ref(svc, body, "body")],
        fail_with="NX_BUILD_FAILED",
        keep=False,
    )
    assert bad["mutation_outcome"] == "unknown"


def test_untouched_view_failure_is_not_started(svc):
    svc.fail_camera = True
    body = svc.part.add("body", "B")
    bad = run(svc, "nx_set_look", objects=[ref(svc, body, "body")])
    assert bad["mutation_outcome"] == "not_started"


def test_restorable_needs_a_view_effect():
    from nx_mcp.core.catalogue import CatalogueError, make_spec

    with pytest.raises(CatalogueError, match="restorable requires effect 'view'"):
        make_spec(
            tools.nx_create_block,
            effect="model",
            label="Block",
            keywords=tools.KEYWORDS,
            domain="modeling",
            restorable=True,
            example={"size": 1},
        )


# -- part ownership ---------------------------------------------------------------------


@pytest.fixture
def production(tmp_path, monkeypatch):
    names = ("nx_create_block", "nx_save_other", "nx_create_steps")
    monkeypatch.setattr(evidence, "_CACHE", {n: {"v2406": {}} for n in names})
    return FakeServices(tmp_path, dev=False)


def test_not_granted_error_tells_the_agent_to_ask_the_engineer(production):
    bad = run(production, "nx_create_block", size=1)
    error = bad["error"]
    assert error["code"] == "NX_PART_NOT_GRANTED" and error["category"] == "engineer_control"
    assert error["retry"] == "ask_engineer" and bad["mutation_outcome"] == "not_started"
    assert "ask them in chat" in error["hint"] and "Allow agent to change a" in error["hint"]
    assert error["details"]["part"] == "a.prt"


def test_file_tools_check_the_part_they_save(production, tmp_path):
    engineer = FakePart(tmp_path / "engineer.prt")
    engineer.IsModified = True
    production.extra_parts.append(engineer)
    part_ref = ref(production, engineer, "part")
    bad = run(production, "nx_save_other", part=part_ref)
    assert bad["error"]["code"] == "NX_PART_NOT_GRANTED" and bad["error"]["field"] == "/part"
    assert engineer.IsModified  # nothing was saved
    production.own(engineer)
    assert run(production, "nx_save_other", part=part_ref)["ok"]


def test_development_mode_never_gates(svc, tmp_path):
    engineer = FakePart(tmp_path / "engineer.prt")
    svc.extra_parts.append(engineer)
    assert run(svc, "nx_save_other", part=ref(svc, engineer, "part"))["ok"]
    assert run(svc, "nx_create_block", size=1)["ok"]


# -- long operations -----------------------------------------------------------------------


def test_checkpoints_write_progress(svc):
    ok = run(svc, "nx_create_steps", operation_id="op_steps_1", steps=3)
    assert ok["ok"]
    receipt = json.loads((svc.root / ".nx-mcp" / "operations" / "op_steps_1.json").read_text())
    assert receipt["progress"]["completed"] == 3 and receipt["progress"]["total"] == 3


def test_budget_overrun_is_cancelled_and_rolls_back(svc, monkeypatch):
    monkeypatch.setitem(tx_module.COST_BUDGET_SECONDS, "light", -1.0)
    bad = run(svc, "nx_create_steps", steps=3)
    error = bad["error"]
    assert error["code"] == "NX_CANCELLED" and error["category"] == "interrupted"
    assert error["details"]["reason"] == "budget" and error["details"]["budget_seconds"] == -1.0
    assert "Split the work" in error["hint"]
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == []


def test_budget_defaults_follow_the_cost_classes():
    assert tx_module.COST_BUDGET_SECONDS == {
        "light": 30.0,
        "heavy": 300.0,
        "long": 1800.0,
    }
    assert tx_module.DEFAULT_BUDGET_SECONDS == 300.0


# -- undo, re-run and redo ------------------------------------------------------------


def call(svc, name, operation_id, **args):
    """A catalogue tool (nx_undo_operation, nx_redo_operation) on the fake services."""
    spec = catalogue.load()[name]
    return kernel.run(spec, args, kernel.Context(svc, operation_id=operation_id))


def blocks(svc):
    return sum(1 for o in svc.part.objects if o.kind == "feature")


def test_a_reverted_operation_runs_again_under_its_id(svc, registered):
    first = run(svc, "nx_create_block", operation_id="op_block_one", size=2)
    assert first["ok"] and blocks(svc) == 1
    undo = call(svc, "nx_undo_operation", "op_undo_one")
    assert undo["ok"] and blocks(svc) == 0, undo
    assert undo["data"]["redo"] == [
        {"tool": "nx_create_block", "arguments": {"size": 2}, "complete": True}
    ]
    assert undo["data"]["redo_depth"] == 1
    again = run(svc, "nx_create_block", operation_id="op_block_one", size=2)
    assert again["ok"] and "replayed" not in again and blocks(svc) == 1, again
    assert again["data"]["feature"]["id"] != first["data"]["feature"]["id"]
    assert "reverted by op_undo_one" in again["warnings"][-1]["message"]
    record = svc.store.get("op_block_one")
    assert "reverted_by" not in record and record["previous"]["reverted_by"] == "op_undo_one"
    assert record["previous"]["created"] == [r["id"] for r in first["changes"]["created"]]
    # The new run is committed: repeating it now replays it.
    assert run(svc, "nx_create_block", operation_id="op_block_one", size=2)["replayed"]
    assert blocks(svc) == 1
    # New work ended the redo list.
    nothing = call(svc, "nx_redo_operation", "op_redo_none")
    assert nothing["error"]["code"] == "NX_NOT_FOUND"
    assert nothing["mutation_outcome"] == "not_started"


def test_redo_repeats_undone_calls_in_order(svc, registered):
    run(svc, "nx_create_block", operation_id="op_block_one", size=2)
    run(svc, "nx_create_block", operation_id="op_block_two", size=3)
    assert call(svc, "nx_undo_operation", "op_undo_last")["ok"]
    assert call(svc, "nx_undo_operation", "op_undo_first")["ok"] and blocks(svc) == 0
    too_many = call(svc, "nx_redo_operation", "op_redo_many", steps=3)
    assert too_many["error"]["field"] == "/steps" and too_many["mutation_outcome"] == "not_started"
    redo = call(svc, "nx_redo_operation", "op_redo_one")
    assert redo["ok"] and redo["data"]["redone"] == ["op_block_one"], redo
    assert redo["data"]["redo_depth"] == 1 and redo["data"]["undo_depth"] == 1
    assert redo["data"]["results"][0]["data"]["size"] == 2 and blocks(svc) == 1
    rest = call(svc, "nx_redo_operation", "op_redo_two")
    assert rest["ok"] and rest["data"]["redone"] == ["op_block_two"] and blocks(svc) == 2
    assert [h["operation_id"] for h in svc.undo_history()] == ["op_block_one", "op_block_two"]
    # Each redone call is an operation of its own: undo takes back only the last one.
    assert call(svc, "nx_undo_operation", "op_undo_again")["ok"] and blocks(svc) == 1
    assert call(svc, "nx_redo_operation", "op_redo_three")["ok"] and blocks(svc) == 2
    chain = svc.store.get("op_block_two")["previous"]
    assert chain["reverted_by"] == "op_undo_again"
    assert chain["earlier"][0]["reverted_by"] == "op_undo_last"


def test_a_failing_redo_step_undoes_the_steps_redone(svc, registered):
    run(svc, "nx_create_block", operation_id="op_block_one", size=2)
    run(svc, "nx_create_block", operation_id="op_block_two", size=3)
    assert call(svc, "nx_undo_operation", "op_undo_both", steps=2)["ok"] and blocks(svc) == 0
    record = svc.store.get("op_block_two")
    record["arguments"] = {"size": 3, "fail_with": "NX_UPDATE_FAILED"}  # fails when repeated
    record["fingerprint"] = __import__("nx_mcp.core.receipts").core.receipts.fingerprint(
        "nx_create_block", record["arguments"]
    )
    svc.store.put(record)
    failed = call(svc, "nx_redo_operation", "op_redo_both", steps=2)
    assert not failed["ok"] and failed["mutation_outcome"] == "rolled_back", failed
    assert failed["error"]["code"] == "NX_UPDATE_FAILED"
    assert failed["error"]["details"]["redone_before_rollback"] == 1
    assert blocks(svc) == 0 and svc.undo_history() == []
    assert [s["operation_id"] for s in svc.redo_steps()] == ["op_block_one", "op_block_two"]
    assert svc.store.get("op_block_one")["reverted_by"] == "op_redo_both"
