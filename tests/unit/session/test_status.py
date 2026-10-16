"""nx_get_status, nx_get_operation and nx_cancel_operation (domains/session/status.py)."""

from unittest.mock import AsyncMock

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.core.receipts import Receipts
from nx_mcp.domains.session import status as st
from nx_mcp.sidecar.proxy import CatalogueProxy
from nx_mcp.workspace import Workspace
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.unit.parts_files.support import side


def second_part(nx):
    """A second loaded part the agent owns (the work and display parts stay)."""
    from pathlib import Path

    from tests.kit.objects import Part

    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    Part(nx.session, Path(services(nx).root) / "second.prt")
    parts.Work, parts.Display = work, display


def panel_status(running=None, operation_id=None):
    return {
        "mode": "auto",
        "engineer_busy": False,
        "waiting_for_engineer": 0,
        "activity": "running" if running else "idle",
        "running_method": running,
        "running_operation_id": operation_id,
        "operation_elapsed_seconds": 2.5 if running else None,
    }


def test_status_reports_bridge_parts_workspace_without_a_panel():
    nx = fake_nx()
    ok = run(nx, "nx_get_status")
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    data = ok.data
    assert data["bridge"] == {
        "state": "connected",
        "nx_version": "v2406",
        "nx_build": "v2406",
        "build_tested": True,
        "interactive": False,
        "session_id": "kit",
        "answered_from": "nx",
        "license_unavailable": [],
    }
    work = data["parts"]["work"]
    assert work["path"].endswith("work.prt") and work["units"] == "mm"
    assert work["part"]["id"] and work["modified"] is False
    assert data["parts"]["display"]["part"]["id"] == work["part"]["id"]
    assert data["parts"]["loaded"] == 1 and data["parts"]["work_component"] is None
    assert data["control"] is None and data["operation"] is None
    assert data["workspace"]["root"] == str(services(nx).workspace.root)
    assert data["workspace"]["absolute_paths"] == st.RULES["absolute_paths"]


def test_status_reads_the_panel_mode_and_filters_sections(monkeypatch):
    nx = fake_nx()
    monkeypatch.setattr(services(nx), "control_status", panel_status, raising=False)
    ok = run(nx, "nx_get_status", include=["control", "bridge"])
    assert set(ok.data) == {"control", "bridge"}
    assert ok.data["control"] == {
        "mode": "auto",
        "mode_label": "Auto",
        "busy": False,
        "waiting": 0,
        "engineer_busy": False,
        "waiting_for_answer": 0,
        "activity": "idle",
        "running": None,
        "queue": {"waiting": 0, "oldest_seconds": None},
    }
    assert ok.data["bridge"]["interactive"] is True


def test_status_counts_agree_while_calls_wait(monkeypatch):
    """Never busy false and waiting 0 next to queue.waiting 4 (busy once meant the
    engineer only, waiting the Ask me first card only)."""
    nx = fake_nx()
    status = panel_status() | {"queue": {"waiting": 4, "oldest_seconds": 0.0}}
    monkeypatch.setattr(services(nx), "control_status", lambda: status, raising=False)
    control = run(nx, "nx_get_status", include=["control"]).data["control"]
    assert control["busy"] is True and control["waiting"] == 4 == control["queue"]["waiting"]
    assert control["activity"] == "queued" and control["engineer_busy"] is False
    assert control["waiting_for_answer"] == 0
    status.update(engineer_busy=True, queue={"waiting": 0, "oldest_seconds": None})
    control = run(nx, "nx_get_status", include=["control"]).data["control"]
    assert control["busy"] and control["engineer_busy"] and control["waiting"] == 0
    assert control["activity"] == "idle"


def test_status_without_a_work_part():
    nx = fake_nx()
    nx.session.Parts.Work = nx.session.Parts.Display = None
    ok = run(nx, "nx_get_status", include=["parts"])
    assert ok.ok and ok.data["parts"]["work"] is None and ok.data["parts"]["display"] is None


def test_snapshot_envelope_reports_the_running_step_and_its_progress(tmp_path):
    nx = fake_nx()
    store = services(nx).store
    Receipts(store, "s").begin("nx_run_batch", {"operations": []}, "op_running_1", None)
    record = store.get("op_running_1")
    Receipts(store, "s").progress(record, 3, 10)
    snapshot = panel_status("nx_run_batch", "op_running_1") | {
        "parts": st.snapshot_parts(nx.session, tmp_path)
    }
    envelope = st.envelope_from_snapshot(
        snapshot, {"include": None}, root=tmp_path, build="v2406.1700", store=store
    )
    assert envelope["ok"] and envelope["tool"] == "nx_get_status"
    data = envelope["data"]
    assert data["bridge"]["answered_from"] == "snapshot" and data["bridge"]["nx_version"] == "v2406"
    assert data["bridge"]["nx_build"] == "v2406.1700"
    assert data["control"]["activity"] == "running" and data["control"]["running"] == "nx_run_batch"
    assert data["operation"]["tool"] == "nx_run_batch"
    assert data["operation"]["operation_id"] == "op_running_1"  # solo: shown as before
    assert data["operation"]["progress"]["completed"] == 3
    assert data["operation"]["progress"]["total"] == 10
    assert data["parts"]["work"]["part"] is None and data["parts"]["loaded"] == 1
    assert data["workspace"]["root"] == str(tmp_path)


def test_snapshot_parts_survive_a_broken_session():
    assert st.snapshot_parts(object(), "C:/w") is None


async def test_the_proxy_answers_status_when_the_bridge_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.side_effect = NXError("NX_BRIDGE_UNAVAILABLE", "connection refused")
    proxy = CatalogueProxy(bridge, Workspace(tmp_path), None, build=None)
    answer = await proxy.call(st.nx_get_status.spec, {}, detail="full")
    body = answer.structuredContent
    assert not answer.isError and body["ok"] and body["mutation_outcome"] == "not_applicable"
    assert body["data"]["bridge"]["state"] == "down"
    assert body["data"]["bridge"]["answered_from"] == "sidecar"
    assert body["data"]["bridge"]["health"]["hint"].startswith("Start NX")
    assert body["data"]["workspace"]["root"] == str(tmp_path)
    assert body["data"]["parts"] is None and "NX_BRIDGE_UNAVAILABLE" not in body["data"]
    assert "connection refused" in body["warnings"][0]["message"]


async def test_other_bridge_failures_stay_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.side_effect = NXError("NX_INVALID_ARGUMENT", "bad")
    proxy = CatalogueProxy(bridge, Workspace(tmp_path), None, build=None)
    answer = await proxy.call(st.nx_get_status.spec, {}, detail="full")
    assert answer.isError and answer.structuredContent["error"]["code"] == "NX_INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# operations (sidecar)


@pytest.fixture
def store(tmp_path):
    from nx_mcp.core.receipts import OperationStore

    return OperationStore(tmp_path)


def test_unknown_operation_is_unknown_not_an_error(tmp_path, store):
    ok = side(tmp_path, st.nx_get_operation, operation="op_never_seen")
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    assert ok.data["state"] == "unknown" and ok.data["mutation_outcome"] == "unknown"
    assert ok.data["reason"] and ok.data["cancel_requested"] is False
    assert "read-only call" in ok.data["reason"]  # reads are never receipted


def test_a_restarted_receipt_keeps_its_own_reason(tmp_path, store):
    store.put({"operation_id": "op_restarted", "tool": "nx_create_sketch", "state": "unknown",
               "fingerprint": "f", "reason": "NX process restarted"})  # fmt: skip
    data = side(tmp_path, st.nx_get_operation, operation="op_restarted").data
    assert data["state"] == "unknown" and data["reason"] == "NX process restarted"


def test_committed_catalogue_operation_carries_its_result(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_sketch", {"name": "A"}, "op_committed1", "part_1")
    envelope = {"ok": True, "mutation_outcome": "committed", "data": {"sketch": {"id": "sk"}}}
    receipts.finish(record, envelope)
    ok = side(tmp_path, st.nx_get_operation, operation="op_committed1")
    assert ok.data["state"] == "committed" and ok.data["tool"] == "nx_create_sketch"
    # A poll stays small: the result and the arguments only on request.
    assert ok.data["result"] is None and ok.data["arguments"] is None
    assert ok.data["omitted"] == ["result", "arguments"] and ok.data["error"] is None
    assert ok.data["started_at"] and ok.data["finished_at"]
    full = side(tmp_path, st.nx_get_operation, operation="op_committed1", full=True).data
    assert full["result"]["data"]["sketch"]["id"] == "sk" and full["arguments"] == {"name": "A"}
    assert "omitted" not in full and "result_stale" not in full


def test_failed_receipt_reports_its_error_and_no_result(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_extrude", {}, "op_failed_01", None)
    error = {"code": "NX_BUILD_FAILED", "category": "geometry_failed", "message": "m", "hint": "h"}
    receipts.finish(record, {"ok": False, "mutation_outcome": "rolled_back", "error": error})
    failed = side(tmp_path, st.nx_get_operation, operation="op_failed_01").data
    assert failed["state"] == "failed" and failed["mutation_outcome"] == "rolled_back"
    assert failed["error"] == {"code": "NX_BUILD_FAILED", "category": "geometry_failed",
                               "message": "m"}  # fmt: skip
    assert failed["result"] is None


def test_running_batch_reports_progress_and_can_be_cancelled(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_run_batch", {"operations": []}, "op_running_2", None)
    receipts.progress(record, 2, 5)
    running = side(tmp_path, st.nx_get_operation, operation="op_running_2").data
    assert running["state"] == "running" and running["progress"]["completed"] == 2
    assert running["progress"]["total"] == 5 and running["cancel_requested"] is False
    ok = side(tmp_path, st.nx_cancel_operation, operation="op_running_2")
    # Only a request flag is written: the cancel itself commits nothing.
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    assert {k: v for k, v in ok.data.items() if k != "note"} == {
        "operation_id": "op_running_2", "tool": "nx_run_batch", "state": "running",
        "cancel_requested": True}  # fmt: skip
    assert "nx_get_operation" in ok.data["note"]
    assert receipts.cancel_requested("op_running_2")  # what tx.checkpoint reads
    again = side(tmp_path, st.nx_get_operation, operation="op_running_2").data
    assert again["cancel_requested"] is True


def test_cancel_refuses_unknown_finished_and_uncatalogued_operations(tmp_path, store):
    unknown = side(tmp_path, st.nx_cancel_operation, operation="op_never_seen")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.mutation_outcome == "not_started"
    assert "only changing calls" in unknown.error["message"]
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_sketch", {}, "op_done_0001", None)
    receipts.finish(record, {"ok": True, "mutation_outcome": "committed", "data": {}})
    done = side(tmp_path, st.nx_cancel_operation, operation="op_done_0001")
    # A finished call can never be cancelled: retry never, not a blocker that clears.
    assert done.error["code"] == "NX_NOT_RUNNING" and done.error["retry"] == "never"
    assert done.error["details"] == {"state": "committed", "tool": "nx_create_sketch"}
    assert "already finished as committed" in done.error["message"]
    assert "nx_undo_operation" in done.error["hint"] and done.mutation_outcome == "not_started"
    record, _ = receipts.begin("nx_create_sketch", {"a": 1}, "op_failed_0001", None)
    receipts.finish(record, {"ok": False, "mutation_outcome": "rolled_back",
                             "error": {"code": "NX_BUILD_FAILED"}})  # fmt: skip
    failed = side(tmp_path, st.nx_cancel_operation, operation="op_failed_0001")
    assert failed.error["code"] == "NX_NOT_RUNNING"
    assert "nx_get_operation" in failed.error["hint"]
    store.put({"operation_id": "op_not_catalogue", "tool": "nx_removed_tool", "state": "running",
               "mutation_outcome": "unknown"})  # fmt: skip
    other = side(tmp_path, st.nx_cancel_operation, operation="op_not_catalogue")
    assert other.error["code"] == "NX_UNSUPPORTED_OPTION"
    for op in ("op_never_seen", "op_done_0001", "op_failed_0001", "op_not_catalogue"):
        assert not store.path(op).with_suffix(".cancel").exists()


def test_operation_argument_is_checked(tmp_path):
    bad = side(tmp_path, st.nx_get_operation, operation="x")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/operation"
    # The error says what is wrong, not just "has an invalid format".
    short = side(tmp_path, st.nx_get_operation, operation="short")
    assert "too short: 5 of at least 8" in short.error["message"]
    chars = side(tmp_path, st.nx_cancel_operation, operation="op/../../x1")
    assert chars.error["field"] == "/operation" and "'/'" in chars.error["message"]
    assert chars.mutation_outcome == "not_started"
    long = side(tmp_path, st.nx_get_operation, operation="o" * 129)
    assert "too long" in long.error["message"]


def test_operation_ids_are_trimmed(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_sketch", {}, "op_trimmed_1", None)
    receipts.finish(record, {"ok": True, "mutation_outcome": "committed", "data": {}})
    ok = side(tmp_path, st.nx_get_operation, operation=" op_trimmed_1\n")
    assert ok.data["state"] == "committed" and ok.data["operation_id"] == "op_trimmed_1"


def test_the_running_operation_id_shows_only_to_its_own_caller(tmp_path):
    """With two or more parts loaded a reader never sees another agent's running id;
    solo it shows as before."""
    nx = fake_nx()
    snapshot = {**panel_status("nx_run_batch", "op_running_7"), "parts": {"loaded": 2}}
    solo = {**snapshot, "parts": {"loaded": 1}}
    shown = st.envelope_from_snapshot(solo, {}, root=tmp_path, build="v2406")
    assert shown["data"]["operation"]["operation_id"] == "op_running_7"
    second_part(nx)

    def operation(params):
        envelope = st.envelope_from_snapshot(snapshot, params, root=tmp_path, build="v2406")
        return envelope["data"]["operation"]

    other = operation({"include": ["operation"], "operation_id": "op_other_caller"})
    assert other["operation_id"] is None and other["tool"] == "nx_run_batch"
    assert other["elapsed_seconds"] == 2.5
    assert operation({"operation_id": "op_running_7"})["operation_id"] == "op_running_7"
    services(nx).control_status = lambda: snapshot  # the panel's live status (NX side)
    read = run(nx, "nx_get_status", include=["operation"])
    assert read.data["operation"]["operation_id"] is None, read
    assert read.data["operation"]["tool"] == "nx_run_batch"
    own = run(nx, "nx_get_status", include=["operation"], operation_id="op_running_7")
    assert own.data["operation"]["operation_id"] == "op_running_7", own


def test_an_undone_operation_reports_reverted_and_its_stale_result(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_block", {"length": 40}, "op_undone_01", "part_1")
    envelope = {"ok": True, "mutation_outcome": "committed", "data": {"body": {"id": "b:7"}}}
    receipts.finish(record, envelope)
    record = store.get("op_undone_01")
    record["reverted_by"] = "op_the_undo_1"
    store.put(record)
    data = side(tmp_path, st.nx_get_operation, operation="op_undone_01").data
    assert data["state"] == "reverted" and data["reverted_by"] == "op_the_undo_1"
    assert data["mutation_outcome"] == "committed" and data["result"] is None
    full = side(tmp_path, st.nx_get_operation, operation="op_undone_01", full=True).data
    assert full["result_stale"] is True and full["result"]["data"]["body"]["id"] == "b:7"


def test_a_running_call_blocked_by_a_dialog_says_so(tmp_path, store):
    """The host's heartbeat names the dialog NX waits on; a poll of the blocked call shows
    it (since as ISO 8601, seconds waited) instead of only its stale progress."""
    import json
    import time

    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_run_batch", {"operations": []}, "op_blocked_01", None)
    receipts.progress(record, 8, 22)
    beat = {"running_method": "nx_run_batch", "running_operation_id": "op_blocked_01",
            "blocked_by_dialog": {"title": "Large Sketch Performance", "hwnd": 77,
                                  "since": time.time() - 90}}  # fmt: skip
    (tmp_path / ".nx-mcp").mkdir(exist_ok=True)
    (tmp_path / ".nx-mcp" / "ui-state.json").write_text(json.dumps(beat), encoding="utf-8")
    data = side(tmp_path, st.nx_get_operation, operation="op_blocked_01").data
    dialog = data["blocked_by_dialog"]
    assert dialog["title"] == "Large Sketch Performance" and "hwnd" not in dialog
    assert 89 <= dialog["seconds"] <= 95 and dialog["since"].endswith("Z")
    assert "dialog" in data["note"] and data["progress"]["completed"] == 8
    assert data["progress"]["age_seconds"] >= 0
    beat["running_operation_id"] = "op_someone_else"  # another call is blocked: not this one
    (tmp_path / ".nx-mcp" / "ui-state.json").write_text(json.dumps(beat), encoding="utf-8")
    other = side(tmp_path, st.nx_get_operation, operation="op_blocked_01").data
    assert "blocked_by_dialog" not in other


def test_a_silent_running_call_gets_a_note(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_run_batch", {"operations": []}, "op_silent_01", None)
    record["progress"] = {"completed": 1, "total": 4, "at": "2020-01-01T00:00:00+00:00"}
    store.put(record)
    data = side(tmp_path, st.nx_get_operation, operation="op_silent_01").data
    assert data["progress"]["age_seconds"] > 60 and "No progress for" in data["note"]


def test_dialog_rows_use_iso_times():
    row = st.dialog_row({"title": "T", "hwnd": 5, "since": 0.0}, now=30.0)
    assert row == {"title": "T", "since": "1970-01-01T00:00:00Z", "seconds": 30}
    assert st.dialog_row(None) is None


def test_status_on_an_untested_build_reports_the_self_test_and_failed_tools(monkeypatch):
    from nx_mcp.core import catalogue, evidence

    monkeypatch.delenv("NX_MCP_UNTESTED", raising=False)
    nx = fake_nx()
    services(nx).build = "2512.1000"
    bridge = run(nx, "nx_get_status", include=["bridge"]).data["bridge"]
    assert bridge["nx_version"] == "v2512" and bridge["nx_build"] == "2512.1000"
    assert bridge["build_tested"] is False and bridge["self_test"] is None
    assert "read_only" not in bridge and "version_unsupported" not in bridge
    evidence.set_self_test({"state": "failed", "reason": "the undo failed"})
    spec = catalogue.get("nx_create_blend")
    evidence.remember_version(spec, "2512.1000", None, "NXOpen.Features.X.Y")
    bridge = run(nx, "nx_get_status", include=["bridge"]).data["bridge"]
    assert bridge["self_test"]["state"] == "failed"
    assert bridge["read_only"] == "NX's undo self-test failed: the undo failed"
    assert [r["tool"] for r in bridge["version_unsupported"]] == ["nx_create_blend"]
    evidence.set_self_test(None)
    monkeypatch.setenv("NX_MCP_UNTESTED", "read_only")
    assert st.read_only_reason() == "NX_MCP_UNTESTED=read_only"
    # The sidecar answering for NX knows no session memory.
    seen = st.sidecar_envelope("w", {"state": "down"}, {}, build="2512.1000")
    assert seen["data"]["bridge"]["build_tested"] is False
    assert "self_test" not in seen["data"]["bridge"]
