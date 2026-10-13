"""OperationStore: running markers, a recover that reads in-flight receipts only, and
retention (age, count, running receipts kept, errors swallowed)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from nx_mcp.core import receipts
from nx_mcp.core.receipts import OperationStore

DAY = 86400.0


def _record(op: str, state: str = "committed", session: str = "old") -> dict:
    return {"operation_id": op, "state": state, "session_id": session, "fingerprint": "f"}


def _age(store: OperationStore, op: str, days: float, suffix: str = ".json") -> None:
    stamp = time.time() - days * DAY
    os.utime(store.root / (op + suffix), (stamp, stamp))


def test_markers_follow_the_running_state(tmp_path):
    store = OperationStore(tmp_path)
    assert not store.running.exists()  # made on first use only
    store.put(_record("op_marker_1", "running"))
    marker = store.running / "op_marker_1"
    assert marker.is_file()
    store.put(dict(_record("op_marker_1", "running"), progress={"completed": 1}))
    assert marker.is_file()
    store.put(_record("op_marker_1", "committed"))
    assert not marker.exists()
    store.put(_record("op_marker_2", "failed"))  # never running: never a marker
    assert list(store.running.iterdir()) == []


def test_recover_reads_only_marked_receipts(tmp_path, monkeypatch):
    store = OperationStore(tmp_path)
    for i in range(50):
        store.put(_record(f"op_finished_{i:02d}"))
    store.put(_record("op_stale_run", "running", session="crashed"))
    store.put(_record("op_live_run1", "running", session="now"))
    # A receipt written before markers existed: running, no marker.
    (store.root / "op_unmarked_1.json").write_text(json.dumps(_record("op_unmarked_1", "running")))
    # A stale marker whose receipt finished.
    store.running.joinpath("op_finished_00").touch()

    reads: list[str] = []
    original = Path.read_text

    def counting(self, *args, **kwargs):
        reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    store.recover("now", prune=False)

    assert sorted(reads) == ["op_finished_00.json", "op_live_run1.json", "op_stale_run.json"]
    assert store.get("op_stale_run")["state"] == "unknown"
    assert store.get("op_stale_run")["mutation_outcome"] == "unknown"
    assert store.get("op_live_run1")["state"] == "running"
    assert store.get("op_unmarked_1")["state"] == "running"  # documented: left to retention
    assert sorted(os.listdir(store.running)) == ["op_live_run1"]


def test_recover_never_raises_and_drops_markers_without_receipts(tmp_path):
    store = OperationStore(tmp_path)
    store.running.mkdir()
    store.running.joinpath("op_missing_1").touch()
    store.put(_record("op_corrupt_1", "running"))
    (store.root / "op_corrupt_1.json").write_text("{not json")
    store.recover("now", prune=False)
    assert sorted(os.listdir(store.running)) == []


def test_recover_starts_a_daemon_pruner(tmp_path):
    store = OperationStore(tmp_path)
    store.put(_record("op_ancient_1"))
    _age(store, "op_ancient_1", receipts.RETENTION_DAYS + 1)
    with receipts._prune_lock:  # wait for a pruner an earlier test may have started
        pass
    store.recover("now")
    assert store.pruner is not None and store.pruner.daemon
    store.pruner.join(10)
    assert store.get("op_ancient_1")["state"] == "unknown"  # pruned reads as unknown


def test_retention_by_age_keeps_running_and_recent(tmp_path):
    store = OperationStore(tmp_path)
    store.put(_record("op_old_done"))
    store.put(_record("op_new_done"))
    store.put(_record("op_old_running", "running"))
    (store.root / "op_old_done.cancel").touch()
    (store.root / "op_old_running.cancel").touch()
    for op in ("op_old_done", "op_old_running"):
        _age(store, op, 8)
        _age(store, op, 8, ".cancel")
    (store.root / "op_new_done.tmp").touch()  # a put in progress: recent, kept

    assert store.prune(pause=0) == 2
    left = sorted(p.name for p in store.root.iterdir() if p.is_file())
    assert left == [
        "op_new_done.json",
        "op_new_done.tmp",
        "op_old_running.cancel",
        "op_old_running.json",
    ]


def test_retention_by_count_keeps_the_newest(tmp_path):
    store = OperationStore(tmp_path)
    for i in range(6):
        store.put(_record(f"op_count_{i}"))
        _age(store, f"op_count_{i}", (6 - i) / 24)  # op_count_5 is the newest
    store.put(_record("op_count_run", "running"))
    _age(store, "op_count_run", 1)  # oldest, but running
    (store.root / "op_count_0.cancel").touch()  # recent flag of a pruned receipt

    assert store.prune(max_count=3, pause=0) == 4
    left = sorted(p.stem for p in store.root.glob("*.json"))
    # The running receipt counts among the kept slots only by mtime, but is never deleted.
    assert left == ["op_count_3", "op_count_4", "op_count_5", "op_count_run"]
    assert not (store.root / "op_count_0.cancel").exists()


def test_prune_keeps_a_receipt_rewritten_or_started_since_the_scan(tmp_path, monkeypatch):
    store = OperationStore(tmp_path)
    store.put(_record("op_rewrite_1"))
    store.put(_record("op_restart_1"))
    _age(store, "op_rewrite_1", 8)
    _age(store, "op_restart_1", 8)
    real_delete = store._delete

    def racing(name, scanned):  # a concurrent put lands between the scan and the delete
        if name == "op_rewrite_1.json":
            store.put(_record("op_rewrite_1", "committed", session="now"))
        if name == "op_restart_1.json":
            store.running.mkdir(exist_ok=True)
            store.running.joinpath("op_restart_1").touch()
        return real_delete(name, scanned)

    monkeypatch.setattr(store, "_delete", racing)
    assert store.prune(pause=0) == 0
    assert store.get("op_rewrite_1")["session_id"] == "now"
    assert store.get("op_restart_1")["state"] == "committed"


def test_prune_swallows_errors(tmp_path, monkeypatch):
    store = OperationStore(tmp_path)
    store.put(_record("op_locked_1"))
    store.put(_record("op_locked_2"))
    _age(store, "op_locked_1", 8)
    _age(store, "op_locked_2", 8)

    def denied(self, *args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "unlink", denied)
    assert store.prune(pause=0) == 0
    assert (store.root / "op_locked_1.json").exists()

    def broken(*args, **kwargs):
        raise OSError("gone")

    monkeypatch.setattr(receipts.os, "scandir", broken)
    assert store.prune(pause=0) == 0  # no raise


def test_one_pruner_at_a_time(tmp_path, monkeypatch):
    store = OperationStore(tmp_path)
    assert receipts._prune_lock.acquire(timeout=10)
    try:
        assert store.start_pruning() is None
    finally:
        receipts._prune_lock.release()
    thread = store.start_pruning()
    assert thread is not None
    thread.join(10)
    assert not receipts._prune_lock.locked()


@pytest.mark.parametrize("state", ["committed", "failed", "unknown"])
def test_a_put_out_of_running_removes_the_marker(tmp_path, state):
    store = OperationStore(tmp_path)
    store.put(_record("op_leaving_1", "running"))
    store.put(_record("op_leaving_1", state))
    assert not (store.running / "op_leaving_1").exists()
