import json

import pytest

from nx_mcp.core.catalogue import hooks
from nx_mcp.domains.assembly import exploded
from tests.kit import run
from tests.unit.assembly.fakes import STATUS, assembly_nx, place

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
I3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def flat(m):
    return [v for row in m for v in row]


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.a = place(nx, nx.blk, "A")
    nx.b = place(nx, nx.blk, "B", (30, 0, 0))
    nx.sub = place(nx, nx.pin, "SUB")
    nx.n = nx.assembly.add(nx.blk, "N", ([0, 10, 0], I3), parent=nx.sub)
    nx.n.ref = nx.ref_of(nx.n, "component")
    return nx


def explosion(nx, name="E1"):
    ok = run(nx, "nx_create_explosion", name=name)
    assert ok.ok, ok
    return ok.data["explosion"]["id"]


def test_create_explosion_starts_at_assembled_poses(nx):
    ok = run(nx, "nx_create_explosion", name="E1")
    assert ok.ok and ok.data["component_count"] == 4 and ok.data["views"] == []
    rows = {r["id"]: r for r in ok.data["components"]}
    assert set(rows) == {nx.a.ref, nx.b.ref, nx.sub.ref, nx.n.ref}
    assert rows[nx.n.ref]["path"] == "SUB/N"
    assert "nx_edit_explosion" in ok.warnings[0]["message"]  # nothing is exploded yet
    dup = run(nx, "nx_create_explosion", name="e1")
    assert dup.error["code"] == "NX_NAME_CONFLICT" and dup.mutation_outcome == "not_started"
    assert dup.error["details"]["existing"]["id"] == ok.data["explosion"]["id"]
    assert "nx_edit_explosion" in dup.error["hint"]
    spaced = run(nx, "nx_create_explosion", name="E2 ")
    assert spaced.error["field"] == "/name"
    nx.session.Parts.Display = nx.blk
    other = run(nx, "nx_create_explosion", name="E3")
    assert other.error["code"] == "NX_WRONG_PART_CONTEXT"


def test_create_explosion_with_initial_placements(nx):
    ok = run(nx, "nx_create_explosion", name="E1", placements=[
        {"component": nx.b.ref, "offset": [0, 0, 40]},
        {"component": nx.n.ref, "pose": {"translation": [0, 0, 100], "rotation": RZ90}},
    ])  # fmt: skip
    assert ok.ok and not ok.warnings, ok
    assert ok.data["moved_count"] == 2 and ok.data["component_count"] == 4
    rows = {r["component"]["id"]: r for r in ok.data["placed"]}
    assert rows[nx.b.ref]["translation"] == pytest.approx([30, 0, 40])
    assert rows[nx.b.ref]["assembled_translation"] == [30, 0, 0] and nx.b.t == [30, 0, 0]
    assert flat(rows[nx.n.ref]["rotation"]) == pytest.approx(flat(RZ90))
    assert nx.assembly.explosions[0].pose(nx.n)[0] == pytest.approx([0, 0, 100])


def test_create_explosion_refuses_bad_placements_before_creating(nx):
    nx.b.IsSuppressed = True
    cases = [
        ([{"component": nx.b.ref, "offset": [0, 0, 1]}], "/placements/0/component"),
        ([{"component": nx.a.ref, "offset": [0, 0, 1]}, {"component": nx.a.ref,
          "offset": [1, 0, 0]}], "/placements/1/component"),
        ([{"component": nx.a.ref}], "/placements/0"),
    ]  # fmt: skip
    for placements, field in cases:
        bad = run(nx, "nx_create_explosion", name="E1", placements=placements)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not nx.assembly.explosions
    api = nx.uf.Assem
    explode = api.ExplodeComponent
    api.ExplodeComponent = lambda e, c, m: explode(
        e, c, [r[:3] + [r[3] + 1] for r in m[:3]] + [m[3]]
    )
    drift = run(nx, "nx_create_explosion", name="E1",
                placements=[{"component": nx.a.ref, "offset": [0, 0, 5]}])  # fmt: skip
    assert drift.mutation_outcome == "rolled_back" and not nx.assembly.explosions


def test_edit_explosion_sets_absolute_nested_poses_and_keeps_assembled_ones(nx):
    ex = explosion(nx)
    ok = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.sub.ref, "pose": {"translation": [0, 0, 50]}},
        {"component": nx.n.ref, "pose": {"translation": [0, 0, 100], "rotation": RZ90}},
        {"component": nx.b.ref, "pose": {"translation": [60, 0, 0], "rotation": RZ90}},
    ])  # fmt: skip
    assert ok.ok, ok
    rows = {r["component"]["id"]: r for r in ok.data["components"]}
    assert rows[nx.n.ref]["translation"] == pytest.approx([0, 0, 100])
    assert flat(rows[nx.n.ref]["rotation"]) == pytest.approx(flat(RZ90))
    assert rows[nx.b.ref]["assembled_translation"] == [30, 0, 0] and nx.b.t == [30, 0, 0]
    assert ok.data["moved_count"] == 3
    # Resetting the subassembly drops only its own post-transform.
    reset = run(nx, "nx_edit_explosion", explosion=ex, reset_components=[nx.sub.ref])
    assert reset.ok, reset
    info = run(nx, "nx_get_explosion", explosion=ex, limit=10)
    got = {r["component"]["id"]: r for r in info.data["components"]}
    assert got[nx.sub.ref]["translation"] == [0, 0, 0]
    assert got[nx.n.ref]["translation"] == pytest.approx([0, 0, 50])
    again = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.b.ref, "pose": {"translation": [60, 0, 0], "rotation": RZ90}}])  # fmt: skip
    assert again.ok and again.data["moved_count"] == 0


def test_edit_explosion_offsets_move_from_the_unexploded_pose(nx):
    ex = explosion(nx)
    ok = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.n.ref, "offset": [5, 0, 0]},
        {"component": nx.sub.ref, "offset": [0, 0, 50]},
        {"component": nx.b.ref, "offset": [0, 0, 10]},
    ])  # fmt: skip
    assert ok.ok, ok
    rows = {r["component"]["id"]: r for r in ok.data["components"]}
    assert rows[nx.sub.ref]["translation"] == pytest.approx([0, 0, 50])
    # A nested component moves from where its exploded parent carries it.
    assert rows[nx.n.ref]["translation"] == pytest.approx([5, 10, 50])
    assert rows[nx.b.ref]["translation"] == pytest.approx([30, 0, 10])
    assert rows[nx.b.ref]["assembled_translation"] == [30, 0, 0] and ok.data["moved_count"] == 3
    # An offset is from the unexploded pose, not added to the current exploded one.
    again = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.b.ref, "offset": [0, 0, 10]}])  # fmt: skip
    assert again.ok and again.data["moved_count"] == 0
    back = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.b.ref, "offset": [0, 0, 0]}])  # fmt: skip
    assert back.data["components"][0]["translation"] == pytest.approx([30, 0, 0])


def test_edit_explosion_rejections_change_nothing(nx):
    ex = explosion(nx)
    nothing = run(nx, "nx_edit_explosion", explosion=ex)
    assert nothing.error["code"] == "NX_INVALID_ARGUMENT" and "offset" in nothing.error["hint"]
    both = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.a.ref, "offset": [0, 0, 1], "pose": {"translation": [0, 0, 1]}}])  # fmt: skip
    assert both.error["field"] == "/placements/0" and both.mutation_outcome == "not_started"
    neither = run(nx, "nx_edit_explosion", explosion=ex, placements=[{"component": nx.a.ref}])
    assert neither.error["field"] == "/placements/0"
    twice = run(nx, "nx_edit_explosion", explosion=ex, reset_components=[nx.a.ref],
                placements=[{"component": nx.a.ref, "pose": {"translation": [1, 0, 0]}}])  # fmt: skip
    assert twice.error["field"] == "/placements/0/component"
    nx.b.IsSuppressed = True
    hidden = run(nx, "nx_edit_explosion", explosion=ex, reset_components=[nx.b.ref])
    assert hidden.error["field"] == "/reset_components/0"
    skew = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.a.ref, "pose": {"translation": [1, 0, 0],
                                         "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, -1]]}}])  # fmt: skip
    assert skew.error["field"] == "/placements/0/pose/rotation"
    assert all(not e.deltas for e in nx.assembly.explosions)


def test_explosion_readback_failure_rolls_back(nx):
    ex = explosion(nx)
    api = nx.uf.Assem
    explode = api.ExplodeComponent
    api.ExplodeComponent = lambda e, c, m: explode(
        e, c, [r[:3] + [r[3] + 1] for r in m[:3]] + [m[3]]
    )
    bad = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.a.ref, "pose": {"translation": [0, 0, 40]}}])  # fmt: skip
    assert bad.error["details"]["check"].startswith("placements[0]")
    assert bad.mutation_outcome == "rolled_back"
    assert not nx.assembly.explosions[0].deltas


def test_display_attaches_and_detaches_an_explosion(nx):
    ex = explosion(nx)
    view = nx.asm.ModelingViews.WorkView
    ok = run(nx, "nx_set_explosion_display", explosion=ex)
    assert ok.ok and nx.assembly.view_explosion[int(view.Tag)] == int(nx.assembly.explosions[0].Tag)
    assert ok.data["view_kind"] == "modeling_view" and nx.fitted
    listed = hooks("lister")["explosion"](nx.tx, None, None)
    assert listed[0][2]["details"]["views"] == [ok.data["view"]["id"]]
    assembled = run(nx, "nx_set_explosion_display")
    assert assembled.ok and nx.assembly.view_explosion[int(view.Tag)] == 0
    both = run(nx, "nx_set_explosion_display", drawing_view="v:1", model_view="mv:2")
    assert both.error["field"] == "/model_view"


def test_delete_refuses_an_explosion_in_use(nx):
    ex = explosion(nx)
    run(nx, "nx_set_explosion_display", explosion=ex)
    obj = nx.assembly.explosions[0]
    with pytest.raises(Exception) as error:
        hooks("deleter")["explosion"](nx.tx, [obj])
    assert error.value.code == "NX_IN_USE"
    run(nx, "nx_set_explosion_display")
    hooks("deleter")["explosion"](nx.tx, [obj])
    assert not nx.assembly.explosions


def trace(nx, ex, start=0, end=0):
    e1 = nx.a.edge([0, 0, 10], [40, 0, 10])
    e2 = nx.b.edge([0, 0, 0], [0, 20, 0])
    return run(nx, "nx_create_explosion_trace", explosion=ex, start_edge=nx.ref_of(e1, "edge"),
               end_edge=nx.ref_of(e2, "edge"), start_percent=start, end_percent=end)  # fmt: skip


def test_trace_anchors_follow_explosion_edits(nx):
    ex = explosion(nx)
    ok = trace(nx, ex, 25, 100)
    assert ok.ok, ok
    assert ok.data["start"] == pytest.approx([10, 0, 10]) and ok.data["end"] == pytest.approx(
        [30, 20, 0]
    )
    line = nx.asm.Tracelines[0]
    records = json.loads(line.GetStringAttribute(exploded.TRACE_ATTRIBUTE))
    assert [r["percent"] for r in records] == [25, 100]
    moved = run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.a.ref, "pose": {"translation": [0, 0, 40]}}])  # fmt: skip
    assert moved.ok
    assert exploded.endpoints(line)[0] == pytest.approx([10, 0, 50])
    edited = run(nx, "nx_edit_explosion_trace", trace=ok.data["trace"]["id"], start_percent=50,
                 end_offset=3)  # fmt: skip
    assert edited.ok and edited.data["start"] == pytest.approx([20, 0, 50])
    assert edited.data["end_offset"] == 3 and edited.data["start_percent"] == 50
    assert edited.data["trace"]["name"] == "TRACE1"  # NX would rename it
    assert edited.data["start_edge"] == ok.data["start_edge"]
    got = run(nx, "nx_get_explosion", explosion=ex)
    assert got.data["traces"] == [ok.data["trace"]]


def test_trace_result_names_its_anchors(nx):
    ex = explosion(nx)
    first, second = trace(nx, ex, 25, 100), trace(nx, ex)
    assert first.ok and second.ok, second
    data = first.data
    assert [data["trace"]["name"], second.data["trace"]["name"]] == ["TRACE1", "TRACE2"]
    assert data["start_edge"]["kind"] == "edge" and data["end_edge"]["kind"] == "edge"
    assert data["start_edge"]["id"] != data["end_edge"]["id"]
    assert (data["start_percent"], data["end_percent"]) == (25, 100)
    assert data["start_direction"] == [0, 0, 1] and data["end_direction"] == [0, 0, 1]
    assert data["start_offset"] == 0 and data["end_offset"] == 0


def test_trace_rules(nx):
    ex = explosion(nx)
    own = nx.ref_of(nx.blk.Bodies[0] if nx.blk.Bodies else nx.a.face(), "face")
    bad = run(nx, "nx_create_explosion_trace", explosion=ex, start_edge=own, end_edge=own)
    assert bad.error["code"] == "NX_WRONG_KIND"
    nx.b.IsSuppressed = True
    hidden = trace(nx, ex)
    assert hidden.error["field"] == "/end_edge" and hidden.mutation_outcome == "not_started"
    nothing = run(nx, "nx_edit_explosion_trace", trace="tl:999")
    assert nothing.error["code"] == "NX_INVALID_ARGUMENT"


def test_animation_renders_frames_and_restores_poses(nx, monkeypatch):
    from nx_mcp.kit import capture

    ex = explosion(nx)
    run(nx, "nx_edit_explosion", explosion=ex, placements=[
        {"component": nx.b.ref, "pose": {"translation": [30, 0, 60], "rotation": RZ90}}])  # fmt: skip
    trace(nx, ex)
    seen = []

    def render(tx, path, width, height, **_kw):
        b = nx.assembly.explosions[0].pose(nx.b)
        seen.append(b[0][2])
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + width.to_bytes(4, "big")
                         + height.to_bytes(4, "big"))  # fmt: skip
        return width, height

    monkeypatch.setattr(capture, "studio_png", render)
    ok = run(nx, "nx_export_explosion_animation", explosion=ex, path="out/e.html", frames=3,
             fps=2, width=320, height=240)  # fmt: skip
    assert ok.ok, ok
    assert seen == pytest.approx([0, 30, 60])
    assert ok.data["frame_count"] == 3 and ok.data["path"] == "out/e.html"
    assert ok.data["trace_count"] == 1
    assert (nx.root / "out" / "e.html").read_text().count("data:image/png;base64,") == 3
    assert nx.assembly.explosions[0].pose(nx.b)[0] == pytest.approx([30, 0, 60])
    view = nx.asm.ModelingViews.WorkView
    assert nx.assembly.view_explosion.get(int(view.Tag), 0) == 0
    again = run(nx, "nx_export_explosion_animation", explosion=ex, path="out/e.html")
    assert again.error["code"] == "NX_ALREADY_EXISTS"


def test_animation_failure_leaves_no_file_and_restores_the_view(nx, monkeypatch):
    from nx_mcp.kit import capture

    ex = explosion(nx)

    def fail(*_a, **_k):
        raise RuntimeError("render failed")

    monkeypatch.setattr(capture, "studio_png", fail)
    bad = run(nx, "nx_export_explosion_animation", explosion=ex, path="out/f.html", frames=2)
    assert not bad.ok and not (nx.root / "out" / "f.html").exists()
    view = nx.asm.ModelingViews.WorkView
    assert nx.assembly.view_explosion.get(int(view.Tag), 0) == 0


def test_update_assembly_solves_and_refreshes(nx):
    ok = run(nx, "nx_create_assembly_constraint", type="fix", component=nx.a.ref)
    ex = explosion(nx)
    trace(nx, ex)
    done = run(nx, "nx_update_assembly")
    assert done.ok, done
    assert [c["status"] for c in done.data["constraints"]] == ["Solved"]
    assert len(done.data["traces"]) == 1 and done.data["documentation_complete"]
    nx.assembly.solver_status = STATUS.NotSolved
    bad = run(nx, "nx_update_assembly")
    assert (
        bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED" and bad.mutation_outcome == "rolled_back"
    )
    assert ok.ok


def test_reference_set_keeps_only_requested_members_when_nx_seeds_components(nx):
    """NX 2406 seeds a new assembly reference set with every component."""
    ok = run(nx, "nx_create_reference_set", name="ASMSET", objects=[nx.a.ref])
    assert ok.ok and ok.data["member_count"] == 1
    assert ok.data["members"][0]["id"] == nx.a.ref
    assert nx.asm.reference_sets[0].Name == "ASMSET"
    dup = run(nx, "nx_create_reference_set", name="asmset", objects=[nx.b.ref])
    assert dup.error["code"] == "NX_ALREADY_EXISTS" and dup.mutation_outcome == "not_started"
    builtin = run(nx, "nx_create_reference_set", name="Empty", objects=[nx.b.ref])
    assert builtin.error["field"] == "/name"
    nested = run(nx, "nx_create_reference_set", name="X", objects=[nx.n.ref])
    assert nested.ok  # a nested occurrence still belongs to the work part
    listed = hooks("lister")["reference_set"](nx.tx, None, None)
    assert [r[2]["details"]["member_count"] for r in listed] == [1, 1]


def _body(nx, component):
    """A body of ``component``'s part and its occurrence in the assembly."""
    from tests.kit.objects import Body

    proto = Body("BODY")
    proto.OwningPart, proto.IsOccurrence = component.Prototype, False
    component.Prototype.Bodies.append(proto)
    occ = Body("BODY")
    occ.OwningPart, occ.IsOccurrence = nx.asm, True
    occ.OwningComponent, occ.Prototype = component, proto
    component.occurrences[int(proto.Tag)] = occ
    return nx.ref_of(proto, "body"), nx.ref_of(occ, "body"), proto


def test_reference_set_in_a_component_part_restores_the_work_part(nx):
    proto_ref, occ_ref, proto = _body(nx, nx.b)
    proto2_ref, occ2_ref, proto2 = _body(nx, nx.a)  # a blk body shown by A (B's part too)
    parts = nx.session.Parts
    other = run(nx, "nx_create_reference_set", name="LIGHT", objects=[occ_ref, occ2_ref],
                component=nx.b.ref)  # fmt: skip
    assert other.error["code"] == "NX_WRONG_OWNER" and other.error["field"] == "/objects/1"
    ok = run(nx, "nx_create_reference_set", name="LIGHT", objects=[occ_ref, proto2_ref],
             component=nx.b.ref)  # fmt: skip
    assert ok.ok, ok
    rs = nx.blk.reference_sets[-1]
    assert rs.Name == "LIGHT" and rs.members == [proto, proto2] and rs.created_in_work is nx.blk
    assert [m["id"] for m in ok.data["members"]] == [proto_ref, proto2_ref]
    assert parts.Work is nx.asm and parts.WorkComponent is None
    assert parts.work_calls == [nx.b, None]
    assert nx.asm.reference_sets == []
    dup = run(nx, "nx_create_reference_set", name="solid", objects=[occ_ref], component=nx.b.ref)
    assert dup.error["code"] == "NX_ALREADY_EXISTS" and dup.mutation_outcome == "not_started"
    twice = run(nx, "nx_create_reference_set", name="X", objects=[occ_ref, proto_ref],
                component=nx.b.ref)  # fmt: skip
    assert twice.error["field"] == "/objects/1"
    comp = run(nx, "nx_create_reference_set", name="X", objects=[nx.a.ref], component=nx.b.ref)
    assert comp.error["code"] == "NX_UNSUPPORTED_OPTION"
    plain = run(nx, "nx_create_reference_set", name="X", objects=[occ_ref])
    assert plain.error["code"] == "NX_WRONG_OWNER" and "component=" in plain.error["hint"]


def test_reference_set_in_a_component_rolls_back_and_restores_on_failure(nx):
    _proto_ref, occ_ref, _proto = _body(nx, nx.b)
    create = nx.blk.CreateReferenceSet

    def lossy():
        rs = create()
        rs.AddObjectsToReferenceSet = lambda objs: rs.members.clear()
        return rs

    nx.blk.CreateReferenceSet = lossy
    bad = run(nx, "nx_create_reference_set", name="LOSSY", objects=[occ_ref], component=nx.b.ref)
    assert bad.error["details"]["check"] == "members" and bad.mutation_outcome == "rolled_back"
    assert [r.Name for r in nx.blk.reference_sets] == ["SOLID"]
    assert nx.session.Parts.Work is nx.asm and nx.session.Parts.WorkComponent is None


def test_reference_set_membership_mismatch_rolls_back(nx):
    create = nx.asm.CreateReferenceSet

    def lossy():
        rs = create()
        rs.AddObjectsToReferenceSet = lambda objs: rs.members.clear()
        return rs

    nx.asm.CreateReferenceSet = lossy
    bad = run(nx, "nx_create_reference_set", name="LOSSY", objects=[nx.a.ref])
    assert bad.error["details"]["check"] == "members" and bad.mutation_outcome == "rolled_back"


def test_component_and_trace_deleters_and_listers(nx):
    with pytest.raises(Exception) as error:
        hooks("deleter")["component"](nx.tx, [nx.n])
    assert error.value.code == "NX_WRONG_OWNER"
    hooks("deleter")["component"](nx.tx, [nx.b])
    assert nx.b in nx.assembly.deleted
    run(nx, "nx_create_assembly_constraint", type="fix", component=nx.a.ref)
    rows = hooks("lister")["assembly_constraint"](nx.tx, None, None)
    assert rows[0][2]["type"] == "fix" and rows[0][2]["details"]["components"] == [nx.a.ref]
    made = run(nx, "nx_create_component_pattern", component=nx.a.ref,
               layout={"linear": {"direction": [1, 0, 0], "count": 2, "spacing": 5}})  # fmt: skip
    assert made.ok
    patterns = hooks("lister")["component_pattern"](nx.tx, None, None)
    assert patterns[0][2]["type"] == "linear" and patterns[0][2]["details"]["count"] == 2
