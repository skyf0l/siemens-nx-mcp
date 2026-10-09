"""nx_analyze_faces, nx_analyze_curve, nx_check_continuity on the fake kit."""

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
    samples = ok.data["samples"]
    assert len(samples) == 8 and ok.data["pull_direction"] == [0, 0, 1]
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
    assert ok.data["skipped_outside_trim"] == 2 and len(ok.data["samples"]) == 2
    assert ok.data["samples"][0]["curvatures"] == [0.5, 0.0]
    assert "draft" not in ok.data["samples"][0]


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
    e2.GetFaces = lambda: [b.faces[0], b.faces[1]]
    solid = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref)
    assert solid.error["code"] == "NX_AMBIGUOUS_REFERENCE" and solid.error["field"] == "/second"
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
