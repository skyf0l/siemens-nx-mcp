"""scripts/dev_reload.py and scripts/dev_python.py: only the development-lock holder."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dev_lock, dev_reload, dev_python = load("dev_lock"), load("dev_reload"), load("dev_python")


@pytest.fixture
def workspace(tmp_path):
    state = tmp_path / ".nx-mcp"
    state.mkdir()
    (state / "dev-state.json").write_text(
        json.dumps({"running": True, "generation": 1, "updated_at": time.time()}),
        encoding="utf-8",
    )
    return tmp_path


def restart_asked(workspace):
    return (workspace / ".nx-mcp" / "dev-restart").exists()


@pytest.mark.parametrize("owner", [[], ["--owner", "b"]])
def test_reload_is_refused_unless_the_owner_holds_the_lock(workspace, capsys, owner):
    assert dev_lock.try_acquire(dev_lock.lock_path(str(workspace)), "a")[0]
    code = dev_reload.main(["--workspace", str(workspace), "--timeout", "0.1", *owner])
    assert code == dev_reload.LOCK_REFUSED and not restart_asked(workspace)
    out = capsys.readouterr().out
    assert "NX untouched" in out
    if owner:
        assert "not held by b" in out and "held by a" in out and "acquire --owner b" in out


def test_the_holder_reloads_and_its_lock_is_refreshed(workspace):
    path = dev_lock.lock_path(str(workspace))
    dev_lock.try_acquire(path, "a")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["refreshed_at"] -= 100
    path.write_text(json.dumps(data), encoding="utf-8")
    code = dev_reload.main(["--workspace", str(workspace), "--timeout", "0.1", "--owner", "a"])
    assert code == 2 and restart_asked(workspace)  # asked NX; no new generation here
    assert dev_lock.age(dev_lock.read(path)) < 50


def test_force_unlocked_skips_the_check_with_a_warning(workspace, capsys):
    dev_lock.try_acquire(dev_lock.lock_path(str(workspace)), "a")
    code = dev_reload.main(["--workspace", str(workspace), "--timeout", "0.1", "--force-unlocked"])
    assert code == 2 and restart_asked(workspace)
    assert "WARNING: --force-unlocked" in capsys.readouterr().err


class Client:
    calls: list = []

    def __init__(self, descriptor, timeout):
        self.timeout = timeout

    async def call(self, method, params):
        Client.calls.append((method, params, self.timeout))
        return {"status": "success", "stdout": "hi\n", "result": "None"}


def test_a_probe_is_refused_unless_the_owner_holds_the_lock(workspace, monkeypatch, capsys):
    Client.calls = []
    monkeypatch.setattr(dev_python, "DescriptorBridgeClient", Client)
    dev_lock.try_acquire(dev_lock.lock_path(str(workspace)), "a")
    base = ["--workspace", str(workspace), "print('hi')"]
    assert dev_python.main([*base, "--owner", "b"]) == 3 and Client.calls == []
    assert "not held by b" in capsys.readouterr().out
    assert dev_python.main(base) == 3 and Client.calls == []  # no --owner
    assert "Pass --owner" in capsys.readouterr().out
    assert dev_python.main([*base, "--owner", "a", "--timeout", "300", "--no-part"]) == 0
    ((method, params, wait),) = Client.calls
    assert method == "nx_dev_python" and params["timeout"] == 300 and params["allow_no_part"]
    assert wait > 300  # the client outwaits the snippet's own limit
    assert capsys.readouterr().out == "hi\n"
