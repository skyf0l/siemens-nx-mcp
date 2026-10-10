"""Agent activity history shown in the NX MCP panel and written to activity.log."""

from nx_mcp.activity import LINE_WIDTH, Activity, created_name
from nx_mcp.core.errors import NXError

T = 1_791_460_000.0  # Fixed wall-clock start for readable assertions.


def run(activity, method, summary, *, read_only=False, result=None, error=None, at=0.0):
    return activity.record(
        method,
        summary,
        started=T + at,
        duration=1.25,
        intent="Add the boss" if method == "nx_extrude" else None,
        read_only=read_only,
        result=result if result is not None or error is not None else {"status": "success"},
        error=error,
    )


def test_success_failure_and_partial_lines_say_what_happened_to_the_model(tmp_path):
    activity = Activity(tmp_path / "activity.log")
    activity.start_session()
    run(
        activity,
        "nx_extrude",
        'Extrude "profile" 10 mm',
        result={"feature": {"name": "EXTRUDE(2)"}},
    )
    rolled = NXError(
        "NX_STALE_REFERENCE", "face reference is stale", details={"mutation_outcome": "rolled_back"}
    )
    run(activity, "nx_hole", "Drill hole Ø6.6 mm", error=rolled, at=5)
    partial = NXError("NX_ROLLBACK_FAILED", "undo failed", details={"mutation_outcome": "partial"})
    run(activity, "nx_blend", "Blend 2 edges R3 mm", error=partial, at=9)
    lines = activity.recent()
    assert lines[0].startswith("✓ ") and 'Extrude "profile" 10 mm → EXTRUDE(2)  1.2 s' in lines[0]
    assert lines[1].startswith("✗ ") and "rolled back, model unchanged (face reference" in lines[1]
    assert lines[2].startswith("⚠ ") and "partly applied, check the model" in lines[2]
    log = (tmp_path / "activity.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 3 and "OK      Extrude" in log[0] and "note: “Add the boss”" in log[0]
    assert "FAILED  Drill hole" in log[1] and "[nx_hole]" in log[1]


def test_read_only_steps_are_grouped_but_fully_logged(tmp_path):
    activity = Activity(tmp_path / "activity.log")
    activity.start_session()
    run(activity, "nx_blend", "Blend 1 edge R2 mm")
    for at in range(3):
        run(activity, "nx_list_bodies", "List bodies", read_only=True, at=at)
    run(activity, "nx_fit_view", "Fit the view", read_only=True, at=4)
    lines = activity.recent()
    assert len(lines) == 2 and "4 read-only or view steps (model unchanged)" in lines[1]
    assert len((tmp_path / "activity.log").read_text(encoding="utf-8").splitlines()) == 5


def test_recap_counts_only_this_sessions_model_changes():
    activity = Activity()
    activity.start_session()
    run(activity, "nx_extrude", "Old session change")
    activity.start_session()
    assert activity.session_recap() is None
    run(activity, "nx_extrude", "Extrude")
    run(activity, "nx_blend", "Blend")
    run(activity, "nx_list_bodies", "List", read_only=True)
    failure = NXError("X", "no", details={"mutation_outcome": "rolled_back"})
    run(activity, "nx_hole", "Hole", error=failure)
    recap = activity.session_recap()
    assert recap.startswith("Agent's last session: 2 changes, 1 failed (")


def test_lines_are_clipped_and_history_is_bounded():
    activity = Activity(keep=3)
    for index in range(5):
        run(activity, "nx_set_expression", f"Set x{index} = " + "9" * 200)
    assert len(activity.entries) == 3
    for line in activity.recent():
        assert len(line) <= LINE_WIDTH and "…" in line and line.endswith("1.2 s")


def test_model_state_survives_a_very_long_summary():
    activity = Activity()
    failure = NXError("X", "why", details={"mutation_outcome": "rolled_back"})
    run(activity, "nx_extrude", "Extrude " + "x" * 300, error=failure)
    run(activity, "nx_extrude", "Extrude " + "y" * 300, result={"feature": {"name": "EXTRUDE(9)"}})
    failed, done = activity.recent()
    assert "— rolled back, model unchanged" in failed and len(failed) <= LINE_WIDTH
    assert done.endswith("→ EXTRUDE(9)  1.2 s") and len(done) <= LINE_WIDTH


def test_created_name_from_result_or_changes():
    assert created_name({"sketch": {"name": "profile"}}) == "profile"
    created = [{"name": "EDGE_1"}, {"name": "EDGE_2"}, {"name": "FACE_1"}]
    assert created_name({"changes": {"created": created}}) == "EDGE_1"
    assert created_name({"status": "success"}) is None
    # nx_create_sketch: the sketch, not "SKETCH(1:1B), SKETCH(1) +5" internals.
    sketch = {"id": "s", "kind": "sketch", "name": "NACELLE_PROFILE"}
    made = [
        {"id": "f", "kind": "feature", "name": "SKETCH(1:1B)"},
        {"id": "f2", "kind": "feature", "name": "SKETCH(1)"},
        sketch,
        *({"id": f"p{i}", "kind": "point", "name": f"Point{i}"} for i in range(4)),
    ]
    assert created_name({"object": sketch, "changes": {"created": made}}) == "NACELLE_PROFILE"
    assert created_name({"changes": {"created": made}}) == "NACELLE_PROFILE"
    lines = [{"id": f"l{i}", "kind": "curve", "name": f"Line{i}"} for i in range(4)]
    assert created_name({"changes": {"created": lines}}) == "4 curves"
    # "object" names a modified thing too: only shown when this step created it.
    assert created_name({"object": sketch, "changes": {"created": []}}) is None


def test_log_failure_never_breaks_recording(tmp_path):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    activity = Activity(blocked / "activity.log")
    assert run(activity, "nx_blend", "Blend").ok


def test_panel_shows_short_reason_and_log_keeps_the_full_message(tmp_path):
    activity = Activity(tmp_path / "activity.log")
    missing = NXError(
        "NX_NOT_FOUND",
        "Object reference is not valid: obj_123456789",
        details={"mutation_outcome": "rolled_back"},
    )
    run(activity, "nx_blend", "Blend 1 edge R50 mm", error=missing)
    assert activity.recent()[0].endswith("— rolled back, model unchanged (object not found)")
    log = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "NX_NOT_FOUND: Object reference is not valid: obj_123456789" in log


def test_catalogue_envelopes_name_the_result_and_the_model_state(tmp_path):
    from nx_mcp.core.errors import NXError

    activity = Activity(tmp_path / "activity.log")
    activity.start_session()
    ok = {"ok": True, "data": {"feature": {"id": "f:3", "name": "DATUM_PLANE(3)"}}}
    run(activity, "nx_create_datum_plane", "Datum plane 15 mm from top", result=ok)
    failed_read = NXError("NX_STALE_REFERENCE", "b:4 is stale")
    failed_read.details["mutation_outcome"] = "not_applicable"
    run(activity, "nx_list_objects", "List faces", read_only=True, error=failed_read, at=2)
    refused = NXError("NX_PART_NOT_GRANTED", "The part bracket.prt belongs to the engineer: ...")
    refused.details["mutation_outcome"] = "not_started"
    run(activity, "nx_create_datum_plane", "Datum plane", error=refused, at=4)
    lines = activity.recent()
    assert "→ DATUM_PLANE(3)" in lines[0]
    assert lines[1].startswith("✗ ") and "List faces — model unchanged (b:4 is stale)" in lines[1]
    assert lines[2].startswith("✗ ") and "not run, model unchanged" in lines[2]
    assert "(engineer's part, not granted)" in lines[2]
