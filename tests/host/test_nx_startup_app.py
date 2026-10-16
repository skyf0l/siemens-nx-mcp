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

BOOTSTRAP = Path(__file__).resolve().parents[2] / "nx_app" / "startup" / "nx_mcp_startup.py"


def load(tmp_path, monkeypatch, dev=True):
    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("NX_MCP_DEV", "1" if dev else "0")
    spec = importlib.util.spec_from_file_location("nx_mcp_startup_under_test", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHost:
    def __init__(self):
        self.mode, self.stopped, self.ticks = "auto", False, 0
        self.last_error = None
        self.notice = None
        self.panel = types.SimpleNamespace(hwnd=4242)

    def _set_notice(self, text):
        self.notice = text

    def tick(self):
        self.ticks += 1

    def stop(self):
        self.stopped = True


def fake_interactive(started, fail=False):
    module = types.ModuleType("nx_mcp.interactive")
    module._host = None
    module.import_nxopen_namespaces = lambda: []

    def start(workspace, descriptor, resident, *, dev, driver):
        if fail:
            raise SyntaxError("bad edit")
        assert resident and driver == "startup"
        module._host = FakeHost()
        started.append(dev)

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
    assert started == [True] and host.ticks == 1 and host.mode == "auto"
    state = json.loads((tmp_path / ".nx-mcp" / "dev-state.json").read_text())
    assert state["ok"] and state["driver"] == "startup" and state["generation"] == 1


def test_dev_restart_starts_a_new_host_and_survives_bad_code(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(app, "purge_nx_mcp", lambda: None)
    monkeypatch.setattr(app, "nx_has_main_window", lambda: True)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started))
    monkeypatch.setattr(app.importlib, "import_module", lambda name: sys.modules[name])
    app.tick()
    first = app.state["host"]
    app.RESTART_FILE.write_text("")
    app.tick()
    assert first.stopped and started == [True, True] and app.state["host"] is not first
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
    assert started == [False]


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


JOURNALS = BOOTSTRAP.parents[1] / "journals"
JOURNAL = JOURNALS / "start.py"


def journal_ready(app, monkeypatch, started, fail=False):
    """No Win32 timer window in tests: arm() only records its loader."""
    monkeypatch.setattr(app, "purge_nx_mcp", lambda: None)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started, fail))
    monkeypatch.setattr(app.importlib, "import_module", lambda name: sys.modules[name])
    monkeypatch.setattr(app, "nx_has_main_window", lambda: True)
    monkeypatch.setattr(app, "foreign_bootstrap", lambda: False)
    shown = []
    monkeypatch.setattr(app, "show_window", shown.append)

    def arm(loader):
        if not app.state["armed"]:
            app.state.update(armed=True, loader=loader)

    monkeypatch.setattr(app, "arm", arm)
    return shown


@pytest.mark.parametrize("name", ["start.py", "start_2406.py", "start_2606.py"])
def test_journal_line_one_runs_in_the_resident_interpreter(name):
    assert (JOURNALS / name).read_text(encoding="utf-8").splitlines()[0] == "# nx: threaded"


def test_play_starts_one_host_and_then_reports_it_running(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    shown = journal_ready(app, monkeypatch, started)
    assert "started" in app.play() and started == [True]
    host = app.state["host"]
    report = app.play()  # A second play: no second bridge.
    assert report == "NX MCP bridge is already running in this NX (started by the journal)."
    assert started == [True] and app.state["host"] is host
    assert host.notice == report and shown == [4242]  # Said on the panel, panel brought up.
    host.stop()  # Stop bridge on the panel: playing again starts a fresh host.
    assert "started" in app.play() and len(started) == 2 and app.state["host"] is not host


def test_play_finds_the_host_of_the_startup_app(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    journal_ready(app, monkeypatch, started)
    # Leave sys.modules as found: absent before the test, absent after it.
    monkeypatch.setitem(sys.modules, app.MODULE_NAME, None)
    monkeypatch.delitem(sys.modules, app.MODULE_NAME)
    app.startup()  # NX loads the app at launch (under some name) ...
    app.tick()  # ... and its timer starts the host once NX's window is up.
    registered = sys.modules[app.MODULE_NAME]  # What start.py imports.
    assert registered.state is app.state
    report = registered.play()
    assert report.endswith("(started by the startup app).") and started == [True]


def test_play_defers_to_a_bootstrap_in_another_interpreter(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    shown = journal_ready(app, monkeypatch, started)
    monkeypatch.setattr(app, "foreign_bootstrap", lambda: True)
    monkeypatch.setattr(app, "panel_windows", lambda: [7, 8])
    assert "already running" in app.play()
    assert not started and not app.state["armed"] and shown == [7, 8]


def test_play_reports_a_host_that_cannot_start(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    journal_ready(app, monkeypatch, started, fail=True)
    with pytest.raises(RuntimeError, match="bad edit"):
        app.play()
    assert "bad edit" in app.ERROR_FILE.read_text() and app.state["host"] is None


@pytest.mark.parametrize("name", ["start.py", "start_2406.py", "start_2606.py"])
def test_journal_plays_the_registered_bootstrap(monkeypatch, capsys, name):
    import runpy

    calls = []
    fake = types.ModuleType("nx_mcp_startup")  # One NX loaded before this checkout changed.
    fake.play = lambda: calls.append(1) or "already running"
    monkeypatch.setitem(sys.modules, "nx_mcp_startup", fake)
    runpy.run_path(str(JOURNALS / name))
    assert calls == [1] and capsys.readouterr().out.strip() == "already running"


def test_journal_prints_the_environment_before_playing(monkeypatch, capsys):
    import runpy

    fake = types.ModuleType("nx_mcp_startup")
    fake.python_problem = lambda: None
    fake.environment = lambda: ["NX build: 2512.1 (v2512, not tested)", "Python: 3.12.1"]
    fake.play = lambda: "NX MCP bridge started"
    monkeypatch.setitem(sys.modules, "nx_mcp_startup", fake)
    runpy.run_path(str(JOURNAL))
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "NX build: 2512.1 (v2512, not tested)",
        "Python: 3.12.1",
        "NX MCP bridge started",
    ]


def test_journal_refuses_an_old_python_before_anything_starts(monkeypatch, capsys):
    import runpy

    calls = []
    fake = types.ModuleType("nx_mcp_startup")
    fake.python_problem = lambda: "NX MCP needs Python 3.10 or newer; this NX runs Python 3.9.7."
    fake.environment = lambda: calls.append("environment") or []
    fake.play = lambda: calls.append("play")
    monkeypatch.setitem(sys.modules, "nx_mcp_startup", fake)
    with pytest.raises(RuntimeError, match="3.10 or newer"):
        runpy.run_path(str(JOURNAL))
    assert not calls and "Python 3.9.7" in capsys.readouterr().out


def test_python_problem_names_the_minimum_and_the_found_version(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    assert app.python_problem((3, 10, 0)) is None and app.python_problem((3, 12, 4)) is None
    message = app.python_problem((3, 9, 7, "final", 0))
    assert "Python 3.10 or newer" in message and "Python 3.9.7" in message
    pyproject = (BOOTSTRAP.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject  # The same minimum as MINIMUM_PYTHON.
    assert app.MINIMUM_PYTHON == (3, 10)


def test_play_and_the_startup_host_refuse_an_old_python(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    started = []
    journal_ready(app, monkeypatch, started)
    monkeypatch.setattr(app, "python_problem", lambda: "NX MCP needs Python 3.10 or newer")
    with pytest.raises(RuntimeError, match="3.10 or newer"):
        app.play()
    assert not app.state["armed"]
    app.start_host()  # The startup app's path: recorded, never raised.
    assert not started and "3.10 or newer" in app.ERROR_FILE.read_text()


class FakeSession:
    def __init__(self, release, variable):
        self.FullReleaseNumber, self.variable = release, variable

    def GetEnvironmentVariableValue(self, name):
        return self.variable


def fake_nxopen(release, variable):
    module = types.ModuleType("NXOpen")
    session = FakeSession(release, variable)
    module.Session = types.SimpleNamespace(GetSession=lambda: session)
    return module


@pytest.mark.parametrize(
    ("release", "variable", "status"),
    [("2406.9160", "v2406", "(v2406, tested)"), ("2512.3000", "v2512", "(v2512, not tested")],
)
def test_environment_reports_build_python_and_namespaces(
    tmp_path, monkeypatch, release, variable, status
):
    app = load(tmp_path, monkeypatch)
    from nx_mcp.core import evidence

    monkeypatch.setattr(evidence, "load", lambda: {"nx_some_tool": {"v2406": {}}})
    monkeypatch.setitem(sys.modules, "NXOpen", fake_nxopen(release, variable))
    interactive = fake_interactive([])
    interactive.NXOPEN_NAMESPACES = ("NXOpen", "NXOpen.Features", "NXOpen.PartFamily")
    interactive.import_nxopen_namespaces = lambda: ["NXOpen.PartFamily (No module named x)"]
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", interactive)
    build, python, namespaces = app.environment()
    assert build.startswith(f"NX build: {release} {status}")
    assert python == f"Python: {sys.version.split()[0]}"
    assert namespaces.startswith("NXOpen namespaces: 2 of 3 imported; missing")
    assert namespaces.endswith("NXOpen.PartFamily (No module named x)")
    interactive.import_nxopen_namespaces = lambda: []
    assert app.environment()[2] == "NXOpen namespaces: all 3 imported"


def test_environment_never_raises(tmp_path, monkeypatch):
    app = load(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "NXOpen", None)  # import NXOpen fails
    broken = types.ModuleType("nx_mcp.interactive")  # lacks the namespace helpers
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", broken)
    build, python, namespaces = app.environment()
    assert build.startswith("NX build: unknown (ModuleNotFoundError")
    assert python.startswith("Python: ") and namespaces.startswith("NXOpen namespaces: not checked")


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="named kernel objects")


@windows_only
def test_the_marker_is_seen_from_any_copy_of_the_bootstrap(tmp_path, monkeypatch):
    import _winapi

    first, second = load(tmp_path, monkeypatch), load(tmp_path, monkeypatch)
    name = f"{first.MARKER}-test-{tmp_path.name}"
    for copy in (first, second):
        monkeypatch.setattr(copy, "MARKER", name)
        monkeypatch.setattr(copy, "foreign_bootstrap_window", lambda: False)
    assert not first.marker_exists() and not second.foreign_bootstrap()
    handle = first.create_marker()
    try:
        first.state["armed"] = True
        assert second.marker_exists() and second.foreign_bootstrap()
        assert not first.foreign_bootstrap()  # Its own mark is not a foreign one.
    finally:
        _winapi.CloseHandle(handle)
    assert not second.marker_exists()  # Gone with its last handle, as with NX's process.


@windows_only
def test_a_bootstrap_in_another_interpreter_never_starts_a_second_bridge(tmp_path, monkeypatch):
    """Two copies of the module stand for two interpreters of one NX: separate
    sys.modules and state, one process."""
    import _winapi

    hosting, other = load(tmp_path, monkeypatch), load(tmp_path, monkeypatch)
    name = f"{hosting.MARKER}-test-{tmp_path.name}"
    started, armed, shown = [], [], []
    monkeypatch.setattr(other, "purge_nx_mcp", lambda: None)
    monkeypatch.setitem(sys.modules, "nx_mcp.interactive", fake_interactive(started))
    monkeypatch.setattr(other.importlib, "import_module", lambda name: sys.modules[name])
    monkeypatch.setattr(other, "foreign_bootstrap_window", lambda: False)
    monkeypatch.setattr(other, "arm", armed.append)
    monkeypatch.setattr(other, "show_window", shown.append)
    monkeypatch.setattr(other, "panel_windows", lambda: [9])
    monkeypatch.setitem(sys.modules, other.MODULE_NAME, None)
    monkeypatch.delitem(sys.modules, other.MODULE_NAME)
    for copy in (hosting, other):
        monkeypatch.setattr(copy, "MARKER", name)
    handle = hosting.create_marker()  # What hosting.arm() does first.
    try:
        assert other.play().endswith("(started by another bootstrap).")
        other.startup()  # A startup app loaded late in another interpreter defers too.
        assert not started and not armed and shown == [9]
    finally:
        _winapi.CloseHandle(handle)


def test_workspace_defaults_to_the_home_folder(tmp_path, monkeypatch):
    from nx_mcp.workspace import default_workspace

    monkeypatch.delenv("NX_MCP_WORKSPACE")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    spec = importlib.util.spec_from_file_location("nx_mcp_startup_home", BOOTSTRAP)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    assert app.WORKSPACE == str(default_workspace()) == str(tmp_path / "NX_MCP_WORKSPACE")
