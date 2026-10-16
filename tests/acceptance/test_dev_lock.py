"""scripts/dev_lock.py: one holder at a time, stale takeover, waiters of dead callers."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_lock.py"


def load():
    spec = importlib.util.spec_from_file_location("dev_lock_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dev_lock = load()


@pytest.fixture
def path(tmp_path):
    return dev_lock.lock_path(str(tmp_path))


def age_lock(path, seconds):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["refreshed_at"] = time.time() - seconds
    path.write_text(json.dumps(data), encoding="utf-8")


def test_one_holder_refresh_and_release(path):
    assert dev_lock.try_acquire(path, "a") == (True, None)
    assert dev_lock.try_acquire(path, "b") == (False, None)
    assert dev_lock.try_acquire(path, "a") == (True, "refreshed")  # acquire again = refresh
    assert not dev_lock.refresh(path, "b") and not dev_lock.release(path, "b")
    age_lock(path, 100)
    assert dev_lock.refresh(path, "a")
    assert dev_lock.age(dev_lock.read(path)) < 5
    assert dev_lock.read(path)["owner"] == "a"
    assert dev_lock.release(path, "a") and not path.exists()
    assert not dev_lock.refresh(path, "a")  # refresh never acquires


def test_a_stale_lock_is_taken_over_and_named(path):
    assert dev_lock.STALE_SECONDS == 600
    dev_lock.try_acquire(path, "gone")
    age_lock(path, dev_lock.STALE_SECONDS - 30)
    assert dev_lock.try_acquire(path, "next") == (False, None)  # not stale yet
    age_lock(path, dev_lock.STALE_SECONDS + 59)
    acquired, note = dev_lock.try_acquire(path, "next")
    assert acquired and "stale lock of gone" in note and "659 s" in note
    assert dev_lock.read(path)["owner"] == "next"
    assert not dev_lock.refresh(path, "gone")  # the old holder cannot take it back


def test_only_one_of_many_takes_over_a_stale_lock(path):
    dev_lock.try_acquire(path, "gone")
    age_lock(path, 10_000)
    start = threading.Barrier(8)
    wins = []

    def contend(name):
        start.wait()
        if dev_lock.try_acquire(path, name)[0]:
            wins.append(name)

    threads = [threading.Thread(target=contend, args=(f"agent{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(wins) == 1 and dev_lock.read(path)["owner"] == wins[0]


def test_cli_status_acquire_refresh_release(tmp_path, capsys):
    args = ["--workspace", str(tmp_path)]
    assert dev_lock.main(["status", *args]) == 0
    assert capsys.readouterr().out.strip() == "free"
    assert dev_lock.main(["acquire", "--owner", "a", *args]) == 0
    assert dev_lock.main(["acquire", "--owner", "b", "--wait", "0", *args]) == 2
    assert "held by a" in capsys.readouterr().out
    assert dev_lock.main(["refresh", "--owner", "b", *args]) == 1
    assert dev_lock.main(["refresh", "--owner", "a", *args]) == 0
    age_lock(dev_lock.lock_path(str(tmp_path)), 700)
    dev_lock.main(["status", *args])
    assert "stale" in capsys.readouterr().out
    assert dev_lock.main(["acquire", "--owner", "b", "--wait", "0", *args]) == 0
    assert "took over the stale lock of a" in capsys.readouterr().out
    assert dev_lock.main(["release", "--owner", "a", *args]) == 1
    assert dev_lock.main(["release", "--owner", "b", *args]) == 0


def test_a_waiter_whose_parent_exited_gives_up(path):
    class Gone:
        pid = 1234

        def alive(self):
            return False

    dev_lock.try_acquire(path, "holder")
    lines = []
    code = dev_lock.acquire(path, "waiter", wait=60, parent=Gone(), poll=0.01, echo=lines.append)
    assert code == 3 and "has exited" in lines[-1]
    assert dev_lock.read(path)["owner"] == "holder"


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX watches the parent PID only")
def test_parent_watch_sees_a_process_exit():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    watch = dev_lock.ParentWatch([sleeper.pid])
    try:
        assert watch.alive()
        sleeper.kill()
        sleeper.wait(timeout=10)
        assert not watch.alive()
    finally:
        watch.close()
        if sleeper.poll() is None:
            sleeper.kill()


def test_an_orphaned_acquire_wait_exits(tmp_path):
    """A waiting ``acquire`` whose starting process dies stops waiting (exit 3)."""
    workspace = str(tmp_path)
    dev_lock.try_acquire(dev_lock.lock_path(workspace), "holder")
    # The parent starts the waiter (through the venv's python launcher when there is
    # one), writes its PID, lets it start, then exits.
    parent = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, {str(SCRIPT)!r}, 'acquire', '--owner', "
        f"'orphan', '--wait', '120', '--workspace', {workspace!r}])\n"
        f"open({str(tmp_path / 'pid.txt')!r}, 'w').write(str(child.pid))\n"
        "time.sleep(3)\n"
    )
    subprocess.run([sys.executable, "-c", parent], check=True, timeout=30)
    pid = int((tmp_path / "pid.txt").read_text())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and process_running(pid):
        time.sleep(0.2)
    assert not process_running(pid), "the orphaned waiter still runs"
    assert dev_lock.read(dev_lock.lock_path(workspace))["owner"] == "holder"


def process_running(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
