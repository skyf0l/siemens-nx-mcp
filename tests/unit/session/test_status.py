"""nx_get_status, nx_get_operation and nx_cancel_operation (domains/session/status.py)."""

from unittest.mock import AsyncMock

import pytest

from nx_mcp.core.receipts import Receipts
from nx_mcp.domains.session import status as st
from nx_mcp.runtime import NXToolError
from nx_mcp.sidecar.proxy import CatalogueProxy
from nx_mcp.workspace import Workspace
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.unit.parts_files.support import side


def panel_status(running=None, operation_id=None):
    return {
        "mode": "agent",
        "reserved_for_agent": True,
        "engineer_busy": False,
        "waiting_for_engineer": 0,
        "activity": "running" if running else "reserved_idle",
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
        "interactive": False,
        "session_id": "kit",
        "answered_from": "nx",
    }
    work = data["parts"]["work"]
    assert work["path"].endswith("work.prt") and work["units"] == "mm"
    assert work["part"]["id"] and work["modified"] is False
    assert data["parts"]["display"]["part"]["id"] == work["part"]["id"]
    assert data["parts"]["loaded"] == 1
    assert data["control"] is None and data["operation"] is None
    assert data["workspace"]["root"] == str(services(nx).workspace.root)
    assert data["workspace"]["absolute_paths"] == st.RULES["absolute_paths"]


def test_status_reads_the_panel_mode_and_filters_sections(monkeypatch):
    nx = fake_nx()
    monkeypatch.setattr(services(nx), "control_status", panel_status, raising=False)
    ok = run(nx, "nx_get_status", include=["control", "bridge"])
    assert set(ok.data) == {"control", "bridge"}
    assert (
        ok.data["control"]["mode_label"] == "Agent drives"
        and ok.data["control"]["owner"] == "agent"
    )
    assert ok.data["bridge"]["interactive"] is True


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
    assert data["operation"]["operation_id"] == "op_running_1"
    assert data["operation"]["progress"]["completed"] == 3
    assert data["operation"]["progress"]["total"] == 10
    assert data["parts"]["work"]["part"] is None and data["parts"]["loaded"] == 1
    assert data["workspace"]["root"] == str(tmp_path)


def test_snapshot_parts_survive_a_broken_session():
    assert st.snapshot_parts(object(), "C:/w") is None


async def test_the_proxy_answers_status_when_the_bridge_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.side_effect = NXToolError("NX_BRIDGE_UNAVAILABLE", "connection refused")
    proxy = CatalogueProxy(bridge, Workspace(tmp_path), None, build=None)
    answer = await proxy.call(st.nx_get_status.spec, {}, detail="full")
    body = answer.structuredContent
    assert not answer.isError and body["ok"] and body["mutation_outcome"] == "not_applicable"
    assert body["data"]["bridge"]["state"] == "unavailable"
    assert body["data"]["bridge"]["answered_from"] == "sidecar"
    assert body["data"]["workspace"]["root"] == str(tmp_path)
    assert body["data"]["parts"] is None and "NX_BRIDGE_UNAVAILABLE" not in body["data"]
    assert "connection refused" in body["warnings"][0]["message"]


async def test_other_bridge_failures_stay_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.side_effect = NXToolError("NX_INVALID_ARGUMENT", "bad")
    proxy = CatalogueProxy(bridge, Workspace(tmp_path), None, build=None)
    answer = await proxy.call(st.nx_get_status.spec, {}, detail="full")
    assert answer.isError and answer.structuredContent["error"]["code"] == "NX_INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# operations (sidecar)


@pytest.fixture
def store(tmp_path):
    from nx_mcp.recovery import OperationStore

    return OperationStore(tmp_path)


def test_unknown_operation_is_unknown_not_an_error(tmp_path, store):
    ok = side(tmp_path, st.nx_get_operation, operation="op_never_seen")
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    assert ok.data["state"] == "unknown" and ok.data["mutation_outcome"] == "unknown"
    assert ok.data["reason"] and ok.data["cancel_requested"] is False


def test_committed_catalogue_operation_carries_its_result(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_sketch", {"name": "A"}, "op_committed1", "part_1")
    envelope = {"ok": True, "mutation_outcome": "committed", "data": {"sketch": {"id": "sk"}}}
    receipts.finish(record, envelope)
    ok = side(tmp_path, st.nx_get_operation, operation="op_committed1")
    assert ok.data["state"] == "committed" and ok.data["tool"] == "nx_create_sketch"
    assert ok.data["result"]["data"]["sketch"]["id"] == "sk" and ok.data["error"] is None
    assert ok.data["started_at"] and ok.data["finished_at"]


def test_failed_and_legacy_receipts(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_extrude", {}, "op_failed_01", None)
    error = {"code": "NX_BUILD_FAILED", "category": "geometry_failed", "message": "m", "hint": "h"}
    receipts.finish(record, {"ok": False, "mutation_outcome": "rolled_back", "error": error})
    failed = side(tmp_path, st.nx_get_operation, operation="op_failed_01").data
    assert failed["state"] == "failed" and failed["mutation_outcome"] == "rolled_back"
    assert failed["error"] == {"code": "NX_BUILD_FAILED", "category": "geometry_failed",
                               "message": "m"}  # fmt: skip
    assert failed["result"] is None
    store.put({"operation_id": "op_legacy_01", "method": "nx_hole", "state": "committed",
               "mutation_outcome": "committed", "result": {"status": "success"}})  # fmt: skip
    legacy = side(tmp_path, st.nx_get_operation, operation="op_legacy_01").data
    assert legacy["tool"] == "nx_hole" and legacy["result"] == {"status": "success"}


def test_running_batch_reports_progress_and_can_be_cancelled(tmp_path, store):
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_run_batch", {"operations": []}, "op_running_2", None)
    receipts.progress(record, 2, 5)
    running = side(tmp_path, st.nx_get_operation, operation="op_running_2").data
    assert running["state"] == "running" and running["progress"]["completed"] == 2
    assert running["progress"]["total"] == 5 and running["cancel_requested"] is False
    ok = side(tmp_path, st.nx_cancel_operation, operation="op_running_2")
    assert ok.ok and ok.mutation_outcome == "committed"
    assert ok.data == {"operation_id": "op_running_2", "tool": "nx_run_batch",
                       "state": "running", "cancel_requested": True}  # fmt: skip
    assert receipts.cancel_requested("op_running_2")  # what tx.checkpoint reads
    again = side(tmp_path, st.nx_get_operation, operation="op_running_2").data
    assert again["cancel_requested"] is True


def test_cancel_refuses_unknown_finished_and_legacy_operations(tmp_path, store):
    unknown = side(tmp_path, st.nx_cancel_operation, operation="op_never_seen")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.mutation_outcome == "not_started"
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_create_sketch", {}, "op_done_0001", None)
    receipts.finish(record, {"ok": True, "mutation_outcome": "committed", "data": {}})
    done = side(tmp_path, st.nx_cancel_operation, operation="op_done_0001")
    assert done.error["code"] == "NX_IN_USE" and done.error["details"]["state"] == "committed"
    assert done.mutation_outcome == "not_started"
    store.put({"operation_id": "op_legacy_02", "method": "nx_hole", "state": "running",
               "mutation_outcome": "unknown"})  # fmt: skip
    legacy = side(tmp_path, st.nx_cancel_operation, operation="op_legacy_02")
    assert legacy.error["code"] == "NX_UNSUPPORTED_OPTION"
    for op in ("op_never_seen", "op_done_0001", "op_legacy_02"):
        assert not store.path(op).with_suffix(".cancel").exists()


def test_operation_argument_is_checked(tmp_path):
    bad = side(tmp_path, st.nx_get_operation, operation="x")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/operation"
