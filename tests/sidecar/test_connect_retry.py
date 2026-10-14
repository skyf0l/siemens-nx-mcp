"""A burst of calls waits in the bridge's listen backlog; a refused connect (nothing sent)
is retried briefly while NX's host is alive; the failure says why truthfully."""

import asyncio
import json
import socket
import time
from types import SimpleNamespace as NS

import pytest

from nx_mcp.bridge import (
    LISTEN_BACKLOG,
    BridgeClient,
    BridgeServer,
    _BridgeTCPServer,
    _ThreadedBridgeTCPServer,
)
from nx_mcp.core.errors import NXError
from nx_mcp.host import dialogs, heartbeat
from nx_mcp.sidecar import proxy as proxy_module
from nx_mcp.sidecar.proxy import CatalogueProxy

PID = 4242


def test_the_bridge_holds_a_burst_of_connections_it_has_not_accepted():
    assert _BridgeTCPServer.request_queue_size == _ThreadedBridgeTCPServer.request_queue_size
    assert LISTEN_BACKLOG >= 64
    server = BridgeServer(lambda m, p: {}, token="t" * 64, concurrent_requests=True)
    server.start(accept_thread=False)  # the interactive host: nobody accepts while NX is busy
    clients = []
    try:
        for _ in range(12):  # socketserver's default backlog (5) refused some of these
            clients.append(socket.create_connection(("127.0.0.1", server.port), timeout=5))
    finally:
        for c in clients:
            c.close()
        server.stop()
    assert len(clients) == 12


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_refused_connect_says_nothing_was_sent():
    client = BridgeClient("127.0.0.1", _closed_port(), token="t" * 64, timeout=5)
    with pytest.raises(NXError) as refused:
        asyncio.run(client.call("nx_get_status", {}))
    error = refused.value
    assert error.code == "NX_BRIDGE_UNAVAILABLE" and "Nothing ran" in error.message
    assert error.details == {"mutation_outcome": "not_started", "connect": "refused"}


class Flaky:
    """A bridge refusing the first ``refusals`` connects."""

    def __init__(self, refusals, error=None):
        self.refusals, self.calls, self.error = refusals, 0, error

    async def call(self, method, params):
        self.calls += 1
        if self.calls <= self.refusals:
            raise self.error or NXError(
                "NX_BRIDGE_UNAVAILABLE",
                "refused",
                details={"mutation_outcome": "not_started", "connect": "refused"},
            )
        return {"ok": True, "tool": method}


def _proxy(tmp_path, bridge, monkeypatch):
    p = CatalogueProxy(bridge, NS(root=tmp_path), None)
    monkeypatch.setattr(p, "_descriptor", lambda: NS(pid=PID))
    monkeypatch.setattr(proxy_module.asyncio, "sleep", _no_sleep)
    return p


async def _no_sleep(_seconds):
    return None


def _beat(tmp_path, **fields):
    folder = tmp_path / ".nx-mcp"
    folder.mkdir(exist_ok=True)
    row = {"pid": PID, "sampled_at": time.time(), **fields}
    (folder / heartbeat.STATE_FILE).write_text(json.dumps(row), encoding="utf-8")


def test_a_refused_connect_is_retried_while_the_host_heartbeat_is_fresh(tmp_path, monkeypatch):
    _beat(tmp_path)
    bridge = Flaky(2)
    p = _proxy(tmp_path, bridge, monkeypatch)
    assert asyncio.run(p._send("nx_create_block", {}))["ok"] and bridge.calls == 3


def test_no_retry_without_a_live_host_or_after_the_request_left(tmp_path, monkeypatch):
    stale = Flaky(1)  # no heartbeat at all: NX's host is not known to run
    with pytest.raises(NXError):
        asyncio.run(_proxy(tmp_path, stale, monkeypatch)._send("nx_create_block", {}))
    assert stale.calls == 1
    _beat(tmp_path)
    unknown = NXError("NX_OUTCOME_UNKNOWN", "sent", details={"mutation_outcome": "unknown"})
    sent = Flaky(1, unknown)  # the request reached NX: never resent
    with pytest.raises(NXError):
        asyncio.run(_proxy(tmp_path, sent, monkeypatch)._send("nx_create_block", {}))
    assert sent.calls == 1
    busy = Flaky(1, NXError("NX_BUSY", "queued", details={"mutation_outcome": "not_started"}))
    with pytest.raises(NXError):
        asyncio.run(_proxy(tmp_path, busy, monkeypatch)._send("nx_create_block", {}))
    assert busy.calls == 1


def test_retries_stop_after_the_retry_window(tmp_path, monkeypatch):
    _beat(tmp_path)
    bridge = Flaky(1000)
    p = _proxy(tmp_path, bridge, monkeypatch)
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(proxy_module.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(NXError):
        asyncio.run(p._send("nx_create_block", {}))
    assert 1 < bridge.calls < 10


def test_a_host_reload_is_said_only_while_one_is_under_way(tmp_path, monkeypatch):
    monkeypatch.setattr(dialogs, "process_alive", lambda pid: True)
    monkeypatch.setattr(dialogs, "scan", lambda pid, **kw: [])
    p = _proxy(tmp_path, Flaky(0), monkeypatch)
    down = NXError("NX_BRIDGE_UNAVAILABLE", "refused", details={"mutation_outcome": "not_started"})
    assert "reloading" not in p.explain_stall(down).message
    assert "did not accept the connection (busy)" in p.explain_stall(down).message
    folder = tmp_path / ".nx-mcp"
    folder.mkdir(exist_ok=True)
    (folder / heartbeat.RESTART_FILE).write_text("", encoding="utf-8")
    told = p.explain_stall(down)
    assert "is reloading" in told.message and told.details["nx"]["reloading"] is True
    (folder / heartbeat.RESTART_FILE).unlink()
    state = {"generation": 1, "running": True, "ok": True, "updated_at": time.time()}
    (folder / heartbeat.DEV_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    assert heartbeat.reload_state(tmp_path) is None  # NX's own first start is no reload
    state["generation"] = 3
    (folder / heartbeat.DEV_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    assert heartbeat.reload_state(tmp_path)["generation"] == 3
    later = time.time() + heartbeat.RELOAD_SECONDS + 1
    assert heartbeat.reload_state(tmp_path, now=later) is None
    monkeypatch.setattr(dialogs, "process_alive", lambda pid: False)
    assert p.explain_stall(down).message.startswith(f"NX (pid {PID}) is not running")


def test_heartbeat_freshness(tmp_path):
    assert not heartbeat.fresh(tmp_path, PID)
    _beat(tmp_path)
    assert heartbeat.fresh(tmp_path, PID) and not heartbeat.fresh(tmp_path, PID + 1)
    _beat(tmp_path, sampled_at=time.time() - 600)
    assert not heartbeat.fresh(tmp_path, PID)
    _beat(tmp_path, sampled_at=time.time() - 600, running_method="nx_create_extrude")
    assert heartbeat.fresh(tmp_path, PID)  # a call holds the UI thread: the file stands
