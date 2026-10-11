"""Ask me first with several calls and parts: one card per call (red team A29, phase P5),
the target part on panel and log lines, and the ghost guard in the host queue (A27).

On the real host scheduler (``interactive._schedule``/``_show_card``/``_apply_answer``)
and the real ``MainThreadDispatcher`` queue, with the fake NX of ``tests/kit``.
"""

import re
import socket
import time

from nx_mcp import interactive
from tests.host.test_ui_recovery import _busy, _queue, host  # noqa: F401  (fixtures)
from tests.kit.objects import Part

BODIES = {"kind": "body"}


def _second(rig):
    """A second loaded part, b.prt; the engineer stays on test.prt."""
    b = Part(rig.session, rig.e.workspace.root / "b.prt")
    rig.session.Parts.Work = rig.session.Parts.Display = rig.part
    return b


def _three_calls(queue):
    a1 = queue.submit("nx_list_objects", {**BODIES, "part": "test", "intent": "A: bodies"})
    b1 = queue.submit("nx_set_camera", {"part": "b", "intent": "B: turn b's camera"})
    a2 = queue.submit("nx_list_objects", {**BODIES, "part": "test", "intent": "A: again"})
    return a1, b1, a2


def _ticks(host, count=4):  # noqa: F811
    for _ in range(count):
        host.tick()


# -- one card per call ----------------------------------------------------------


def test_run_once_runs_only_the_call_on_the_card(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "ask")
    a1, b1, a2 = _three_calls(queue)
    host.tick()
    text = host.panel_text()
    assert "[test.prt] List body objects" in text and "(2 more waiting)" in text
    assert "camera" not in text.split("\n")[0]  # the card shows its own call only
    host.answer_requested = "run_once"
    _ticks(host, 6)
    assert [p.complete.is_set() for p in (a1, b1, a2)] == [True, False, False]
    assert a1.error is None and host.waiting_count == 2
    assert "[b.prt]" in host.panel_text()  # the next call is on the card now


def test_not_now_declines_only_the_shown_call(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "ask")
    a1, b1, a2 = _three_calls(queue)
    host.tick()
    host.answer_requested = "decline"
    host.tick()
    assert a1.error.code == "NX_CONTROL_DECLINED"
    assert a1.error.details["mutation_outcome"] == "not_started"
    assert not b1.complete.is_set() and not a2.complete.is_set()


def test_a_call_arriving_while_the_card_is_up_is_not_approved(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "ask")
    a1 = queue.submit("nx_list_objects", {**BODIES, "part": "test"})
    host.tick()
    b1 = queue.submit("nx_set_camera", {"part": "b", "intent": "B arrives later"})
    host.answer_requested = "run_once"  # the engineer clicks for what they saw
    _ticks(host)
    assert a1.complete.is_set() and a1.error is None and not b1.complete.is_set()


def test_each_call_has_its_own_clock(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "ask")
    a1 = queue.submit("nx_list_objects", {**BODIES, "part": "test"})
    host.tick()
    host.waiting_since -= 58  # a1 has been on the card for 58 s
    b1 = queue.submit("nx_set_camera", {"part": "b"})
    host.tick()
    host.waiting_since -= 3  # a1 expires; b1's clock has not started yet
    host.tick()
    assert a1.error.code == "NX_CONTROL_PAUSED" and not b1.complete.is_set()
    host.tick()
    assert host.waiting_count == 1 and "[b.prt]" in host.panel_text()
    assert re.search(r"Answer within (59|60) s\.", host.panel_text())  # a fresh clock for b1
    host.waiting_since -= 59
    host.tick()
    assert not b1.complete.is_set()  # 59 s of its own: still waiting
    host.waiting_since -= 2
    host.tick()
    assert b1.error.code == "NX_CONTROL_PAUSED"


def test_the_clock_ends_before_the_bridge_stops_waiting(host):  # noqa: F811
    queue = _queue(host, "ask")
    late = queue.submit("nx_set_camera")
    late.queued_at -= interactive.HOLD_SECONDS + 75  # it waited behind other calls
    host.tick()
    assert late.error.code == "NX_CONTROL_PAUSED"  # the host answers, not the bridge


def test_run_and_switch_to_auto_approves_the_shown_call_then_auto_rules(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "ask")
    shown = queue.submit("nx_set_camera", {"fit": True, "part": "b"})
    host.tick()
    read = queue.submit("nx_list_objects", {**BODIES, "part": "test"})
    change = queue.submit("nx_set_camera", {"fit": True, "part": "test"})
    host.answer_requested = "run_auto"
    _busy(host)  # Auto: changes wait for a busy engineer, reads do not
    _ticks(host)
    assert host.mode == "auto" and host.waiting_count == 0  # Auto shows no card
    assert read.complete.is_set() and read.error is None
    assert host.busy_wait is shown and not change.complete.is_set()
    _busy(host, False)
    _ticks(host)
    assert shown.error is None and change.error is None and change.complete.is_set()


def test_a_call_needing_no_answer_never_waits_behind_the_card(host, rig):  # noqa: F811
    queue = _queue(host, "ask")
    held = queue.submit("nx_set_camera")
    approved = queue.submit("nx_list_objects", BODIES)
    approved.approved = True  # e.g. answered on an earlier card
    status = queue.submit("nx_get_status")
    _ticks(host, 3)
    assert status.complete.is_set() and approved.complete.is_set()
    assert approved.error is None and not held.complete.is_set() and host.waiting_count == 1


# -- solo is unchanged -----------------------------------------------------------


def test_one_part_shows_the_card_as_before(host):  # noqa: F811
    queue = _queue(host, "ask")
    held = queue.submit("nx_list_objects", {**BODIES, "intent": "Check the boss body"})
    host.tick()
    lines = host.panel_text().splitlines()
    assert lines[:2] == [
        "▶ Agent wants to: List body objects",
        "   “Check the boss body”  (agent's note)",
    ]
    assert re.fullmatch(
        r"Answer within (59|60) s\. Finish your current NX command first\.", lines[2]
    )
    host.answer_requested = "run_once"
    host.tick()
    assert held.error is None
    line = host.activity.recent(1)[0]
    assert "[" not in line and "1 read-only or view step" in line


# -- the target part on every line ----------------------------------------------------


def test_activity_lines_name_the_part_when_two_are_loaded(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "auto")
    camera = queue.submit("nx_set_camera", {"fit": True, "part": "b"})
    read = queue.submit("nx_list_objects", {**BODIES, "part": "test"})
    _ticks(host)
    assert camera.error is None and read.error is None
    lines = host.activity.recent(2)  # the read ran first (Auto); groups never mix parts
    assert "[test.prt] 1 read-only or view step" in lines[0]
    assert "[b.prt] 1 read-only or view step" in lines[1]
    log = (host.state_dir / "activity.log").read_text(encoding="utf-8").splitlines()
    assert "OK      [test.prt] List body objects" in log[0]
    assert "OK      [b.prt] Fit the view" in log[1]


def test_a_failed_call_names_its_part_too(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "auto")
    failed = queue.submit("nx_list_objects", {**BODIES, "owner": "obj_unknown", "part": "b"})
    _ticks(host)
    assert failed.error is not None
    assert "[b.prt] " in host.activity.recent(1)[0]


def test_the_running_line_names_the_part(host, rig):  # noqa: F811
    _second(rig)
    seen = []
    rig.e.dev_tools["nx_test_step"] = lambda: seen.append(host.panel_text()) or {}
    host.mode, host._light = "auto", True
    host.execute("nx_test_step", {})
    assert "[test.prt] Test step" in seen[0].splitlines()[0]


# -- the ghost guard in the host queue (A27) ------------------------------------------


def test_an_expired_call_is_answered_and_never_shown(host):  # noqa: F811
    queue = _queue(host, "ask")
    ghost = queue.submit("nx_set_camera", {"expires_at": time.time() - 1})
    fresh = queue.submit("nx_set_camera", {"expires_at": time.time() + 60})
    host.tick()
    assert ghost.error.code == "NX_CALL_EXPIRED"
    assert ghost.error.details["mutation_outcome"] == "not_started"
    assert host.waiting_count == 1 and not fresh.complete.is_set()
    read = queue.submit("nx_list_objects", {**BODIES, "expires_at": time.time() - 1})
    host.mode = "auto"
    _ticks(host)
    assert read.error is None  # a read still answers (core/target.py check_expiry)


def test_a_call_whose_client_closed_its_socket_never_runs(host, rig):  # noqa: F811
    ours, theirs = socket.socketpair()
    try:
        queue = _queue(host, "auto")
        rig.e.dev_tools["nx_test_edit"] = lambda: {"ran": True}
        pending = queue.submit("nx_test_edit")
        pending.peer = ours
        theirs.close()  # the MCP client cancelled: the sidecar closed its connection
        _ticks(host)
        assert pending.error.code == "NX_CALL_EXPIRED" and pending.result is None
    finally:
        ours.close()


def test_status_reports_the_queue(host):  # noqa: F811
    queue = _queue(host, "ask")
    first = queue.submit("nx_set_camera")
    queue.submit("nx_list_objects", BODIES)
    first.queued_at -= 12
    host.tick()
    state = host.status()["queue"]
    assert state["waiting"] == 2 and 11 < state["oldest_seconds"] < 30


def test_a_refused_queued_call_leaves_one_activity_line(host, rig):  # noqa: F811
    _second(rig)
    queue = _queue(host, "ask")
    ghost = queue.submit("nx_set_camera", {"part": "b", "expires_at": time.time() - 1})
    host.tick()
    assert ghost.error.code == "NX_CALL_EXPIRED"
    lines = host.activity.recent(10)
    skipped = [line for line in lines if "Skipped" in line]
    assert len(skipped) == 1 and "[b.prt]" in skipped[0], lines
    assert "the agent stopped waiting" in skipped[0]
    logged = host.activity.log_path.read_text(encoding="utf-8")
    assert "SKIPPED  Skipped [b.prt]" in logged and "[nx_set_camera]" in logged, logged
    host.tick()
    assert len([line for line in host.activity.recent(10) if "Skipped" in line]) == 1


def test_a_call_whose_socket_closed_at_its_start_leaves_one_activity_line(host, rig):  # noqa: F811
    ours, theirs = socket.socketpair()
    try:
        queue = _queue(host, "auto")
        rig.e.dev_tools["nx_test_edit"] = lambda: {"ran": True}
        pending = queue.submit("nx_test_edit")
        host._gone = lambda _pending: False  # the socket closes after the host's check
        pending.peer = ours
        theirs.close()
        _ticks(host)
        assert pending.error.code == "NX_CALL_EXPIRED" and pending.result is None
        skipped = [line for line in host.activity.recent(10) if "Skipped" in line]
        assert len(skipped) == 1 and "stopped waiting" in skipped[0], skipped
    finally:
        ours.close()
