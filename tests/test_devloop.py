"""Development journal loop: reload from disk, keep control choice, survive bad code."""

import json

from nx_mcp.devloop import run_dev


class FakeHost:
    def __init__(self, agent_control):
        self.mode = "manual"
        self.auto_start = agent_control
        self.stopped = False
        self.ticks = 0
        self.last_error = None
        self.had_agent = False

    def tick(self):
        self.ticks += 1
        if self.auto_start:
            self.mode, self.auto_start, self.had_agent = "agent", False, True

    def stop(self):
        self.mode, self.stopped = "manual", True


class FakeInteractive:
    def __init__(self):
        self.started = []
        self._host = None

    def import_nxopen_namespaces(self):
        return []

    def start(self, workspace, descriptor, resident, *, agent_control, dev):
        assert resident and dev
        self._host = FakeHost(agent_control)
        self.started.append(self._host)


def scripted(steps):
    """A sleep() that runs one scripted action per loop iteration."""
    iterator = iter(steps)

    def sleep(_):
        next(iterator, lambda: None)()

    return sleep


def state(tmp_path):
    return json.loads((tmp_path / ".nx-mcp" / "dev-state.json").read_text())


def touch(tmp_path, name):
    return lambda: (tmp_path / ".nx-mcp" / name).write_text("")


def test_agent_gets_control_reloads_keep_it_and_exit_stops(tmp_path):
    fake, purges = FakeInteractive(), []
    steps = [
        lambda: None,
        touch(tmp_path, "dev-restart"),
        lambda: None,
        touch(tmp_path, "dev-exit"),
    ]
    run_dev(
        tmp_path,
        "d.json",
        pump=lambda: None,
        sleep=scripted(steps),
        importer=lambda name: fake,
        purge=lambda: purges.append(1),
    )
    first, second = fake.started
    assert first.stopped and second.stopped and len(purges) == 2
    assert first.mode == "manual" and second.ticks >= 1
    assert second.stopped and state(tmp_path)["running"] is False
    assert state(tmp_path)["generation"] == 2


def test_taking_control_back_survives_a_restart(tmp_path):
    fake = FakeInteractive()

    def engineer_takes_control():
        host = fake._host
        host.mode, host.auto_start = "manual", False
        touch(tmp_path, "dev-restart")()

    steps = [lambda: None, engineer_takes_control, lambda: None, touch(tmp_path, "dev-exit")]
    run_dev(
        tmp_path,
        "d",
        pump=lambda: None,
        sleep=scripted(steps),
        importer=lambda n: fake,
        purge=lambda: None,
    )
    assert fake.started[1].mode == "manual"  # Never re-grabbed after the engineer's choice.


def test_broken_code_is_reported_and_the_next_reload_recovers(tmp_path):
    fake, imports = FakeInteractive(), []

    def importer(name):
        imports.append(name)
        if len(imports) == 1:
            raise SyntaxError("bad edit in hardened.py")
        return fake

    pumped = []

    def check_failure_then_restart():
        assert state(tmp_path)["ok"] is False and "bad edit" in state(tmp_path)["error"]
        error = (tmp_path / ".nx-mcp" / "resident-error.txt").read_text()
        assert "SyntaxError" in error
        touch(tmp_path, "dev-restart")()

    steps = [check_failure_then_restart, lambda: None, touch(tmp_path, "dev-exit")]
    run_dev(
        tmp_path,
        "d",
        pump=lambda: pumped.append(1),
        sleep=scripted(steps),
        importer=importer,
        purge=lambda: None,
    )
    assert pumped  # NX kept receiving messages while the code was broken.
    assert len(fake.started) == 1 and fake.started[0].had_agent
    assert not (tmp_path / ".nx-mcp" / "resident-error.txt").exists()


def test_stop_bridge_ends_the_development_session(tmp_path):
    fake = FakeInteractive()
    steps = [lambda: fake._host.stop()]
    host = run_dev(
        tmp_path,
        "d",
        pump=lambda: None,
        sleep=scripted(steps),
        importer=lambda n: fake,
        purge=lambda: None,
    )
    assert host is fake.started[0] and state(tmp_path)["running"] is False


def test_start_options_set_agent_control_and_dev_title(monkeypatch):
    from nx_mcp import interactive

    def init(self, *args):
        self.mode, self.auto_start, self.stopped = "manual", False, False
        self.status = lambda: {}

    monkeypatch.setattr(interactive, "_host", None)
    monkeypatch.setattr(interactive.InteractiveHost, "__init__", init)
    interactive.start("w", "d", True, agent_control=True, dev=True)
    host = interactive._host
    assert host.auto_start and host.dev
    host.requested_mode = host.running_method = None
    host.waiting_count = 0
    host.panel_text = lambda: ""
    assert host.panel_state()[1] == "NX MCP DEV — You have control"
