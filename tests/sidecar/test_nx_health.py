"""NX busy, hung or down, told from outside NX within seconds (host/heartbeat.py).

While one long native step holds NX's main thread, NX accepts no bridge connection: a
status read must not wait for that step, and a call that cannot start must not wait
for minutes either. The sidecar reads the host's heartbeat file and checks the NX
process (alive, window responding, CPU time) instead.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.errors import NXError
from nx_mcp.domains.session import status as st
from nx_mcp.host import dialogs, heartbeat
from nx_mcp.sidecar import proxy as proxy_module
from nx_mcp.sidecar.proxy import CatalogueProxy
from nx_mcp.workspace import Workspace

PID = 4242
BOX = dialogs.Dialog(7, "Edit Sketch", "The edit causes curves or dimensions to be deleted.")


def beat(root, *, running="nx_check_model", seconds=2400.0, age=None, pid=PID, op="op_long_1"):
    """Write the host's heartbeat: ``running`` for ``seconds`` (None: idle)."""
    now = time.time()
    started = now - seconds if running else None
    state = {
        "interactive": True,
        "pid": pid,
        "session_id": "s1",
        "mode": "auto",
        "activity": "running" if running else "idle",
        "running_method": running,
        "running_operation_id": op if running else None,
        "running_part": "C:/w/bracket.prt" if running else None,
        "running_started_at": started,
        "sampled_at": started if running else now - (age or 0.5),
        "queue": {"waiting": 2, "oldest_seconds": 90.0},
        "waiting_for_engineer": 0,
        "engineer_busy": False,
        "parts": {"work": {"part": None, "path": "bracket.prt", "units": "mm"}},
    }
    folder = root / ".nx-mcp"
    folder.mkdir(exist_ok=True)
    (folder / "ui-state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def nx(monkeypatch):
    """The NX process seen from outside: alive, window responding, CPU per sample."""
    seen = NS(alive=True, responding=False, cpu_step=1.0, dialogs=[], cpu=0.0)

    def cpu(pid):
        seen.cpu += seen.cpu_step
        return seen.cpu

    monkeypatch.setattr(dialogs, "process_alive", lambda pid: seen.alive)
    monkeypatch.setattr(dialogs, "scan", lambda pid, **kw: list(seen.dialogs))
    monkeypatch.setattr(heartbeat, "responding", lambda pid: seen.responding)
    monkeypatch.setattr(heartbeat, "cpu_seconds", cpu)
    monkeypatch.setattr(heartbeat, "SAMPLE_SECONDS", 0.01)
    monkeypatch.setattr(heartbeat.time, "sleep", lambda s: None)
    return seen


# ---------------------------------------------------------------------------
# assess


def test_a_long_step_is_busy_with_tool_part_and_time(tmp_path, nx):
    beat(tmp_path)
    health = heartbeat.assess(tmp_path, PID)
    assert health["state"] == "busy" and health["process"]["cpu_busy"] is True
    assert health["running"]["tool"] == "nx_check_model"
    assert health["running"]["part"] == "bracket.prt"
    assert 2390 <= health["running"]["seconds"] <= 2410
    assert health["running"]["operation_id"] is None  # another caller's id stays hidden
    assert "computing nx_check_model on bracket.prt for 40 min" in health["message"]
    assert "accepts no call until that step ends" in health["message"]
    assert "Do not resend nx_check_model" in health["hint"]


def test_the_running_operation_id_shows_only_to_its_own_reader(tmp_path, nx):
    beat(tmp_path)
    assert heartbeat.assess(tmp_path, PID, reader="op_long_1")["running"]["operation_id"] == (
        "op_long_1"
    )


def test_a_step_without_cpu_and_a_hung_window_is_unresponsive(tmp_path, nx):
    beat(tmp_path, seconds=600)
    nx.cpu_step = 0.0
    health = heartbeat.assess(tmp_path, PID)
    assert health["state"] == "unresponsive" and health["process"]["cpu_busy"] is False
    assert "uses no CPU" in health["message"] and "never kill NX" in health["hint"]


def test_a_silent_host_with_no_call_is_unresponsive(tmp_path, nx):
    beat(tmp_path, running=None, age=300)
    health = heartbeat.assess(tmp_path, PID)
    assert health["state"] == "unresponsive" and health["running"] is None
    assert "silent for 5 min" in health["message"]


def test_a_fresh_idle_host_that_did_not_answer_is_not_answering(tmp_path, nx):
    beat(tmp_path, running=None, age=0.5)
    nx.responding = True
    assert heartbeat.assess(tmp_path, PID)["state"] == "not_answering"


def test_a_gone_process_or_no_host_is_down(tmp_path, nx):
    beat(tmp_path)
    nx.alive = False
    health = heartbeat.assess(tmp_path, PID)
    assert health["state"] == "down" and f"pid {PID}" in health["message"]
    health = heartbeat.assess(tmp_path, None, registered=False)
    assert health["state"] == "down" and "No NX MCP host is registered" in health["message"]


def test_a_modal_dialog_is_reported_as_such(tmp_path, nx):
    beat(tmp_path, seconds=30)
    nx.dialogs = [BOX]
    health = heartbeat.assess(tmp_path, PID)
    assert health["state"] == "blocked_by_dialog" and health["dialogs"][0]["title"] == "Edit Sketch"


def test_a_heartbeat_of_an_earlier_nx_process_is_ignored(tmp_path, nx):
    beat(tmp_path, pid=1111)
    assert heartbeat.assess(tmp_path, PID)["running"] is None


@pytest.mark.parametrize(
    ("seconds", "text"), [(45, "45 s"), (157, "2 min"), (3200, "53 min"), (3900, "1 h 05 min")]
)
def test_durations_read_naturally(seconds, text):
    assert heartbeat.duration(seconds) == text


# ---------------------------------------------------------------------------
# the sidecar (proxy)


def listing():
    catalogue.load()
    spec = catalogue.get("nx_list_parts")
    assert spec is not None and spec.runs != "sidecar"
    return spec


def make(tmp_path, monkeypatch, bridge=None):
    bridge = bridge or AsyncMock()
    p = CatalogueProxy(bridge, Workspace(tmp_path), None, build="v2406")
    monkeypatch.setattr(p, "_descriptor", lambda: NS(pid=PID, nx_version="2406"))
    return p, bridge


async def test_status_answers_at_once_while_a_long_step_holds_nx(tmp_path, monkeypatch, nx):
    beat(tmp_path)
    p, bridge = make(tmp_path, monkeypatch)
    started = time.monotonic()
    answer = await p.call(st.nx_get_status.spec, {}, detail="full")
    assert time.monotonic() - started < 3
    bridge.call.assert_not_called()  # never queued behind the step it reports
    body = answer.structuredContent
    assert not answer.isError and body["ok"]
    bridge_info = body["data"]["bridge"]
    assert bridge_info["state"] == "busy" and bridge_info["answered_from"] == "sidecar"
    assert bridge_info["interactive"] is True and bridge_info["session_id"] == "s1"
    assert bridge_info["health"]["running"]["tool"] == "nx_check_model"
    assert body["data"]["operation"]["tool"] == "nx_check_model"
    assert body["data"]["operation"]["elapsed_seconds"] >= 2390
    assert body["data"]["control"]["running"] == "nx_check_model"
    assert body["data"]["control"]["queue"]["waiting"] == 2
    assert "nx_check_model on bracket.prt" in body["warnings"][0]["message"]


async def test_status_stops_waiting_after_a_few_seconds(tmp_path, monkeypatch, nx):
    beat(tmp_path, seconds=1)  # too recent to skip the bridge
    nx.responding = True
    monkeypatch.setattr(proxy_module, "STATUS_WAIT", 0.2)

    async def stuck(method, params):
        await asyncio.sleep(30)

    p, _ = make(tmp_path, monkeypatch, AsyncMock(call=stuck))
    started = time.monotonic()
    answer = await p.call(st.nx_get_status.spec, {}, detail="full")
    assert time.monotonic() - started < 3
    bridge_info = answer.structuredContent["data"]["bridge"]
    assert bridge_info["state"] == "busy" and bridge_info["answered_from"] == "sidecar"


async def test_status_with_nx_down_says_down(tmp_path, monkeypatch, nx):
    nx.alive = False
    bridge = AsyncMock()
    bridge.call.side_effect = NXError("NX_BRIDGE_UNAVAILABLE", "connection refused")
    p, _ = make(tmp_path, monkeypatch, bridge)
    body = (await p.call(st.nx_get_status.spec, {}, detail="full")).structuredContent
    assert body["ok"] and body["data"]["bridge"]["state"] == "down"
    assert body["data"]["control"] is None
    assert "connection refused" in body["warnings"][0]["message"]


async def test_a_call_behind_a_step_holding_nx_is_refused_at_once(tmp_path, monkeypatch, nx):
    beat(tmp_path)
    p, bridge = make(tmp_path, monkeypatch)
    spec = listing()
    body = (await p.call(spec, {}, detail="full")).structuredContent
    bridge.call.assert_not_called()
    assert not body["ok"] and body["mutation_outcome"] == "not_started"
    assert body["error"]["code"] == "NX_BUSY" and body["error"]["retry"] == "after_wait"
    assert "was not sent" in body["error"]["message"]
    assert body["error"]["details"]["running"]["tool"] == "nx_check_model"


async def test_a_short_step_or_a_responding_nx_does_not_refuse(tmp_path, monkeypatch, nx):
    spec = listing()
    for seconds, responding in ((30, False), (2400, True)):
        beat(tmp_path, seconds=seconds)
        nx.responding = responding
        bridge = AsyncMock()
        bridge.call.return_value = {"ok": True, "tool": "nx_list_parts", "data": {}}
        p, _ = make(tmp_path, monkeypatch, bridge)
        await p.call(spec, {}, detail="full")
        bridge.call.assert_awaited_once()


def test_a_stalled_call_names_what_nx_runs(tmp_path, monkeypatch, nx):
    beat(tmp_path)
    p, _ = make(tmp_path, monkeypatch)
    timeout = NXError(
        "NX_OUTCOME_UNKNOWN", "no answer arrived.", details={"mutation_outcome": "unknown"}
    )
    error = p.explain_stall(timeout)
    assert error.code == "NX_OUTCOME_UNKNOWN" and error.details["mutation_outcome"] == "unknown"
    assert "computing nx_check_model on bracket.prt for 40 min" in error.message
    assert "nx_get_operation" in error.hint and error.details["nx"]["state"] == "busy"
    refused = NXError(
        "NX_BRIDGE_UNAVAILABLE", "refused.", details={"mutation_outcome": "not_started"}
    )
    assert "retry when nx_get_status shows NX free" in p.explain_stall(refused).hint
