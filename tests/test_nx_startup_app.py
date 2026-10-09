"""The NX startup application bootstrap (nx_app/startup/nx_mcp_startup.py).

NX keeps this module loaded for the whole session ("# nx: threaded") and ticks
the host from a timer on its UI thread. These tests load it with a fake
nx_mcp.interactive; native window/timer calls are not exercised here.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "nx_app" / "startup" / "nx_mcp_startup.py"


def load(tmp_path, monkeypatch, dev=True):
    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("NX_MCP_DEV", "1" if dev else "0")
    spec = importlib.util.spec_from_file_location("nx_mcp_startup_under_test", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHost:
    def __init__(self, agent_control):
        self.mode, self.auto_start, self.stopped, self.ticks = "manual", agent_control, False, 0
        self.last_error = None

    def tick(self):
        self.ticks += 1
        if self.auto_start:
            self.mode, self.auto_start = "agent", False

    def stop(self):
        self.mode, self.stopped = "manual", True


def fake_interactive(started, fail=False):
    module = types.ModuleType("nx_mcp.interactive")
    module._host = None
    module.import_nxopen_namespaces = lambda: []

    def start(workspace, descriptor, resident, *, agent_control, dev, driver):
        if fail:
            raise SyntaxError("bad edit")
        assert resident and driver == "startup"
        module._host = FakeHost(agent_control)
        started.append((dev, agent_control))

    module.start = start
    return module


def test_line_one_keeps_the_module_loaded():
    assert BOOTSTRAP.read_text(encoding="utf-8").splitlines()[0] == "# nx: threaded"


def test_host_starts_once_nx_has_a_window_and_ticks(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(app, "purge_nx_mcp", lambda: None)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started))
    monkeypatch.setattr(app.importlib, "import_module", lambda name: sys.modules[name])
    monkeypatch.setattr(app, "nx_has_main_window", lambda: False)
    app.tick()
    assert app.state["host"] is None and not started  # Waits for NX's main window.
    monkeypatch.setattr(app, "nx_has_main_window", lambda: True)
    app.tick()
    app.tick()
    host = app.state["host"]
    assert started == [(True, True)] and host.ticks == 1 and host.mode == "agent"
    state = json.loads((tmp_path / ".nx-mcp" / "dev-state.json").read_text())
    assert state["ok"] and state["driver"] == "startup" and state["generation"] == 1


def test_dev_restart_keeps_the_engineers_choice_and_survives_bad_code(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(app, "purge_nx_mcp", lambda: None)
    monkeypatch.setattr(app, "nx_has_main_window", lambda: True)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started))
    monkeypatch.setattr(app.importlib, "import_module", lambda name: sys.modules[name])
    app.tick()
    first = app.state["host"]
    first.mode, first.auto_start = "manual", False  # The engineer took control back.
    app.RESTART_FILE.write_text("")
    app.tick()
    assert first.stopped and started[-1] == (True, False)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started, fail=True))
    app.RESTART_FILE.write_text("")
    app.tick()
    assert app.state["host"] is None and "bad edit" in app.ERROR_FILE.read_text()
    state = json.loads(app.STATE_FILE.read_text())
    assert state["ok"] is False and state["generation"] == 3


@pytest.mark.parametrize("dev", [False])
def test_production_mode_starts_with_the_engineer_in_control(tmp_path, monkeypatch, dev):
    app = load(tmp_path, monkeypatch, dev=dev)
    started = []
    monkeypatch.setattr(app, "purge_nx_mcp", lambda: None)
    monkeypatch.setattr(app, "nx_has_main_window", lambda: True)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started))
    monkeypatch.setattr(app.importlib, "import_module", lambda name: sys.modules[name])
    app.tick()
    app.RESTART_FILE.write_text("")
    app.tick()  # Reload requests are ignored outside development mode.
    assert started == [(False, False)]


def test_failing_restart_backs_off_records_once_and_gives_up(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(app, "purge_nx_mcp", lambda: None)
    monkeypatch.setattr(app, "nx_has_main_window", lambda: True)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started))
    monkeypatch.setattr(app.importlib, "import_module", lambda name: sys.modules[name])
    app.tick()
    host = app.state["host"]
    stops = []

    def failing_stop():
        stops.append(1)
        raise RuntimeError("stop failed")

    host.stop = failing_stop
    clock = {"now": 100.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["now"])
    writes = []
    real_write = Path.write_text

    def counting_write(self, *args, **kwargs):
        if self == app.ERROR_FILE:
            writes.append(1)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", counting_write)
    app.RESTART_FILE.write_text("")
    app.tick()
    for _ in range(20):  # 50 ms ticks inside the back-off window do not retry.
        app.tick()
    assert len(stops) == 1 and host.ticks == 20  # The current host keeps ticking.
    for _ in range(app.RESTART_ATTEMPTS - 1):
        clock["now"] += app.RESTART_BACKOFF_MAX
        app.tick()
    assert len(stops) == app.RESTART_ATTEMPTS and len(writes) == 1
    assert not app.RESTART_FILE.exists()  # The request is dropped, not retried forever.
    state = json.loads(app.STATE_FILE.read_text())
    assert state["ok"] is False and "stop failed" in state["error"] and state["generation"] == 2
    clock["now"] += app.RESTART_BACKOFF_MAX
    app.tick()
    assert len(stops) == app.RESTART_ATTEMPTS
