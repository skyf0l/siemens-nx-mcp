"""nx_set_control on the shared fake kernel: the panel mode through Services.control."""

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.session import control
from tests.kit import fake_nx, run
from tests.kit.kernel import services

RANK = {"ask": 0, "auto": 1, "agent": 2}


class Panel:
    """The interactive host's side: the agent lowers at once, raises only when approved."""

    def __init__(self, mode="agent", approved=False):
        self.mode, self.approved, self.calls = mode, approved, []

    def status(self):
        return {
            "mode": self.mode,
            "reserved_for_agent": self.mode == "agent",
            "engineer_busy": False,
            "waiting_for_engineer": 0,
            "activity": "reserved_idle" if self.mode == "agent" else "idle",
            "running_method": None,
        }

    def control(self, mode):
        self.calls.append(mode)
        if RANK[mode] > RANK[self.mode] and not self.approved:
            raise NXError("NX_CONTROL_PAUSED", "Only the engineer can give the agent more control")
        self.mode = mode
        return self.status()


@pytest.fixture
def panel(monkeypatch):
    nx = fake_nx()
    host = Panel()
    kit = services(nx)
    monkeypatch.setattr(kit, "control_status", host.status, raising=False)
    monkeypatch.setattr(kit, "control", host.control, raising=False)
    return nx, host


def test_status_reads_mode_owner_busy_and_waiting(panel):
    nx, host = panel
    ok = run(nx, "nx_set_control")
    assert ok.ok and ok.mutation_outcome == "not_applicable" and host.calls == []
    assert ok.data == {
        "mode": "agent",
        "mode_label": "Agent drives",
        "owner": "agent",
        "busy": False,
        "waiting": 0,
        "activity": "reserved_idle",
        "running": None,
    }


@pytest.mark.parametrize(("mode", "now"), [("auto", "auto"), ("ask", "ask"), ("manual", "ask")])
def test_lowering_control_is_immediate(panel, mode, now):
    nx, host = panel
    ok = run(nx, "nx_set_control", mode=mode)
    assert ok.ok and ok.mutation_outcome == "committed"
    assert host.calls == [now] and ok.data["mode"] == now and ok.data["owner"] == "engineer"
    assert ok.data["mode_label"] == control.LABELS[now]


def test_more_control_without_the_engineer_is_refused_unchanged(panel):
    nx, host = panel
    host.mode = "ask"
    failed = run(nx, "nx_set_control", mode="agent")
    assert not failed.ok and failed.error["code"] == "NX_CONTROL_PAUSED"
    assert failed.mutation_outcome == "not_started" and host.mode == "ask"


def test_approved_request_raises_control(panel):
    nx, host = panel
    host.mode, host.approved = "auto", True
    ok = run(nx, "nx_set_control", mode="agent")
    assert ok.ok and ok.data["owner"] == "agent" and ok.data["mode"] == "agent"


def test_without_the_panel_status_and_changes_fail_cleanly():
    nx = fake_nx()
    for args in ({}, {"mode": "ask"}):
        failed = run(nx, "nx_set_control", **args)
        assert not failed.ok and failed.error["code"] == "NX_VIEWPORT_UNAVAILABLE"
        assert failed.mutation_outcome in ("not_started", "not_applicable", "unknown")
    assert run(nx, "nx_set_control", mode="ask").mutation_outcome == "not_started"


def test_snapshot_envelope_matches_the_kernel_shape():
    status = Panel(mode="auto").status() | {"engineer_busy": True, "waiting_for_engineer": 2}
    envelope = control.envelope_from_snapshot(status, {"mode": "status", "operation_id": "op"})
    assert envelope["ok"] and envelope["tool"] == "nx_set_control"
    assert envelope["operation_id"] == "op" and envelope["mutation_outcome"] == "not_applicable"
    assert envelope["data"]["busy"] is True and envelope["data"]["waiting"] == 2
    assert envelope["data"]["owner"] == "engineer" and envelope["data"]["mode_label"] == "Auto"


def test_target_maps_manual_to_ask():
    assert control.target({}) == "status" and control.target({"mode": "manual"}) == "ask"
