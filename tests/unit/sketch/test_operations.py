"""nx_create_sketch_corner, nx_create_sketch_copy, nx_create_sketch_projection (fake kit)."""

import pytest

from tests.kit import NXException, run
from tests.unit.sketch.fakes import P, sketch_nx


def _corner(nx, sk, a, b, **kw):
    return run(
        nx, "nx_create_sketch_corner", sketch=sk.ref, first=nx.cref(a), second=nx.cref(b), **kw
    )


def test_fillet_trims_both_lines_and_is_tangent():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bottom = nx.line(sk, (50, 0, 0), (90, 0, 0))
    right = nx.line(sk, (90, 0, 0), (90, 30, 0))
    ok = _corner(nx, sk, bottom, right, kind="fillet", radius=5)
    assert ok.ok, ok
    assert ok.data["corner"]["radius"] == 5 and ok.data["corner"]["center"] == pytest.approx(
        [85, 5]
    )
    assert ok.data["trimmed"][0]["end"] == pytest.approx([85, 0])
    assert ok.data["trimmed"][1]["start"] == pytest.approx([90, 5])


def test_fillet_too_large_for_the_corner_is_refused_and_rolled_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bottom = nx.line(sk, (0, 0, 0), (40, 0, 0))
    right = nx.line(sk, (40, 0, 0), (40, 30, 0))
    bad = _corner(nx, sk, bottom, right, kind="fillet", radius=100)
    assert bad.error["details"]["check"] == "fillet_within_curves", bad
    assert bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"kind": "fillet"}, "/radius"),
        ({"kind": "fillet", "radius": 2, "distance": 1}, "/distance"),
        ({"kind": "chamfer", "radius": 2, "distance": 1}, "/radius"),
        ({"kind": "chamfer"}, "/distance"),
        ({"kind": "fillet", "radius": -1}, "/radius"),
    ],
)
def test_corner_argument_rules(kw, field):
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a, b = nx.line(sk, (0, 0, 0), (1, 0, 0)), nx.line(sk, (1, 0, 0), (1, 1, 0))
    bad = _corner(nx, sk, a, b, **kw)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad


def test_chamfer_swaps_distances_when_nx_assigns_them_the_other_way():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    top = nx.line(sk, (40, 30, 0), (50, 30, 0))
    right = nx.line(sk, (50, 30, 0), (50, 0, 0))
    attempts = []

    def chamfer(b):
        d1 = float(b.Distance1.RightHandSide)
        d2 = float(b.Distance2.RightHandSide)
        attempts.append((d1, d2))
        # NX's own rule: Distance1 goes along the vertical line.
        top.EndPoint = P([50 - d2, 30, 0])
        right.StartPoint = P([50, 30 - d1, 0])
        nx.line(sk, (50 - d2, 30, 0), (50, 30 - d1, 0))

    nx.on_commit("SketchChamferBuilder", chamfer)
    ok = _corner(nx, sk, top, right, kind="chamfer", distance=4, distance2=6)
    assert ok.ok, ok
    assert attempts == [(4.0, 6.0), (6.0, 4.0)]
    assert ok.data["distances"] == pytest.approx([4, 6])
    assert ok.data["corner"]["start"] == pytest.approx([46, 30])


def test_offset_side_and_distance_are_read_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    base = nx.line(sk, (0, 0, 0), (10, 0, 0))

    def offset(b):
        d = float(b.Distance.RightHandSide)
        flipped = bool(b.ReverseOffsetDirectionOfChain.calls)
        y = -d if flipped else d
        nx.line(sk, (0, y, 0), (10, y, 0))

    nx.on_commit("SketchOffsetBuilder", offset)
    ok = run(
        nx, "nx_create_sketch_copy", sketch=sk.ref, curves=[nx.cref(base)], mode="offset",
        distance=3,
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["side"] == "left" and ok.data["measured_distance"] == pytest.approx(3, abs=1e-6)
    b = nx.last("SketchOffsetBuilder")
    assert b.Tolerance == pytest.approx(0.001)  # set before UpdateLoopsAndCopies


def test_mirror_checks_the_reflection_and_the_mirror_line_owner():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    axis = nx.line(sk, (60, -10, 0), (60, 10, 0))
    src = nx.line(sk, (0, 0, 0), (30, 5, 0))

    def mirror(b):
        nx.line(sk, (120, 0, 0), (90, 5, 0))

    nx.on_commit("SketchMirrorPatternBuilder", mirror)
    args = {"sketch": sk.ref, "curves": [nx.cref(src)], "mode": "mirror"}
    ok = run(nx, "nx_create_sketch_copy", mirror_line=nx.cref(axis), **args)
    assert ok.ok, ok
    assert ok.data["mirror_line"] == {"start": [60, -10], "end": [60, 10]}
    other = nx.sketch()
    foreign = nx.line(other, (0, 0, 0), (0, 1, 0))
    bad = run(nx, "nx_create_sketch_copy", mirror_line=nx.cref(foreign), **args)
    assert bad.error["category"] == "wrong_owner"
    nx.on_commit("SketchMirrorPatternBuilder", lambda b: nx.line(sk, (0, 0, 0), (1, 1, 0)))
    wrong = run(nx, "nx_create_sketch_copy", mirror_line=nx.cref(axis), **args)
    assert wrong.error["details"]["check"] == "mirror_reflection"
    sk.ref = nx.ref_of(sk, "sketch")  # the rollback invalidated the part's refs
    args = {"sketch": sk.ref, "curves": [nx.cref(src)], "mode": "mirror"}
    same = run(nx, "nx_create_sketch_copy", mirror_line=nx.cref(src), **args)
    assert same.error["field"] == "/mirror_line" and same.mutation_outcome == "not_started"


def test_projection_maps_the_native_refusal_and_returns_the_feature():
    nx = sketch_nx()
    sk = nx.sketch(origin=(0, 0, 20), active=True)
    edge = nx.model_edge((140, -10, 0), (160, -10, 0))

    def project(b):
        object.__setattr__(b, "GetOperation", lambda: None)
        nx.line(sk, (140, -10, 20), (160, -10, 20))

    nx.on_commit("ProjectBuilder", project)
    ok = run(nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[edge.ref], mode="project")
    assert ok.ok, ok
    assert ok.data["curves"][0]["start"] == pytest.approx([140, -10])
    nx.refuse("ProjectBuilder", code=674275, message="Invalid data for Sketch Project")
    bad = run(nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[edge.ref], mode="project")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["nx_code"] == 674275
    assert bad.mutation_outcome == "rolled_back"
    face = nx.block(1, 1, 1).faces[0]
    wrong = run(
        nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[face.ref], mode="project"
    )
    assert wrong.error["category"] == "wrong_kind"


def test_intersection_curves_must_lie_in_the_sketch_plane():
    nx = sketch_nx()
    sk = nx.sketch(origin=(0, 0, 5), active=True)
    face = nx.block(10, 10, 10).faces[2]

    def intersect(b):
        nx.line(sk, (10, 0, 5), (10, 10, 5))

    nx.on_commit("IntersectionCurveBuilder", intersect)
    ok = run(nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[face.ref], mode="intersect")
    assert ok.ok, ok
    nx.on_commit("IntersectionCurveBuilder", lambda b: nx.line(sk, (10, 0, 6), (10, 10, 6)))
    off = run(
        nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[face.ref], mode="intersect"
    )
    assert off.error["details"]["check"] == "curves_in_plane"

    def miss(*_a):
        raise NXException(3680011, "No intersection exists between selected faces and sketch plane")

    make = nx.builder

    def builder(kind, *a):
        b = make(kind, *a)
        if kind == "IntersectionCurveBuilder":
            object.__setattr__(b, "CollectorUpdated", miss)
        return b

    nx.builder = builder
    sk.ref, face.ref = nx.ref_of(sk, "sketch"), nx.ref_of(face, "face")  # after the rollback
    bad = run(
        nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[face.ref], mode="intersect"
    )
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["nx_code"] == 3680011
