"""nx_find_holes: hole ends, length, through/blind, form and the diameter/through filters,
on fake bores bounded by circular edges (their neighbours decide open or closed ends)."""

import math

from tests.kit import run
from tests.unit.inspection.fakes import arc_spec, inspection_nx, spec_curve


def _face(nx, body, kind, normal, origin):
    face = nx.remember(nx.face(normal=normal, origin=origin, kind=kind, body=body))
    face.sense = 1
    return face


def _top(body, sign=1):
    return next(f for f in body.faces if list(f.normal) == [0, 0, sign])


def _ring(nx, center, radius, *faces):
    edge = spec_curve(nx, arc_spec(center, radius, 0, 2 * math.pi))
    edge.faces = list(faces)
    edge.GetFaces = lambda e=edge: e.faces
    return edge


def _bore(nx, body, x, y, radius, low, high, below, above):
    """A bore face from z=low to z=high (axis +Z) with ``below``/``above`` beyond its rings."""
    face = _face(nx, body, "cylinder", (0, 0, 1), (x, y, low))
    face.radius, face.sense, face.uv_box = radius, -1, (0.0, 2 * math.pi, 0.0, 1.0)
    rings = [_ring(nx, (x, y, low), radius, face, below), _ring(nx, (x, y, high), radius, face)]
    rings[1].faces.append(above)
    face.GetEdges = lambda: rings
    body.faces.append(face)
    return face, rings


def _plate():
    """A 100x20x10 plate with a through hole, a blind hole, a counterbore, a drilled
    (cone tip) blind hole and a countersunk through hole."""
    nx = inspection_nx()
    body = nx.block(100, 20, 10)
    top, bottom = _top(body), _top(body, -1)
    _bore(nx, body, 10, 10, 3.0, 0, 10, bottom, top)  # simple through, d 6
    floor = _face(nx, body, "plane", (0, 0, 1), (30, 10, 6))
    _bore(nx, body, 30, 10, 2.0, 6, 10, floor, top)  # blind, flat floor at z=6
    step = _face(nx, body, "plane", (0, 0, 1), (50, 10, 7))
    _bore(nx, body, 50, 10, 5.0, 7, 10, step, top)  # counterbore d 10 x 3 deep
    _bore(nx, body, 50, 10, 2.5, 0, 7, bottom, step)  # its d 5 through drill
    tip = _face(nx, body, "cone", (0, 0, 1), (70, 10, 4))
    tip.radius, tip.half_angle = 1.5, math.radians(59)
    _face_tip, rings = _bore(nx, body, 70, 10, 1.5, 4, 10, tip, top)  # drilled, tip below z=4
    tip.GetEdges = lambda: [rings[0]]
    sink = _face(nx, body, "cone", (0, 0, 1), (90, 10, 8))
    sink.radius, sink.half_angle = 2.0, math.radians(45)
    _sunk, sunk_rings = _bore(nx, body, 90, 10, 2.0, 0, 8, bottom, sink)  # countersunk
    mouth = _ring(nx, (90, 10, 10), 4.0, sink, top)
    sink.GetEdges = lambda: [sunk_rings[1], mouth]
    return nx, body


def _by_x(groups):
    return {round(g["axis_point"][0]): g for g in groups}


def test_holes_report_ends_length_through_and_form():
    nx, body = _plate()
    ok = run(nx, "nx_find_holes", owner=body.ref)
    assert ok.ok, ok
    holes = _by_x(ok.data["groups"])
    assert set(holes) == {10, 30, 50, 70, 90}
    simple = holes[10]
    assert simple["form"] == "simple" and simple["through"] is True
    assert simple["diameters"] == [6.0] and simple["length"] == 10
    assert simple["ends"][0] == {"point": [10.0, 10.0, 10.0], "diameter": 6.0, "open": True}
    blind = holes[30]
    assert blind["through"] is False and blind["length"] == 4 and blind["form"] == "simple"
    assert [e["open"] for e in blind["ends"]] == [True, False]
    assert blind["ends"][0]["point"] == [30.0, 10.0, 10.0]
    cbore = holes[50]
    assert cbore["form"] == "counterbore" and cbore["through"] is True
    assert cbore["diameters"] == [10.0, 5.0] and cbore["length"] == 10
    assert [e["diameter"] for e in cbore["ends"]] == [10.0, 5.0]
    assert cbore["ends"][0]["point"][2] == 10 and len(cbore["faces"]) == 2
    drilled = holes[70]
    assert drilled["through"] is False and drilled["ends"][1]["open"] is False
    sunk = holes[90]
    assert sunk["form"] == "countersink" and sunk["through"] is True


def test_holes_filter_by_diameter_and_through():
    nx, body = _plate()
    five = run(nx, "nx_find_holes", diameter="5 mm")
    assert five.ok, five
    assert [round(g["axis_point"][0]) for g in five.data["groups"]] == [50]
    assert five.page["total"] == 2  # both faces of the counterbore
    six = run(nx, "nx_find_holes", diameter=6)
    assert [g["diameters"] for g in six.data["groups"]] == [[6.0]]
    blind = run(nx, "nx_find_holes", through=False)
    assert sorted(round(g["axis_point"][0]) for g in blind.data["groups"]) == [30, 70]
    through = run(nx, "nx_find_holes", through=True, diameter=4)
    assert [round(g["axis_point"][0]) for g in through.data["groups"]] == [90]
    none = run(nx, "nx_find_holes", diameter=7)
    assert none.ok and none.data["groups"] == [] and none.page["total"] == 0
    bad = run(nx, "nx_find_holes", diameter=0)
    assert bad.error["field"] == "/diameter"


def test_holes_without_edges_keep_unknown_shape():
    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    face = _face(nx, body, "cylinder", (0, 0, 1), (10, 10, 0))
    face.radius, face.sense, face.uv_box = 3.0, -1, (0.0, 2 * math.pi, 0.0, 1.0)
    face.GetEdges = lambda: []
    body.faces.append(face)
    (group,) = run(nx, "nx_find_holes").data["groups"]
    assert group["length"] is None and group["through"] is None and group["ends"] == []
    assert group["form"] == "simple"
    assert run(nx, "nx_find_holes", through=True).data["groups"] == []


def test_holes_owner_takes_an_anchor_and_refuses_names():
    nx, body = _plate()
    anchor = run(nx, "nx_create_anchor", object=body.ref).data["anchor"]
    ok = run(nx, "nx_find_holes", owner=anchor)
    assert ok.ok and len(ok.data["groups"]) == 5, ok
    named = run(nx, "nx_find_holes", owner="SIMPLE HOLE(3)")
    assert named.error["code"] == "NX_INVALID_ARGUMENT" and named.error["field"] == "/owner"
    assert "f:12" in named.error["message"] and "anchor" in named.error["message"]
