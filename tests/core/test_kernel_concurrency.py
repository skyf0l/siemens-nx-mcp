"""Calls of several agents in one NX session: a failed call never touches the refs of
other parts, a rollback that cannot undo keeps every live ref, a call that undid past its
own undo mark is not undone twice, part refs that change come back as ``remapped``, a
resolver's part taken from shared session state never overrides the call's ``part``, a
transient part-switch failure is retried once, and a late queued call says why it did
not run."""

from __future__ import annotations

import time

import pytest

from nx_mcp.core import busy, kernel, target
from nx_mcp.core.errors import fail
from nx_mcp.kit import snapshots
from nx_mcp.kit import switching as kit_switching
from tests.core import test_kernel_switch as sw
from tests.core.fakes import FakePart, FakeServices
from tests.core.test_kernel import SPECS, _with_handler, ref, run
from tests.core.test_kernel_target import BLOCK, _block_nx, _count, _second
from tests.kit import run as kit_run
from tests.kit.objects import NXException

NULL = "Encountered unexpected NULL pointer"


@pytest.fixture
def svc(tmp_path):
    return FakeServices(tmp_path)


def _two_parts(svc, tmp_path):
    other = FakePart(tmp_path / "b.prt")
    svc.extra_parts.append(other)
    return other


# ---------------------------------------------------------------------------
# Rollback scope


def test_a_rollback_never_retires_or_reports_another_parts_refs(svc, tmp_path):
    other = _two_parts(svc, tmp_path)
    gone = other.add("feature", "GONE")
    gone_ref = ref(svc, gone, "feature")
    other.objects.remove(gone)  # another agent's object went meanwhile (not this call)
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED", part="a.prt")
    assert bad["mutation_outcome"] == "rolled_back", bad
    assert bad["changes"]["references"] == "preserved"
    assert gone_ref not in bad["error"]["details"].get("stale_refs", [])
    assert gone_ref not in svc.registry._stale_ids  # left to the part's own next check


def test_a_failed_rollback_keeps_every_live_ref_and_part_ref(svc, tmp_path):
    other = _two_parts(svc, tmp_path)
    keep = svc.part.add("feature", "KEEP")
    there = other.add("feature", "THERE")
    keep_ref, there_ref = ref(svc, keep, "feature"), ref(svc, there, "feature")
    own_part, other_part = ref(svc, svc.part, "part"), ref(svc, other, "part")
    svc.fail_undo = True
    bad = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED", part="a.prt")
    assert bad["mutation_outcome"] == "partial" and bad["error"]["code"] == "NX_ROLLBACK_FAILED"
    assert bad["changes"]["references"] == "preserved", bad
    for value, kind, obj in (
        (keep_ref, "feature", keep),
        (there_ref, "feature", there),
        (own_part, "part", svc.part),
        (other_part, "part", other),
    ):
        assert svc.references.resolve(value, (kind,)) is obj
    assert ref(svc, svc.part, "part") == own_part  # never re-issued


def test_a_call_that_undid_past_its_own_mark_is_not_undone_again(svc):
    old = svc.part.add("feature", "OLD")
    earlier = svc.set_mark("an earlier call")
    later = svc.part.add("feature", "LATER")
    svc.undone.clear()

    def undo_then_fail(tx, *, size):
        tx.touched = True
        svc.undo_to(earlier)  # an undo tool: back past the call's own mark
        svc.delete_mark(tx.mark)  # NX drops the newer marks with it
        tx.after_undo()
        fail("NX_BUILD_FAILED", "a later step failed")

    bad = _with_handler(svc, undo_then_fail, size=1)
    assert bad["mutation_outcome"] == "partial" and bad["error"]["code"] == "NX_BUILD_FAILED"
    assert bad["error"]["details"]["undo_mark"] == "reverted_by_call"
    assert "stays undone" in bad["error"]["hint"]
    assert svc.undone == [earlier]  # the kernel undid nothing more
    assert svc.part.objects == [old] and later not in svc.part.objects


def test_an_undo_to_a_mark_nx_no_longer_has_is_no_rollback_failure(svc, monkeypatch):
    def touched(tx, *, size):
        tx.touched = True
        fail("NX_BUILD_FAILED", "refused before any change")

    def no_mark(mark):
        raise RuntimeError("cannot tell")

    def undo_to(mark):
        raise RuntimeError(NULL)

    monkeypatch.setattr(svc, "mark_exists", no_mark)
    monkeypatch.setattr(svc, "undo_to", undo_to)
    bad = _with_handler(svc, touched, size=1)
    assert bad["mutation_outcome"] == "rolled_back" and bad["error"]["code"] == "NX_BUILD_FAILED"


def test_a_part_ref_that_changed_comes_back_remapped(svc):
    old_part = svc.part
    old_ref = ref(svc, old_part, "part")
    before = kernel._part_refs(svc, [old_part])
    svc.invalidate_parts([old_part])
    svc.part = FakePart(svc.root / "a.prt")  # NX loaded the same file again
    remapped = kernel._remapped(svc, before)
    assert remapped == {old_ref: ref(svc, svc.part, "part")} and old_ref not in remapped.values()


def test_call_parts_are_the_target_and_touched_parts_only(svc, tmp_path):
    other = _two_parts(svc, tmp_path)
    third = _two_parts(svc, tmp_path / "x")
    assert kernel.call_parts(svc, None, svc.part) == [svc.part]
    tx = kernel.Tx(svc, SPECS["nx_create_block"], operation_id=None)
    tx.touch_part(other)
    assert kernel.call_parts(svc, tx, svc.part) == [svc.part, other]
    assert third not in kernel.call_parts(svc, tx, svc.part)


# ---------------------------------------------------------------------------
# Gate 3t: shared session state never decides the target


def test_another_parts_pending_redo_never_decides_a_calls_part():
    nx = _block_nx()
    _second(nx)
    assert kit_run(nx, "nx_create_primitive", part="work", **BLOCK).ok
    assert kit_run(nx, "nx_undo_operation", part="work").ok  # work.prt has a call to redo
    r = kit_run(nx, "nx_redo_operation", part="plate")
    assert r.error["code"] == "NX_NOT_FOUND" and "plate.prt" in r.error["message"], r
    assert r.part["file"] == "plate.prt" and r.part["from"] == "part"
    assert _count(nx) == 0  # work.prt's undone call was not redone
    redo = kit_run(nx, "nx_redo_operation", part="work")
    assert redo.ok and _count(nx) == 1, redo


def test_a_call_naming_another_parts_own_object_is_still_refused():
    nx = _block_nx()
    _second(nx)
    made = kit_run(nx, "nx_create_primitive", part="work", **BLOCK)
    assert made.ok, made
    r = kit_run(nx, "nx_undo_operation", operation=made.operation_id, part="plate")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT", r
    assert "own object" in r.error["message"] and _count(nx) == 1


# ---------------------------------------------------------------------------
# Gate 4s: a transient part-switch failure


def _flaky(calls, real, times=1):
    def once(*args):
        if calls.count("failed") < times:
            calls.append("failed")
            raise NXException(68, NULL)
        calls.append("ok")
        return real(*args)

    return once


def test_a_transient_null_pointer_on_the_switch_is_retried_once():
    nx = sw._nx()
    calls: list = []
    nx.uf.Assem.SetWorkPartQuietly = _flaky(calls, nx.uf.Assem.SetWorkPartQuietly)
    r = sw.call(nx, sw.MODEL, part="plate")
    assert r.ok and r.mutation_outcome == "committed", r
    assert calls[:2] == ["failed", "ok"] and len(nx.plate.Features) == 1
    assert sw._state(nx) == (nx.part, nx.part)


def test_a_switch_nx_refuses_twice_is_not_started_with_a_retry_hint():
    nx = sw._nx()
    calls: list = []
    nx.uf.Assem.SetWorkPartQuietly = _flaky(calls, nx.uf.Assem.SetWorkPartQuietly, times=2)
    r = sw.call(nx, sw.MODEL, part="plate")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.mutation_outcome == "not_started", r
    assert "repeat the call" in r.error["hint"] and "dialog" not in r.error["hint"]
    assert calls == ["failed", "failed"] and not nx.plate.Features


def test_a_transient_null_pointer_on_the_restore_is_retried_without_a_warning():
    nx = sw._nx()
    calls: list = []
    nx.session.Parts.SetWork = _flaky(calls, nx.session.Parts.SetWork)
    r = sw.call(nx, sw.MODEL, part="plate")
    assert r.ok and not any("restore" in w["message"] for w in r.warnings), r
    assert calls == ["failed", "ok"] and sw._state(nx) == (nx.part, nx.part)


def test_transient_names_only_nx_null_pointers():
    assert kit_switching.transient(RuntimeError(NULL))
    assert not kit_switching.transient(RuntimeError("Part is not a descendant"))
    assert not kit_switching.transient(None)


# ---------------------------------------------------------------------------
# A queued call past its start deadline


def test_a_late_call_says_nx_was_busy_and_may_be_sent_again():
    error = target.expired_error(time.time() - 3, 3.2, "NX is running and busy with nx_x")
    assert error.code == "NX_CALL_EXPIRED" and error.retry == "after_wait"
    assert "busy with nx_x" in error.message and "nothing changed" in error.message
    assert "gave up" not in error.message
    with pytest.raises(Exception) as caught:
        target.check_expiry(SPECS["nx_create_block"], time.time() - 5)
    assert caught.value.code == "NX_CALL_EXPIRED" and caught.value.retry == "after_wait"


# ---------------------------------------------------------------------------
# Busy parts: only a model change makes a close "discard a recent change"


class _Busy:
    """Services with the busy table only (core/busy.py)."""

    def __init__(self, root):
        self.table: dict = {}
        self.part = FakePart(root / "a.prt")
        self.workspace = type("W", (), {"root": root})()

    def part_activity(self):
        return self.table

    def parts(self):
        return [self.part]

    def part_key(self, part):
        return "k"

    def created(self, part):
        return True


def _stamp(services, effect, name, now, children=()):
    from types import SimpleNamespace as NS

    spec = NS(effect=effect, name=name)
    tx = NS(operation_id=name, caller=None, extra_parts={}, worked_in=None, children=list(children))
    busy.record(services, spec, NS(key="k"), tx, frozenset({services.part.Tag}), now=now)
    return services.table["parts"]["k"]


def test_a_checkpoint_or_activation_is_no_change_of_the_part(tmp_path):
    services = _Busy(tmp_path)
    entry = _stamp(services, "model", "nx_set_expression", 100.0)
    assert entry["changed_at"] == 100.0 and entry["change"]["tool"] == "nx_set_expression"
    entry = _stamp(services, "session", "nx_create_checkpoint", 110.0)
    assert entry["at"] == 110.0 and entry["changed_at"] == 100.0
    assert entry["change"]["tool"] == "nx_set_expression"  # the refusal names the real change
    entry = _stamp(services, "session", "nx_preview_changes", 120.0, children=[{}])
    assert entry["changed_at"] == 120.0  # an accepted preview's steps changed the part


# ---------------------------------------------------------------------------
# Result snapshots: tagged with the caller that made them


def test_snapshots_are_claimed_per_caller(monkeypatch):
    monkeypatch.setattr(snapshots, "_CLAIMED", {})
    token = snapshots.OWNER.set("sc-1.1:agent-a")
    try:
        snapshots.claim("result_a")
        assert snapshots.current_owner() == "sc-1.1:agent-a"
    finally:
        snapshots.OWNER.reset(token)
    snapshots.claim("result_b", owner="sc-1.1:agent-b")
    snapshots.claim("result_n", owner=None)  # no caller id: every caller's
    assert snapshots.claimed("sc-1.1:agent-a") == {"result_a", "result_n"}
    assert snapshots.claimed("sc-1.1:agent-b") == {"result_b", "result_n"}
    assert snapshots.claimed(None) == {"result_a", "result_b", "result_n"}
    token = snapshots.OWNER.set("sc-1.1:agent-b")
    try:
        assert snapshots.claimed() == {"result_b", "result_n"}  # the current caller's
    finally:
        snapshots.OWNER.reset(token)
