"""nx_create_sketch, nx_finish_sketch, nx_get_sketch, nx_check_sketch on the fake kit."""

import pytest

from tests.kit import run
from tests.unit.sketch.fakes import CT, Constraint, sketch_nx


def test_principal_xz_sketch_with_origin_and_axis_is_active_and_verified():
    nx = sketch_nx()
    ok = run(
        nx,
        "nx_create_sketch",
        placement={"principal": "xz", "origin": [0, 5, 0], "x_axis": [0, 0, 1]},
        name="SIDE",
    )
    assert ok.ok, ok
    assert ok.data["frame"]["normal"] == pytest.approx([0, -1, 0])
    assert ok.data["frame"]["x_axis"] == pytest.approx([0, 0, 1])
    assert ok.data["frame"]["y_axis"] == pytest.approx([-1, 0, 0])
    assert ok.data["frame"]["origin"] == pytest.approx([0, 5, 0])
    assert ok.data["active"] is True and ok.data["sketch"]["kind"] == "sketch"
    sketch = nx.part.Sketches[-1]
    assert nx.session.ActiveSketch is sketch and sketch.Name == "SIDE"
    assert nx.live_builders == 0 and ok.mutation_outcome == "committed"
    assert [r["kind"] for r in ok.changes["created"]] == ["sketch"]


def test_frame_mismatch_rolls_back():
    nx = sketch_nx()
    nx.next_sketch = ((0, 0, 1), (1, 0, 0), (0, 1, 0))  # NX ignored the origin
    bad = run(nx, "nx_create_sketch", placement={"principal": "xy"})
    assert (
        bad.error["category"] == "verification_failed" and bad.error["details"]["check"] == "origin"
    )
    assert bad.mutation_outcome == "rolled_back" and list(nx.part.Sketches) == []
    assert nx.session.ActiveSketch is None


@pytest.mark.parametrize(
    ("placement", "field"),
    [
        ({"principal": "xy", "x_axis": [0, 0, 1]}, "/placement/x_axis"),
        ({"principal": "xy", "x_axis": [0, 0, 0]}, "/placement/x_axis"),
        ({"principal": "xy", "plane": "XY"}, "/placement/plane"),
    ],
)
def test_invalid_placements_are_not_started(placement, field):
    nx = sketch_nx()
    bad = run(nx, "nx_create_sketch", placement=placement)
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_create_refuses_while_another_sketch_is_active():
    nx = sketch_nx()
    nx.sketch(active=True)
    bad = run(nx, "nx_create_sketch", placement={"principal": "xy"})
    assert bad.error["code"] == "NX_SKETCH_ACTIVE" and "nx_finish_sketch" in bad.error["hint"]


def test_sketch_on_face_with_x_direction_uses_a_coincident_plane():
    nx = sketch_nx()
    top = nx.block(10, 10, 5).face(normal=(0, 0, 1))
    nx.next_sketch = ((0, 0, 5), (0, 1, 0), (-1, 0, 0))
    ok = run(nx, "nx_create_sketch", placement={"on": top.ref, "x_direction": [0, 1, 0]})
    assert ok.ok, ok
    b = nx.last("SketchInPlaceBuilder2")
    assert b.PlaneOption == "NewPlane" and b.OriginOption == "SpecifyPoint"
    tilted = run(nx, "nx_finish_sketch", sketch=ok.data["sketch"]["id"])
    assert tilted.ok
    bad = run(nx, "nx_create_sketch", placement={"on": top.ref, "x_direction": [0, 0, 1]})
    assert bad.error["field"] == "/placement/x_direction"
    edge = nx.block(1, 1, 1).edges[0]
    wrong = run(nx, "nx_create_sketch", placement={"on": edge.ref})
    assert wrong.error["category"] == "wrong_kind"


def test_path_sketch_checks_the_origin_on_the_path():
    nx = sketch_nx()
    edge = nx.model_edge((0, 0, 0), (10, 0, 0))
    nx.next_sketch = ((5, 0, 0), (0, 1, 0), (0, 0, 1))  # normal +X along the edge
    ok = run(
        nx,
        "nx_create_sketch",
        placement={"path": [edge.ref], "help_point": [0, 0, 0], "percent": 50},
    )
    assert ok.ok, ok
    b = nx.last("SketchAlongPathBuilder")
    assert b.PlaneLocation.Expression.RightHandSide == "50.0"
    nx.session.ActiveSketch.Deactivate()
    nx.next_sketch = ((5, 3, 0), (0, 1, 0), (0, 0, 1))  # off the path
    bad = run(nx, "nx_create_sketch", placement={"path": [edge.ref], "help_point": [0, 0, 0]})
    assert bad.error["details"]["check"] == "origin_on_path"


def test_finish_deactivates_and_is_a_no_op_for_an_inactive_sketch():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    ok = run(nx, "nx_finish_sketch", sketch=sk.ref)
    assert ok.ok and ok.data["was_active"] is True and nx.session.ActiveSketch is None
    again = run(nx, "nx_finish_sketch", sketch=sk.ref)
    assert again.ok and again.data["was_active"] is False
    other = nx.sketch(active=True)
    bad = run(nx, "nx_finish_sketch", sketch=sk.ref)
    assert bad.error["code"] == "NX_SKETCH_ACTIVE"
    assert nx.session.ActiveSketch is other


def test_get_sketch_reads_geometry_constraints_and_status_without_leaving_it_active():
    nx = sketch_nx()
    sk = nx.sketch(origin=(0, 0, 10))
    line = nx.line(sk, (0, 0, 10), (10, 0, 10))
    nx.arc(sk, (20, 0, 10), 5, 0, 90)
    sk.constraints.append(Constraint(CT.Horizontal, [(line, "NotSet")], sk))
    ok = run(nx, "nx_get_sketch", sketch=sk.ref)
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    rows = ok.data["curves"]
    assert rows[0]["type"] == "line" and rows[0]["end"] == pytest.approx([10, 0])
    assert rows[0]["part"]["end"] == pytest.approx([10, 0, 10])
    assert rows[1]["type"] == "arc" and rows[1]["radius"] == 5
    assert rows[1]["start_angle"] == pytest.approx(0) and rows[1]["end_angle"] == pytest.approx(90)
    (con,) = ok.data["constraints"]
    assert con["type"] == "horizontal" and con["curves"] == [rows[0]["curve"]["id"]]
    assert ok.data["status"] == {"state": "under_constrained", "dof": 1}
    assert nx.session.ActiveSketch is None and nx.undo_marks == []
    only = run(nx, "nx_get_sketch", sketch=sk.ref, include=["geometry"])
    assert "status" not in only.data and "constraints" not in only.data


def test_check_sketch_reports_relieving_constraints():
    nx = sketch_nx()
    sk = nx.sketch()
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    h = Constraint(CT.Horizontal, [(line, "NotSet")], sk)
    v = Constraint(CT.Vertical, [(line, "NotSet")], sk)
    sk.constraints += [h, v]
    ok = run(nx, "nx_check_sketch", sketch=sk.ref)
    assert ok.ok, ok
    assert len(ok.data["explicit_conflicts"]) == 1 and ok.data["checked"] == 2
    assert len(ok.data["relieving"]) == 2 and ok.data["complete"] is True
    assert sk.constraints == [h, v] and nx.undo_marks == []
    limited = run(nx, "nx_check_sketch", sketch=sk.ref, max_checks=1)
    assert limited.data["checked"] == 1 and limited.data["complete"] is False
    sk.constraints.remove(v)
    calm = run(nx, "nx_check_sketch", sketch=sk.ref)
    assert calm.data["trials"] == [] and calm.data["state"] == "under_constrained"
