"""The panel's "Parts in use" list, the engineer-change rule and the queue counters.

The panel is Win32: these tests check the model (``InteractiveHost.parts_in_use``), the
lines (``activity.part_line``) and what reaches the panel (``show_parts``), on the fake
NX of ``tests/kit``; live checks go through status and logs only, never clicks.
"""

import time
from unittest.mock import Mock

from nx_mcp import activity, interactive
from nx_mcp.kit import parts as kit_parts
from tests.host.test_ui_recovery import _queue, host  # noqa: F401  (fixtures)
from tests.kit.objects import Part

BODIES = {"kind": "body"}


def _agent_parts(rig):
    """The engineer shows test.prt (not the agent's); the agent has a.prt and b.prt."""
    a = Part(rig.session, rig.e.workspace.root / "a.prt")
    b = Part(rig.session, rig.e.workspace.root / "b.prt")
    rig.e.owned_parts.pop(rig.e.part_key(rig.part), None)  # the engineer's own part
    for part in (a, b):
        rig.e.own(part)
    rig.session.Parts.Work = rig.session.Parts.Display = rig.part
    return a, b


def test_parts_in_use_lists_the_agents_parts_with_tab_state_and_save_state(host, rig):  # noqa: F811
    a, b = _agent_parts(rig)
    kit_parts.show(rig.nx, rig.session.Parts, a)  # a gets its own tab ...
    kit_parts.show(rig.nx, rig.session.Parts, rig.part)  # ... the engineer's comes back
    a.IsModified = True
    b.IsModified = False
    rows = host.parts_in_use()
    assert [(r["file"], r["shown"], r["state"]) for r in rows] == [
        ("a.prt", "tab", "modified"),
        ("b.prt", "background", "new"),  # never saved: no file on disk
    ]
    (rig.e.workspace.root / "b.prt").write_bytes(b"saved")
    kit_parts.show(rig.nx, rig.session.Parts, b)
    rows = {r["file"]: r for r in host.parts_in_use()}
    assert rows["b.prt"]["shown"] == "shown" and rows["b.prt"]["state"] == "saved"
    assert "test.prt" not in rows  # the engineer's part is never listed


def test_a_line_names_the_last_agent_step_and_its_time():
    at = time.mktime((2026, 10, 12, 9, 5, 7, 0, 0, -1))
    row = {"file": "cover.prt", "shown": "tab", "state": "modified"}
    assert activity.part_line(row) == "cover.prt  ·  in a tab  ·  modified"
    line = activity.part_line({**row, "last": "Extrude 12 mm", "at": at, "ok": False})
    assert line == "cover.prt  ·  in a tab  ·  modified  ·  09:05:07 Extrude 12 mm (failed)"
    long = activity.part_line({**row, "last": "x" * 200, "at": at})
    assert len(long) == activity.LINE_WIDTH and long.endswith("…")


def test_each_call_updates_its_parts_line_and_the_panel_gets_the_list(host, rig):  # noqa: F811
    a, b = _agent_parts(rig)
    host.panel.show_parts = Mock()
    host.publish = interactive.InteractiveHost.publish.__get__(host)
    host._refresh_snapshot = lambda: {}
    host.mode, host._light = "auto", True
    host.execute("nx_list_objects", {**BODIES, "part": "b"})
    lines = dict(host.panel.show_parts.call_args.args[0])
    key_b = rig.e.part_key(b)
    assert "List body objects" in lines[key_b] and lines[key_b].startswith("b.prt  ·  ")
    assert "·" in lines[rig.e.part_key(a)] and "List" not in lines[rig.e.part_key(a)]


def test_show_part_activates_that_parts_tab_for_the_engineer(host, rig):  # noqa: F811
    a, _b = _agent_parts(rig)
    host.show_part_requested = rig.e.part_key(a)
    host.tick()
    assert rig.session.Parts.Display is a and host.show_part_requested is None
    assert kit_parts.tabs(rig.session.Parts) == [a, rig.part]  # the engineer's tab stays
    assert "Showing a.prt" in host.notice
    host.show_part_requested = "part_gone"
    host.tick()
    assert "no longer loaded" in host.notice and rig.session.Parts.Display is a


def test_show_part_waits_while_the_engineer_is_in_an_nx_command(host, rig):  # noqa: F811
    a, _b = _agent_parts(rig)
    host.simulated_busy = True
    host.show_part_requested = rig.e.part_key(a)
    host.tick()
    assert rig.session.Parts.Display is rig.part and "Finish your NX command" in host.notice


# -- engineer changes ---------------------------------------------------------------------


def test_a_tab_or_display_change_alone_is_no_engineer_edit(host, rig):  # noqa: F811
    model = {"marks": 5, "display": 1}
    host.model_fingerprint = lambda: (
        41,
        model["marks"],
        (1, 2),
        model["display"],
        model["display"],
    )
    host._remember_model()
    host._engineer_had_nx = True
    model["display"] = 2  # the engineer clicked another tab (NX kept its undo marks)
    assert not host._check_engineer_change()
    host._engineer_had_nx = True
    model["marks"] = 6  # an edit adds an undo mark
    assert host._check_engineer_change()
    log = (host.state_dir / "activity.log").read_text(encoding="utf-8")
    assert "ENGINEER  You changed the model" in log
    assert "(changed: the number of NX undo marks)" in log
    assert "[" not in log.split("ENGINEER", 1)[1]  # an engineer line names no part


def test_edit_changes_name_what_changed():
    before = (41, 5, (1, 2), 1, 1)
    assert interactive._edit_changes(before, (41, 5, (1, 2), 3, 3)) == []
    assert interactive._edit_changes(before, (42, 6, (1, 2), 1, 1)) == [
        "NX's newest undo mark",
        "the number of NX undo marks",
    ]
    assert interactive._edit_changes(before, (41, 5, (1,), 1, 1)) == ["the loaded parts"]
    assert interactive._edit_changes(None, before) == ["an unknown NX state"]


def test_a_development_snippet_line_names_no_part(host, rig):  # noqa: F811
    _agent_parts(rig)
    rig.e.dev_tools["nx_dev_python"] = lambda **_: {}
    host.mode, host._light = "auto", True
    host.execute("nx_dev_python", {})
    log = (host.state_dir / "activity.log").read_text(encoding="utf-8")
    line = next(x for x in log.splitlines() if "[nx_dev_python]" in x)
    assert "OK      Dev python" in line and not any(
        f"[{name}]" in line for name in ("test.prt", "a.prt", "b.prt")
    )


# -- queue counters ---------------------------------------------------------------------------


def test_a_status_answered_during_a_call_counts_the_queue_as_it_is_now(host, rig):  # noqa: F811
    queue = _queue(host, "auto")
    waiting = queue.submit("nx_list_objects", BODIES)
    waiting.queued_at -= 7.0  # queued 7 s ago, behind the running call
    host._snapshot = {"running_method": "nx_extrude", "mode": "auto",
                      "queue": {"waiting": 5, "oldest_seconds": 0.0}}  # fmt: skip
    result = host.dispatch("nx_get_status", {})
    control = result["data"]["control"]
    assert control["queue"]["waiting"] == 1 and control["queue"]["oldest_seconds"] >= 7.0
    assert control["running"] == "nx_extrude"
    assert control["busy"] is True and control["waiting"] == 1  # one count (bench3t)
