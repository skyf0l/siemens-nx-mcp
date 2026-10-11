"""nx_analyze_faces, nx_analyze_curve, nx_check_continuity on the fake kit."""

import math

import pytest

from tests.kit import run
from tests.unit.inspection.fakes import curve, inspection_nx


def _face(body, normal):
    return next(f for f in body.faces if list(f.normal) == list(normal))


def test_flat_radii_read_as_straight_and_draft_against_pull():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top, side = _face(body, (0, 0, 1)), _face(body, (1, 0, 0))
    ok = run(
        nx, "nx_analyze_faces", faces=[top.ref, side.ref], samples_per_axis=2,
        pull_direction=[0, 0, 2], minimum_draft=3,
    )  # fmt: skip
    assert ok.ok, ok
    groups = ok.data["faces"]
    assert [g["face"]["id"] for g in groups] == [top.ref, side.ref]
    assert [len(g["samples"]) for g in groups] == [4, 4] and ok.data["sample_count"] == 8
    assert "face" not in groups[0]["samples"][0] and ok.data["pull_direction"] == [0, 0, 1]
    samples = [row for g in groups for row in g["samples"]]
    first = samples[0]
    assert first["radii"] == [None, None] and first["curvatures"] == [0.0, 0.0]
    assert first["draft"] == pytest.approx(90) and first["verdict"] == "ok"
    assert samples[-1]["draft"] == pytest.approx(0) and samples[-1]["verdict"] == "below_minimum"
    assert ok.data["below_minimum"] == 4


def test_cylinder_curvature_and_trim_skips():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = _face(body, (0, 0, 1))
    top.radii = [2.0, 3.937e8]
    nx.uf.Modeling.AskPointContainment = lambda p, tag: 2 if p[0] < 5 else 1
    ok = run(nx, "nx_analyze_faces", faces=[top.ref], samples_per_axis=2)
    rows = ok.data["faces"][0]["samples"]
    assert ok.data["skipped_outside_trim"] == 2 and len(rows) == 2
    assert ok.data["sample_count"] == 2 and rows[0]["curvatures"] == [0.5, 0.0]
    assert "draft" not in rows[0]


def test_face_analysis_argument_rules():
    nx = inspection_nx()
    body = nx.block(1, 1, 1)
    face = body.faces[0].ref
    lone = run(nx, "nx_analyze_faces", faces=[face], minimum_draft=2)
    assert lone.error["field"] == "/minimum_draft"
    steep = run(nx, "nx_analyze_faces", faces=[face], pull_direction=[0, 0, 1], minimum_draft=90)
    assert steep.error["field"] == "/minimum_draft"
    zero = run(nx, "nx_analyze_faces", faces=[face], pull_direction=[0, 0, 0])
    assert zero.error["field"] == "/pull_direction"
    wrong = run(nx, "nx_analyze_faces", faces=[body.ref])
    assert wrong.error["code"] == "NX_WRONG_KIND"


def test_curve_samples_circle_and_line():
    nx = inspection_nx()
    circle = curve(nx, radius=5)
    ok = run(nx, "nx_analyze_curve", curve=circle.ref, samples=3)
    assert ok.ok, ok
    rows = ok.data["samples"]
    assert [r["t"] for r in rows] == [0, 0.5, 1]
    assert rows[0]["curvature"] == pytest.approx(0.2) and rows[0]["radius"] == pytest.approx(5)
    assert rows[1]["point"][:2] == pytest.approx([-5, 0])
    line = curve(nx)
    straight = run(nx, "nx_analyze_curve", curve=line.ref, samples=2)
    assert straight.data["samples"][1]["curvature"] == 0
    assert straight.data["samples"][1]["radius"] is None
    assert straight.data["samples"][0]["tangent"] == [1, 0, 0]
    few = run(nx, "nx_analyze_curve", curve=line.ref, samples=1)
    assert few.error["category"] == "invalid_argument"


def test_continuity_of_two_boundary_edges(monkeypatch):
    from nx_mcp.kit import surface

    nx = inspection_nx()
    a = nx.block(10, 10, 1)
    b = nx.block(10, 10, 1, origin=(10, 0, 0))
    e1, e2 = a.edges[0], b.edges[0]
    e1.GetFaces = lambda: [a.faces[0]]
    e2.GetFaces = lambda: [b.faces[0]]
    reports = iter(
        [
            {"gap": 0.0, "angle": 0.01, "curvature": 0.02, "samples": 4, "unresolved": 0},
            {"gap": 0.0005, "angle": 0.02, "curvature": 0.03, "samples": 4, "unresolved": 0},
        ]
    )
    monkeypatch.setattr(surface, "edge_continuity", lambda *a, **k: next(reports))
    ok = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref, samples=4)
    assert ok.ok, ok
    data = ok.data
    assert (data["g0"], data["g1"], data["g2"]) == (True, True, False)
    assert data["gap"] == 0.0005 and data["curvature"] == 0.03 and data["samples"] == 8
    assert data["mode"] == "surface" and "junction" not in data
    e2.GetFaces = lambda: [b.faces[0], b.faces[1]]
    solid = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref, mode="surface")
    assert solid.error["code"] == "NX_WRONG_GEOMETRY" and solid.error["field"] == "/second"
    assert e2.ref in solid.error["message"] and "2 faces" in solid.error["message"]
    assert "nx_create_extract" in solid.error["hint"] and "candidates" not in solid.error["hint"]
    same = run(nx, "nx_check_continuity", first=e1.ref, second=e1.ref)
    assert same.error["field"] == "/second"


def test_continuity_unresolved_samples_leave_g1_g2_open(monkeypatch):
    from nx_mcp.kit import surface

    nx = inspection_nx()
    a = nx.block(1, 1, 1)
    b = nx.block(1, 1, 1, origin=(5, 0, 0))
    e1, e2 = a.edges[0], b.edges[0]
    e1.GetFaces = lambda: [a.faces[0]]
    e2.GetFaces = lambda: [b.faces[0]]
    report = {"gap": 4.0, "angle": None, "curvature": None, "samples": 2, "unresolved": 2}
    monkeypatch.setattr(surface, "edge_continuity", lambda *a, **k: report)
    ok = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref,
             tolerances={"position": 0.1})  # fmt: skip
    assert ok.data["g0"] is False and ok.data["g1"] is None and ok.data["g2"] is None
    assert ok.data["tolerances"]["position"] == 0.1 and ok.data["unresolved"] == 4


def test_continuity_tolerances_take_units_and_are_checked(monkeypatch):
    from nx_mcp.kit import surface

    nx = inspection_nx()
    a = nx.block(1, 1, 1)
    b = nx.block(1, 1, 1, origin=(5, 0, 0))
    e1, e2 = a.edges[0], b.edges[0]
    e1.GetFaces = lambda: [a.faces[0]]
    e2.GetFaces = lambda: [b.faces[0]]
    report = {"gap": 0.005, "angle": 0.4, "curvature": 0.0, "samples": 2, "unresolved": 0}
    monkeypatch.setattr(surface, "edge_continuity", lambda *a, **k: report)
    ok = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref,
             tolerances={"position": "0.01mm", "angle": "0.5 deg"})  # fmt: skip
    assert ok.ok, ok
    assert ok.data["tolerances"] == {"position": 0.01, "angle": 0.5, "curvature": 0.01}
    assert ok.data["g0"] is True and ok.data["g1"] is True
    inch = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref,
               tolerances={"position": "0.001 in"})  # fmt: skip
    assert inch.data["tolerances"]["position"] == pytest.approx(0.0254)
    zero = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref,
               tolerances={"position": 0})  # fmt: skip
    assert zero.error["field"] == "/tolerances/position"
    steep = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref,
                tolerances={"angle": "90 deg"})  # fmt: skip
    assert steep.error["field"] == "/tolerances/angle"


@pytest.mark.parametrize(
    ("value", "expected", "draft"),
    [
        ("z", [0, 0, 1], 90),
        ("+z", [0, 0, 1], 90),
        ("Z", [0, 0, 1], 90),
        ("top", [0, 0, 1], 90),
        ("-z", [0, 0, -1], -90),
        ("-x", [-1, 0, 0], 0),
        ([0, 2, 0], [0, 1, 0], 0),
    ],
)
def test_pull_direction_takes_principal_names(value, expected, draft):
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = _face(body, (0, 0, 1))
    ok = run(nx, "nx_analyze_faces", faces=[top.ref], samples_per_axis=1,
             pull_direction=value, minimum_draft="3deg")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["pull_direction"] == expected
    assert ok.data["faces"][0]["samples"][0]["draft"] == pytest.approx(draft)


def test_pull_direction_rejects_unknown_names():
    nx = inspection_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_analyze_faces", faces=[body.faces[0].ref], pull_direction="sideways")
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == "/pull_direction"
    assert "+z" in bad.error["message"]


def test_edge_curvature_from_tangent_differences():
    from tests.unit.inspection.fakes import arc_spec

    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    arc, line = body.edges[0], body.edges[1]
    nx.curve_specs[int(arc.Tag)] = arc_spec((0, 0, 0), 4.0, 0.0, math.pi)
    nx.curve_specs[int(line.Tag)] = ("line", [0, 0, 0], [10, 0, 0])
    ok = run(nx, "nx_analyze_curve", curve=arc.ref, samples=3)
    assert ok.ok, ok
    assert ok.data["curve"]["kind"] == "edge" and "spline" not in ok.data
    rows = ok.data["samples"]
    assert [r["t"] for r in rows] == [0, 0.5, 1]
    for row in rows:  # ends use a one-sided difference, still exact on an arc
        assert row["curvature"] == pytest.approx(0.25) and row["radius"] == pytest.approx(4)
    assert rows[1]["point"] == pytest.approx([0, 4, 0], abs=1e-12)
    assert rows[1]["tangent"] == pytest.approx([-1, 0, 0], abs=1e-12)
    default = run(nx, "nx_analyze_curve", curve=arc.ref)
    assert len(default.data["samples"]) == 11
    straight = run(nx, "nx_analyze_curve", curve=line.ref, samples=2)
    assert straight.ok, straight
    assert straight.data["samples"][0]["curvature"] == 0
    assert straight.data["samples"][0]["radius"] is None


def _edges(nx, *specs):
    body = nx.block(10, 10, 10)
    for edge, spec in zip(body.edges, specs, strict=False):
        nx.curve_specs[int(edge.Tag)] = spec
    return body.edges[: len(specs)]


def test_curve_continuity_at_the_nearest_ends():
    from tests.unit.inspection.fakes import arc_spec

    nx = inspection_nx()
    # a line (-x) into a quarter arc (radius 4) tangent at (0, 4, 0): G1, not G2
    line, arc, kink, far = _edges(
        nx,
        ("line", [10.0, 4.0, 0.0], [0.0, 4.0, 0.0]),
        arc_spec((0, 0, 0), 4.0, math.pi / 2, math.pi),
        ("line", [0.0, 4.0, 0.0], [0.0, 14.0, 0.0]),
        ("line", [50.0, 0.0, 0.0], [60.0, 0.0, 0.0]),
    )
    ok = run(nx, "nx_check_continuity", first=line.ref, second=arc.ref)
    assert ok.ok, ok
    data = ok.data
    assert data["mode"] == "curve" and data["samples"] == 2 and data["unresolved"] == 0
    assert data["junction"]["first_end"] == "end" and data["junction"]["second_end"] == "start"
    assert data["junction"]["point"] == pytest.approx([0, 4, 0], abs=1e-12)
    assert data["gap"] == pytest.approx(0, abs=1e-12) and data["angle"] == pytest.approx(
        0, abs=1e-6
    )
    assert data["curvature"] == pytest.approx(0.25, rel=1e-3)
    assert (data["g0"], data["g1"], data["g2"]) == (True, True, False)
    # the line turns 90 degrees at the shared start of kink (both start there)
    corner = run(nx, "nx_check_continuity", first=kink.ref, second=line.ref)
    assert corner.data["junction"]["first_end"] == "start"
    assert corner.data["junction"]["second_end"] == "end"
    assert corner.data["angle"] == pytest.approx(90) and corner.data["g1"] is False
    # straight on: the same direction leaving the junction both ways is 180 degrees
    back = run(nx, "nx_check_continuity", first=line.ref, second=line.ref)
    assert back.error["field"] == "/second"
    apart = run(nx, "nx_check_continuity", first=line.ref, second=far.ref)
    assert apart.data["g0"] is False and apart.data["gap"] == pytest.approx(math.sqrt(1616))
    assert apart.data["junction"]["first_end"] == "start"


def test_curve_continuity_of_curves_and_mode_rules(monkeypatch):
    from nx_mcp.kit import surface

    nx = inspection_nx()
    c1, c2 = curve(nx), curve(nx)
    nx.curve_specs[int(c1.Tag)] = ("line", [0.0, 0.0, 0.0], [10.0, 0.0, 0.0])
    nx.curve_specs[int(c2.Tag)] = ("line", [10.0, 0.0, 0.0], [20.0, 0.0, 0.0])
    ok = run(nx, "nx_check_continuity", first=c1.ref, second=c2.ref)
    assert ok.ok, ok
    assert ok.data["mode"] == "curve" and (ok.data["g0"], ok.data["g1"], ok.data["g2"]) == (
        True,
        True,
        True,
    )
    refused = run(nx, "nx_check_continuity", first=c1.ref, second=c2.ref, mode="surface")
    assert refused.error["code"] == "NX_WRONG_GEOMETRY" and refused.error["field"] == "/first"
    # two boundary edges of different sheet faces: auto is surface; mode curve overrides
    a, b = nx.block(10, 10, 1), nx.block(10, 10, 1, origin=(10, 0, 0))
    e1, e2 = a.edges[0], b.edges[0]
    e1.GetFaces = lambda: [a.faces[0]]
    e2.GetFaces = lambda: [b.faces[0]]
    nx.curve_specs[int(e1.Tag)] = ("line", [10.0, 0.0, 0.0], [10.0, 10.0, 0.0])
    nx.curve_specs[int(e2.Tag)] = ("line", [10.0, 10.0, 0.0], [10.0, 20.0, 0.0])
    report = {"gap": 0.0, "angle": 0.0, "curvature": 0.0, "samples": 2, "unresolved": 0}
    monkeypatch.setattr(surface, "edge_continuity", lambda *a, **k: report)
    auto = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref)
    assert auto.data["mode"] == "surface"
    forced = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref, mode="curve")
    assert forced.data["mode"] == "curve" and forced.data["junction"]["first_end"] == "end"
    # two boundary edges of the same face meet at a corner: curve mode
    e2.GetFaces = lambda: [a.faces[0]]
    same_face = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref)
    assert same_face.data["mode"] == "curve"
