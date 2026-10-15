"""Sketch size guard, duplicates, solver side effects and clearer refusals (fake kit)."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.sketch import operations
from nx_mcp.kit import sketch as ksketch
from tests.kit import run
from tests.kit.objects import Feature
from tests.unit.sketch.fakes import CT, Constraint, P, sketch_nx


def _curves(nx, sk, *curves, **kw):
    return run(nx, "nx_create_sketch_curves", sketch=sk.ref, curves=list(curves), **kw)


# ---- size guard (NX's blocking "Large Sketch Performance" question) -------------------


def test_too_many_curves_in_one_call_are_refused_before_nx():
    nx = sketch_nx()
    sk = nx.sketch()
    gon = {"type": "polygon", "center": [0, 0], "sides": 64, "radius": 4}
    bad = _curves(nx, sk, *[gon] * 8)
    assert bad.error["code"] == "NX_TOO_MANY_OBJECTS" and bad.error["field"] == "/curves", bad
    assert bad.error["details"]["adding"] == 512 and bad.mutation_outcome == "not_started"
    assert sk.geometry == []


def test_a_sketch_that_would_grow_past_the_limit_is_refused():
    nx = sketch_nx()
    sk = nx.sketch()
    for k in range(ksketch.MAX_CURVES - 3):
        nx.line(sk, (k, 0, 0), (k, 1, 0))
    rect = {"type": "rectangle", "corner1": [0, 5], "corner2": [4, 9]}
    bad = _curves(nx, sk, rect)
    assert bad.error["code"] == "NX_TOO_MANY_OBJECTS" and bad.mutation_outcome == "not_started"
    assert bad.error["details"] == {"curves": 497, "adding": 4, "limit": ksketch.MAX_CURVES}
    ok = _curves(nx, sk, {"type": "line", "start": [0, 5], "end": [4, 5]})
    assert ok.ok, ok


def test_one_call_sketch_refuses_an_oversized_profile():
    nx = sketch_nx()
    gon = {"type": "polygon", "center": [0, 0], "sides": 64, "radius": 4}
    bad = run(nx, "nx_create_sketch", placement="xy", curves=[gon] * 8)
    assert bad.error["code"] == "NX_TOO_MANY_OBJECTS" and bad.mutation_outcome == "not_started"
    assert list(nx.part.Sketches) == []


# ---- joining separate items, item errors ---------------------------------------------


def test_connect_joins_coinciding_ends_of_separate_items():
    nx = sketch_nx()
    sk = nx.sketch()
    items = [
        {"type": "line", "start": [0, 0], "end": [40, 0]},
        {"type": "line", "start": [40, 0], "end": [40, 20]},
        {"type": "polyline", "points": [[40, 20], [0, 20], [0, 0]]},
    ]
    plain = _curves(nx, sk, *items)
    assert plain.ok and "connected" not in plain.data
    joined_before = len(sk.constraints)
    other = nx.sketch()
    ok = _curves(nx, other, *items, connect=True)
    assert ok.ok, ok
    assert ok.data["connected"] == 3  # line-line, line-polyline, polyline-line
    coincident = [c for c in other.constraints if c.ConstraintType == CT.Coincident]
    assert len(coincident) == 3 + 1  # the polyline's own joint too
    assert joined_before == 1  # without connect: the polyline's joint only


def test_item_errors_list_every_invalid_field_and_name_the_real_schema():
    nx = sketch_nx()
    sk = nx.sketch()
    bad = _curves(nx, sk, {"type": "rectangle", "center": [0, 100], "corner2": [10, 10]})
    assert bad.error["field"].startswith("/curves/0"), bad
    assert "nx_create_sketch_curves#rectangle" in bad.error["hint"]
    if "details.errors" in bad.error["message"]:
        assert bad.error["details"]["errors"]
    one = run(
        nx, "nx_create_sketch", placement="xy",
        curves=[{"type": "polygon", "center": [0, 0], "sides": 6, "radius": "8 mm"}],
    )  # fmt: skip
    assert "nx_create_sketch_curves#polygon" in one.error["hint"], one


# ---- names, open sketches --------------------------------------------------------------


def test_a_duplicate_sketch_name_is_refused_with_the_existing_sketch():
    nx = sketch_nx()
    first = nx.sketch()
    first.Name = "PROF_XY"
    bad = run(nx, "nx_create_sketch", placement="xy", name="prof_xy")
    assert bad.error["code"] == "NX_NAME_CONFLICT" and bad.error["field"] == "/name", bad
    assert bad.error["details"]["existing"] == first.ref
    assert bad.mutation_outcome == "not_started" and len(nx.part.Sketches) == 1


def test_get_sketch_reads_geometry_while_another_sketch_is_open():
    nx = sketch_nx()
    sk = nx.sketch()
    nx.line(sk, (0, 0, 0), (10, 0, 0))
    other = nx.sketch(active=True)
    other.Name = "B_OPEN"
    ok = run(nx, "nx_get_sketch", sketch=sk.ref)
    assert ok.ok, ok
    assert len(ok.data["curves"]) == 1 and "status" not in ok.data and "constraints" in ok.data
    assert any("B_OPEN" in w["message"] for w in ok.warnings)
    bad = run(nx, "nx_get_sketch", sketch=sk.ref, include=["status"])
    assert bad.error["code"] == "NX_SKETCH_ACTIVE"
    assert bad.error["details"]["open_sketch"] == other.ref


def test_get_sketch_flags_projected_curves():
    nx = sketch_nx()
    sk = nx.sketch()
    drawn = nx.line(sk, (0, 0, 0), (10, 0, 0))
    projected = nx.line(sk, (0, 5, 0), (10, 5, 0))
    edge = nx.model_edge((0, 5, 3), (10, 5, 3))
    sk.IsIncludedGeometry = lambda c: c is projected
    sk.GetParentOfIncludedGeometry = lambda c: edge
    ok = run(nx, "nx_get_sketch", sketch=sk.ref, include=["geometry"])
    rows = {r["curve"]["id"]: r for r in ok.data["curves"]}
    assert "projected" not in rows[nx.cref(drawn)]
    assert rows[nx.cref(projected)]["projected"] is True
    assert rows[nx.cref(projected)]["source"] == edge.ref


def _projection_feature(curves, sources):
    """A fake NX 2406 projection feature (``CPROJ``): its curves and section edges."""
    elements = [NS(GetSectionElementData=lambda o=o: (o, None, None, None, None)) for o in sources]
    data = NS(GetSectionElementsData=lambda: elements)
    feature = Feature("SKETCH(1:2B)")
    feature.FeatureType = "CPROJ"
    feature.GetEntities = lambda: list(curves)
    feature.GetSections = lambda: [NS(GetSectionData=lambda: [data])]
    return feature


def test_get_sketch_flags_curves_of_projection_features_with_their_edges():
    nx = sketch_nx()
    sk = nx.sketch()
    drawn = nx.line(sk, (0, 0, 0), (10, 0, 0))
    a = nx.line(sk, (0, 5, 0), (10, 5, 0))
    b = nx.line(sk, (0, 9, 0), (10, 9, 0))
    edge_a = nx.model_edge((0, 5, 3), (10, 5, 3))
    edge_b = nx.model_edge((0, 9, 3), (10, 9, 3))
    nx.part.Features.append(_projection_feature([b, a], [edge_a, edge_b]))
    ok = run(nx, "nx_get_sketch", sketch=sk.ref, include=["geometry"])
    rows = {r["curve"]["id"]: r for r in ok.data["curves"]}
    assert "projected" not in rows[nx.cref(drawn)]
    assert rows[nx.cref(a)]["projected"] is True and rows[nx.cref(a)]["source"] == edge_a.ref
    assert rows[nx.cref(b)]["projected"] is True and rows[nx.cref(b)]["source"] == edge_b.ref


# ---- corners ---------------------------------------------------------------------------


def _corner(nx, sk, a, b, **kw):
    return run(
        nx, "nx_create_sketch_corner", sketch=sk.ref, first=nx.cref(a), second=nx.cref(b), **kw
    )


def test_a_corner_filleted_already_is_refused_with_the_existing_arc():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bottom = nx.line(sk, (50, 0, 0), (90, 0, 0))
    right = nx.line(sk, (90, 0, 0), (90, 30, 0))
    ok = _corner(nx, sk, bottom, right, kind="fillet", radius=5)
    assert ok.ok, ok
    again = _corner(nx, sk, bottom, right, kind="fillet", radius=5)
    assert again.error["code"] == "NX_ALREADY_EXISTS" and again.error["field"] == "/second"
    assert again.error["details"]["existing"] == ok.data["corner"]["curve"]["id"]
    assert again.mutation_outcome == "not_started"
    assert len(sk.geometry) == 3


def test_curves_that_do_not_meet_are_refused_with_the_gap():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 40, 0), (30, 40, 0))
    arc = nx.arc(sk, (200, 100, 0), 15, 0, 90)
    bad = _corner(nx, sk, line, arc, kind="fillet", radius=5)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/second", bad
    assert bad.error["details"]["gap"] > 100 and "do not meet" in bad.error["message"]
    assert bad.mutation_outcome == "not_started"


def test_a_fillet_radius_formula_stays_driven_and_a_chamfer_formula_is_refused():
    nx = sketch_nx()
    nx.expression("p0", "100", 100.0)
    sk = nx.sketch(active=True)
    bottom = nx.line(sk, (50, 0, 0), (90, 0, 0))
    right = nx.line(sk, (90, 0, 0), (90, 30, 0))
    ok = _corner(nx, sk, bottom, right, kind="fillet", radius="p0/20")
    assert ok.ok, ok
    assert ok.data["corner"]["radius"] == pytest.approx(5)
    assert ok.data["expression"]["formula"] == "p0/20"
    top = nx.line(sk, (90, 30, 0), (60, 30, 0))
    bad = _corner(nx, sk, right, top, kind="chamfer", distance="p0/50")
    assert bad.error["field"] == "/distance" and "formula" in bad.error["message"], bad
    assert bad.mutation_outcome == "not_started"


# ---- copies ----------------------------------------------------------------------------


def test_an_inside_offset_deeper_than_a_convex_arc_is_refused_with_its_limit():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (0, 0, 0), (35, 0, 0))
    r = nx.arc(sk, (35, 5, 0), 5, 270, 360)
    b = nx.line(sk, (40, 5, 0), (40, 30, 0))
    c = nx.line(sk, (40, 30, 0), (0, 30, 0))
    d = nx.line(sk, (0, 30, 0), (0, 0, 0))
    refs = [nx.cref(x) for x in (a, r, b, c, d)]
    bad = run(
        nx, "nx_create_sketch_copy", sketch=sk.ref, curves=refs, mode="offset",
        distance=5, side="inside",
    )  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/distance", bad
    assert bad.error["details"]["max_distance"] == pytest.approx(5)
    assert bad.mutation_outcome == "not_started"


def test_inradius_bounds_a_rectangle():
    square = [
        ([0, 0], [40, 0]),
        ([40, 0], [40, 30]),
        ([40, 30], [0, 30]),
        ([0, 30], [0, 0]),
    ]
    estimate, slack = operations._inradius(square)
    assert estimate <= 15 + 1e-9 and estimate + slack >= 15


def test_an_offset_returns_its_driving_expression():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    base = nx.line(sk, (0, 0, 0), (10, 0, 0))

    def offset(b):
        d = float(b.Distance.RightHandSide)
        made = nx.line(sk, (0, d, 0), (10, d, 0))
        sk._dimension(CT.PerpendicularDim, [(base, "NotSet"), (made, "StartPoint")], "driving")

    nx.on_commit("SketchOffsetBuilder", offset)
    ok = run(
        nx, "nx_create_sketch_copy", sketch=sk.ref, curves=[nx.cref(base)], mode="offset",
        distance=3,
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["expression"]["value"] == pytest.approx(3)


def test_mirror_copies_on_existing_curves_are_reported():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    axis = nx.line(sk, (0, -10, 0), (0, 10, 0))
    src = nx.line(sk, (-5, 0, 0), (5, 0, 0))  # symmetric about the axis

    nx.on_commit("SketchMirrorPatternBuilder", lambda b: nx.line(sk, (5, 0, 0), (-5, 0, 0)))
    ok = run(
        nx, "nx_create_sketch_copy", sketch=sk.ref, curves=[nx.cref(src)], mode="mirror",
        mirror_line=nx.cref(axis),
    )  # fmt: skip
    assert ok.ok, ok
    assert any("lie on existing curves" in w["message"] for w in ok.warnings), ok.warnings


def test_faces_are_grouped_into_connected_sets():
    class Face:
        def __init__(self, tag):
            self.Tag, self.edges = tag, []

        def GetEdges(self):  # noqa: N802
            return self.edges

    class Edge:
        def __init__(self, *faces):
            self.faces = faces

        def GetFaces(self):  # noqa: N802
            return self.faces

    a, b, c = Face(1), Face(2), Face(3)
    shared = Edge(a, b)
    a.edges, b.edges, c.edges = [shared], [shared], [Edge(c)]
    groups = operations.face_sets(None, [a, c, b])
    assert [[f.Tag for f in g] for g in groups] == [[1, 2], [3]]


# ---- constraints and dimensions --------------------------------------------------------


def _constrain(nx, sk, *items):
    return run(nx, "nx_create_sketch_constraints", sketch=sk.ref, constraints=list(items))


def _dimension(nx, sk, *items):
    return run(nx, "nx_create_sketch_dimensions", sketch=sk.ref, dimensions=list(items))


def test_collinear_moves_the_second_line_onto_the_first():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (60, 0, 0), (80, 0, 0))
    b = nx.line(sk, (90, 3, 0), (110, 3, 0))
    ok = _constrain(nx, sk, {"type": "colinear", "curves": [nx.cref(a), nx.cref(b)]})
    assert ok.ok, ok
    assert ok.data["constraints"][0]["type"] == "collinear"
    assert abs(b.StartPoint.Y) < 1e-9 and abs(b.EndPoint.Y) < 1e-9


def test_an_unknown_curve_ref_names_its_field():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    bad = _constrain(nx, sk, {"type": "parallel", "curves": ["c:999999", nx.cref(a)]})
    assert bad.error["field"] == "/constraints/0/curves/0", bad
    assert bad.mutation_outcome == "not_started"


def test_edit_reports_the_neighbours_the_solver_moved():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (-5, -40, 0), (-5, -25, 0))
    other = nx.line(sk, (-5, -25, 0), (20, -25, 0))
    sk.constraints.append(Constraint(CT.Vertical, [(line, "NotSet")], sk))

    def solve():  # the neighbour follows the edited line's end (a coincident joint)
        other.StartPoint = P([-10, -25, 0])

    sk.solve = solve
    ok = run(
        nx, "nx_edit_sketch_curves", sketch=sk.ref,
        edits=[{"type": "line", "curve": nx.cref(line), "start": [-10, -50], "end": [-10, -25]}],
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["moved"] == [nx.cref(other)]
    modified = {r["id"] for r in ok.changes.get("modified", [])}
    assert {nx.cref(line), nx.cref(other)} <= modified


def test_a_driving_dimension_on_a_fully_constrained_sketch_fails_early():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (60, 0, 0))
    nx.status = (3, 0)  # NX: WellConstrained
    bad = _dimension(nx, sk, {"kind": "length", "first": nx.cref(line), "value": 70})
    assert bad.error["code"] == "NX_SKETCH_OVERCONSTRAINED", bad
    assert bad.error["field"] == "/dimensions/0" and "reference" in bad.error["hint"]
    assert sk.constraints == []


def test_an_over_constrained_dimension_names_the_relieving_constraint():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (60, 0, 0))
    held = Constraint(CT.Fixed, [(line, "NotSet")], sk)
    sk.constraints.append(held)
    sk.constraints.append(Constraint(CT.Fixed, [(line, "NotSet")], sk))  # fully fixed
    sk.over_needs_fixed = True
    bad = _dimension(nx, sk, {"kind": "length", "first": nx.cref(line), "value": 70})
    assert bad.error["category"] == "solver_failed", bad
    assert bad.error["field"] == "/dimensions/0"
    relieving = {nx.ref_of(c, "sketch_constraint") for c in sk.constraints}
    assert set(bad.error["details"]["relieving"]) <= relieving
    assert bad.error["details"]["relieving"], bad.error
    assert "nx_check_sketch" not in bad.error["hint"] and "reference" in bad.error["hint"]


def test_dimension_name_and_formula_keep_the_expression_driven():
    nx = sketch_nx()
    nx.expression("width", "50", 50.0)
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (60, 0, 0))
    ok = _dimension(
        nx, sk, {"kind": "length", "first": nx.cref(line), "value": "width/2", "name": "half"}
    )
    assert ok.ok, ok
    row = ok.data["dimensions"][0]
    assert row["expression"]["name"] == "half" and row["expression"]["formula"] == "width/2"
    assert row["measured"] == pytest.approx(25)
    taken = _dimension(nx, sk, {"kind": "length", "first": nx.cref(line), "name": "WIDTH"})
    assert taken.error["code"] == "NX_NAME_CONFLICT", taken
    assert taken.error["field"] == "/dimensions/0/name" and taken.mutation_outcome == "not_started"


def test_angle_rows_give_the_supplement():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    b = nx.line(sk, (0, 0, 0), (-10, 5, 0))
    ok = _dimension(
        nx, sk, {"kind": "angle", "first": nx.cref(a), "second": nx.cref(b), "reference": True}
    )
    assert ok.ok, ok
    row = ok.data["dimensions"][0]
    assert row["measured"] + row["supplement"] == pytest.approx(180)
