"""The runner holds the dev lock for a whole run or call and presents one caller per owner."""

import asyncio
import contextlib
import functools
import itertools
import json
import time
import weakref

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.acceptance import runner, sidecar_launch
from nx_mcp.server import create_server
from nx_mcp.sidecar import proxy
from nx_mcp.workspace import Workspace
from tests.acceptance.test_runner import SCENARIO, FakeNX


def lock_state(workspace):
    try:
        return json.loads((workspace / ".nx-mcp" / "dev.lock").read_text(encoding="utf-8"))
    except OSError:
        return None


class WatchedNX(FakeNX):
    """FakeNX noting the dev lock and the caller of every bridge call."""

    def __init__(self, workspace, on_call=None):
        super().__init__()
        self.workspace = workspace
        self.on_call = on_call
        self.locks = []
        self.callers = []

    async def call(self, method, params):
        self.locks.append(lock_state(self.workspace))
        self.callers.append((params.get("caller") or {}).get("id"))
        if self.on_call:
            self.on_call(method, params)
        return await super().call(method, params)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Two scenarios, a workspace, a fast heartbeat and a fake sidecar per process."""
    root = tmp_path / "repo"
    (root / "acceptance" / "modeling").mkdir(parents=True)
    for name in ("one", "two"):
        path = root / "acceptance" / "modeling" / f"{name}.json"
        path.write_text(json.dumps(SCENARIO), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "SCENARIOS", root / "acceptance")
    monkeypatch.setenv("NX_MCP_DEV", "1")
    monkeypatch.setattr(runner, "DevLock", functools.partial(runner.DevLock, beat=0.02))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = {"nx": WatchedNX(workspace), "callers": []}

    @contextlib.asynccontextmanager
    async def sidecar(ws, caller=None):
        # A fresh sidecar process: its own session numbers and, as sidecar_launch
        # sets it, the runner's caller id as the process id.
        state["callers"].append(caller)
        monkeypatch.setattr(proxy, "PROCESS_ID", caller or "sc-random")
        monkeypatch.setattr(proxy, "_NUMBERS", itertools.count(1))
        monkeypatch.setattr(proxy, "_SESSIONS", weakref.WeakKeyDictionary())
        server = create_server(state["nx"], Workspace(ws), surface="agent")
        async with create_connected_server_and_client_session(server) as session:
            yield session

    async def version(session):
        return ("v2406", "NX 2406.1700")

    monkeypatch.setattr(runner, "sidecar", sidecar)
    monkeypatch.setattr(runner, "nx_version", version)
    state["workspace"] = workspace
    state["evidence"] = tmp_path / "evidence"
    return state


async def test_a_run_holds_one_lock_for_all_its_scenarios(setup):
    workspace = setup["workspace"]
    nx = setup["nx"]
    code = await runner.run(
        ["modeling"],
        workspace=workspace,
        owner="tester",
        evidence_root=setup["evidence"],
        beside_engineer=True,
    )
    assert code == 0
    assert [m for m, _ in nx.calls].count("nx_create_part") == 2  # both scenarios ran
    # Held by the same acquisition at every NX call of both scenarios, then released.
    assert {lock["owner"] for lock in nx.locks} == {"tester"}
    assert len({lock["acquired_at"] for lock in nx.locks}) == 1
    assert lock_state(workspace) is None
    # One caller id for the whole run: the owner's.
    assert setup["callers"] == ["dev-tester"]
    assert set(nx.callers) == {"dev-tester.1"}


async def test_a_run_stops_when_its_lock_was_taken(setup, capsys):
    workspace = setup["workspace"]

    def steal(method, params):
        if method == "nx_create_part":
            stolen = {"owner": "other", "acquired_at": time.time(), "refreshed_at": time.time()}
            (workspace / ".nx-mcp" / "dev.lock").write_text(json.dumps(stolen))

    setup["nx"].on_call = steal
    code = await runner.run(
        ["modeling"],
        workspace=workspace,
        owner="tester",
        evidence_root=setup["evidence"],
        beside_engineer=True,
    )
    assert code == 1
    assert "ABORTED before acceptance/modeling/two.json" in capsys.readouterr().out
    assert [m for m, _ in setup["nx"].calls].count("nx_create_part") == 1
    assert lock_state(workspace)["owner"] == "other"  # never released or re-acquired


async def test_a_run_releases_its_lock_on_an_error(setup, monkeypatch):
    async def broken(session):
        raise RuntimeError("bridge gone")

    monkeypatch.setattr(runner, "nx_version", broken)
    with pytest.raises(Exception, match="bridge gone|TaskGroup"):  # anyio may group it
        await runner.run(["modeling"], workspace=setup["workspace"], owner="tester")
    assert lock_state(setup["workspace"]) is None


def test_a_run_releases_its_lock_on_ctrl_c(setup, monkeypatch):
    async def interrupted(session):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "nx_version", interrupted)
    with pytest.raises(BaseException) as raised:  # anyio may group it
        asyncio.run(runner.run(["modeling"], workspace=setup["workspace"], owner="tester"))
    group = getattr(raised.value, "exceptions", None)
    assert raised.errisinstance(KeyboardInterrupt) or "KeyboardInterrupt" in repr(group)
    assert lock_state(setup["workspace"]) is None


async def test_calls_of_one_owner_are_one_caller(setup):
    workspace = setup["workspace"]
    nx = setup["nx"]
    step = [{"call": "nx_list_objects", "args": {"kind": "body"}}]
    assert await runner.call(step, workspace=workspace, owner="tester") == 0
    assert await runner.call(step, workspace=workspace, owner="tester") == 0
    assert setup["callers"] == ["dev-tester", "dev-tester"]
    assert set(nx.callers) == {"dev-tester.1"}  # the same id in both sidecars
    assert {lock["owner"] for lock in nx.locks} == {"tester"}
    assert lock_state(workspace) is None


def test_the_heartbeat_keeps_the_lock_fresh_and_notices_a_takeover(tmp_path):
    lock = runner.DevLock(tmp_path, "tester", wait=0, beat=0.02, echo=None)
    with lock:
        data = json.loads(lock.path.read_text())
        data["refreshed_at"] = 0
        lock.path.write_text(json.dumps(data))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and lock_state(tmp_path)["refreshed_at"] == 0:
            time.sleep(0.02)
        assert time.time() - lock_state(tmp_path)["refreshed_at"] < 5
        lock.path.write_text(json.dumps({"owner": "other", "refreshed_at": time.time()}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and lock.lost is None:
            time.sleep(0.02)
        assert "held by other" in lock.lost
        with pytest.raises(runner.LockLost):
            lock.refresh()
    assert lock_state(tmp_path)["owner"] == "other"  # release keeps the other's lock


def test_a_stale_lock_is_taken_over_by_the_runner(tmp_path):
    lines = []
    old = {"owner": "gone", "acquired_at": 0, "refreshed_at": time.time() - 3600}
    path = tmp_path / ".nx-mcp" / "dev.lock"
    path.parent.mkdir()
    path.write_text(json.dumps(old))
    with runner.DevLock(tmp_path, "tester", wait=0, echo=lines.append):
        assert lock_state(tmp_path)["owner"] == "tester"
    assert lines and "stale lock of gone" in lines[0]
    assert lock_state(tmp_path) is None


def test_caller_ids_and_the_sidecar_launch(monkeypatch):
    assert runner.caller_id("holes") == runner.caller_id("holes") == "dev-holes"
    assert runner.caller_id("a b/c") == "dev-abc"
    assert runner.caller_id("$$") == "dev-anonymous"
    assert runner.sidecar_args() == ["-m", "nx_mcp.server"]
    assert runner.sidecar_args("dev-x") == ["-m", "nx_mcp.acceptance.sidecar_launch", "dev-x"]
    started = []
    monkeypatch.setattr(proxy, "PROCESS_ID", proxy.PROCESS_ID)
    monkeypatch.setattr("nx_mcp.server.main", lambda: started.append(proxy.PROCESS_ID))
    sidecar_launch.main(["dev-holes"])
    assert started == ["dev-holes"]
    with pytest.raises(SystemExit):
        sidecar_launch.main(["bad id!"])
