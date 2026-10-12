"""nx_create_n_sided_surface and nx_create_ruled on the shared fake."""

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import curve, edge, face, flat, freeform_nx, made, sheet

SQUARE = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]


def square_curves(nx, z=0.0, reverse_one=False):
    pts = [[x, y, z] for x, y, _ in SQUARE]
    out = []
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        out.append(curve(nx, b, a) if reverse_one and i == 2 else curve(nx, a, b))
    return out


def frame_edges(nx):
    """Four boundary edges of a flat frame around the square hole, one face each."""
    faces = [face(nx, flat()) for _ in range(4)]
    edges = [edge(nx, SQUARE[i], SQUARE[(i + 1) % 4], [faces[i]]) for i in range(4)]
    return edges, faces


def patch_commit(nx, box=(0, 0, 0, 10, 10, 0), area=100.0):
    def commit(_b):
        f = face(nx, flat(), area=area)
        ends = [edge(nx, SQUARE[i], SQUARE[(i + 1) % 4], [f]) for i in range(4)]
        return made(nx, "NSIDED_SURFACE", [sheet(nx, box, [f], ends)])

    nx.on_commit("NSidedSurfaceBuilder", commit)


# ---------------------------------------------------------------------------
# N-sided


@pytest.mark.parametrize(("kind", "native"), [("trimmed", "TrimmedPatch"),
                                              ("triangular", "TriangularPatch")])  # fmt: skip
def test_n_sided_fills_a_closed_curve_loop(kind, native):
    nx = freeform_nx()
    loop = square_curves(nx, reverse_one=True)  # chained whatever the curve directions
    patch_commit(nx)
    ok = run(nx, "nx_create_n_sided_surface", boundary=[c.ref for c in loop], type=kind)
    assert ok.ok, ok
    b = nx.last("NSidedSurfaceBuilder")
    types = nx.nx.Features.NSidedSurfaceBuilder.SurfaceType
    assert b.Type is getattr(types, native)
    assert b.PositionTolerance == pytest.approx(0.001)
    assert nx.license_queries == ["nx_freeform_1"]
    assert ok.data["type"] == kind and ok.data["continuity"] == "g0"
    assert ok.data["gap"] == pytest.approx(0) and ok.data["area"] == pytest.approx(100)
    assert [f["boundary"] for f in ok.data["boundary"]] == [c.ref for c in loop]
    assert all(f["edge"] and f["angle"] is None for f in ok.data["boundary"])
    assert nx.live_builders == 0


def test_n_sided_g1_uses_the_boundary_edges_faces():
    nx = freeform_nx()
    edges, faces = frame_edges(nx)
    patch_commit(nx)
    ok = run(nx, "nx_create_n_sided_surface", boundary=[e.ref for e in edges], continuity="g1",
             type="triangular")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("NSidedSurfaceBuilder")
    g1 = nx.nx.GeometricUtilities.Continuity.ContinuityTypes.G1
    assert b.Continuity.ContinuityType is g1
    assert b.ConstraintFaces.ReplaceRules.calls[0][0][0] == [("FaceDumb", faces)]
    assert [f["angle"] for f in ok.data["boundary"]] == pytest.approx([0, 0, 0, 0])


def test_n_sided_g1_tilted_patch_fails_read_back():
    nx = freeform_nx()
    edges, _faces = frame_edges(nx)

    def commit(_b):
        f = face(nx, flat(dv=(0, 1, 1)), area=100.0)  # tilted 45 deg
        ends = [edge(nx, SQUARE[i], SQUARE[(i + 1) % 4], [f]) for i in range(4)]
        return made(nx, "NSIDED_SURFACE", [sheet(nx, (0, 0, 0, 10, 10, 0), [f], ends)])

    nx.on_commit("NSidedSurfaceBuilder", commit)
    bad = run(nx, "nx_create_n_sided_surface", boundary=[e.ref for e in edges], continuity="g1",
              type="triangular")  # fmt: skip
    assert bad.error["details"]["check"] == "g1", bad
    assert bad.mutation_outcome == "rolled_back"


def test_n_sided_gap_to_the_boundary_fails_read_back():
    nx = freeform_nx()
    loop = square_curves(nx)
    patch_commit(nx, box=(0, 0, 0, 10, 5, 0))  # misses the y=10 side
    bad = run(nx, "nx_create_n_sided_surface", boundary=[c.ref for c in loop])
    assert bad.error["details"]["check"] == "boundary"
    assert bad.mutation_outcome == "rolled_back"


def test_n_sided_rejections_before_nx():
    nx = freeform_nx()
    loop = square_curves(nx)
    refs = [c.ref for c in loop]
    open_loop = run(nx, "nx_create_n_sided_surface", boundary=refs[:3])
    assert (
        open_loop.error["code"] == "NX_WRONG_GEOMETRY" and open_loop.error["field"] == "/boundary"
    )
    dup = run(nx, "nx_create_n_sided_surface", boundary=[*refs, refs[0]])
    assert dup.error["field"] == "/boundary/4"
    curves_g1 = run(nx, "nx_create_n_sided_surface", boundary=refs, continuity="g1",
                    type="triangular")  # fmt: skip
    assert curves_g1.error["field"] == "/boundary/0" and "faces" in curves_g1.error["hint"]
    trimmed_g1 = run(nx, "nx_create_n_sided_surface", boundary=refs, continuity="g1")
    assert trimmed_g1.error["field"] == "/continuity" and "triangular" in trimmed_g1.error["hint"]
    faces_g0 = run(nx, "nx_create_n_sided_surface", boundary=refs,
                   faces=[face(nx, flat()).ref])  # fmt: skip
    assert faces_g0.error["field"] == "/faces"
    for bad in (open_loop, dup, curves_g1, trimmed_g1, faces_g0):
        assert bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_n_sided_with_g1_faces_for_curves():
    nx = freeform_nx()
    loop = square_curves(nx)
    around = face(nx, flat())
    around.box = [-5, -5, 0, 15, 15, 0]  # a flat frame face around the loop
    patch_commit(nx)
    ok = run(nx, "nx_create_n_sided_surface", boundary=[c.ref for c in loop], continuity="g1",
             type="triangular", faces=[around.ref])  # fmt: skip
    assert ok.ok, ok  # curves: measured against the given face nearest each
    assert [f["angle"] for f in ok.data["boundary"]] == pytest.approx([0, 0, 0, 0])
    b = nx.last("NSidedSurfaceBuilder")
    assert b.ConstraintFaces.ReplaceRules.calls[0][0][0] == [("FaceDumb", [around])]


# ---------------------------------------------------------------------------
# Ruled


def ruled_commit(nx, box=(0, 0, 0, 10, 0, 10), solid=False):
    def commit(_b):
        body = sheet(nx, box, [face(nx, flat(), area=100.0)])
        if solid:
            body.IsSolidBody, body.IsSheetBody, body.volume = True, False, 1000.0
        return made(nx, "RULED", [body])

    nx.on_commit("RuledBuilder", commit)


def help_points(section):
    h = section.added[0][2]
    return [h.X, h.Y, h.Z]


@pytest.mark.parametrize(("reverse", "start"), [(False, [0, 0, 10]), (True, [10, 0, 10])])
def test_ruled_between_two_lines(reverse, start):
    nx = freeform_nx()
    a = curve(nx, [0, 0, 0], [10, 0, 0])
    b = curve(nx, [0, 0, 10], [10, 0, 10])
    ruled_commit(nx)
    ok = run(nx, "nx_create_ruled", first=a.ref, second=[b.ref], reverse_second=reverse)
    assert ok.ok, ok
    rb = nx.last("RuledBuilder")
    assert help_points(rb.FirstSection) == [0, 0, 0] and help_points(rb.SecondSection) == start
    assert rb.FirstSection.rules == [("CurveDumb", [a])]
    kinds = nx.nx.GeometricUtilities.AlignmentMethodBuilder.Type
    assert rb.AlignmentMethod.AlignType is kinds.Parameter
    assert rb.BodyPreference.BodyType is nx.nx.GeometricUtilities.FeatureOptions.BodyStyle.Sheet
    assert ok.data["gaps"] == pytest.approx({"first": 0, "second": 0})
    assert ok.data["area"] == pytest.approx(100) and ok.data["volume"] is None
    assert ok.data["alignment"] == "parameter" and nx.live_builders == 0


def test_ruled_arc_length_turns_off_shape_preservation():
    nx = freeform_nx()
    a = curve(nx, [0, 0, 0], [10, 0, 0])
    b = curve(nx, [0, 0, 10], [10, 0, 10])
    ruled_commit(nx)
    ok = run(nx, "nx_create_ruled", first=a.ref, second=b.ref, alignment="arc_length")
    assert ok.ok, ok
    rb = nx.last("RuledBuilder")
    assert rb.IsShapePreserved is False
    assert (
        rb.AlignmentMethod.AlignType
        is nx.nx.GeometricUtilities.AlignmentMethodBuilder.Type.ArcLength
    )


def test_ruled_solid_between_closed_sections():
    nx = freeform_nx()
    low, high = square_curves(nx, 0), square_curves(nx, 10)
    ruled_commit(nx, box=(0, 0, 0, 10, 10, 10), solid=True)
    ok = run(nx, "nx_create_ruled", first=[c.ref for c in low], second=[c.ref for c in high],
             solid=True)  # fmt: skip
    assert ok.ok, ok
    rb = nx.last("RuledBuilder")
    assert rb.BodyPreference.BodyType is nx.nx.GeometricUtilities.FeatureOptions.BodyStyle.Solid
    assert help_points(rb.SecondSection) == [0, 0, 10]
    assert ok.data["volume"] == pytest.approx(1000) and ok.data["sheet"] is False


def test_ruled_missed_section_and_rejections():
    nx = freeform_nx()
    a = curve(nx, [0, 0, 0], [10, 0, 0])
    b = curve(nx, [0, 0, 10], [10, 0, 10])
    same = run(nx, "nx_create_ruled", first=a.ref, second=[b.ref, a.ref])
    assert same.error["field"] == "/second/1" and same.mutation_outcome == "not_started"
    open_solid = run(nx, "nx_create_ruled", first=a.ref, second=b.ref, solid=True)
    assert open_solid.error["field"] == "/solid"
    c = curve(nx, [50, 0, 10], [60, 0, 10])
    apart = run(nx, "nx_create_ruled", first=a.ref, second=[b.ref, c.ref])
    assert apart.error["code"] == "NX_WRONG_GEOMETRY" and apart.error["field"] == "/second"
    assert not nx.builders
    ruled_commit(nx, box=(0, 0, 0, 10, 0, 5))  # stops short of the second section
    bad = run(nx, "nx_create_ruled", first=a.ref, second=b.ref)
    assert bad.error["details"]["check"] == "second_section"
    assert bad.mutation_outcome == "rolled_back"


def test_n_sided_gap_refusal_says_which_tolerance_accepts_it():
    nx = freeform_nx()
    loop = square_curves(nx)
    patch_commit(nx, box=(0, 0, 0.05, 10, 10, 0.05))  # NX's fit 0.05 off the boundary
    bad = run(nx, "nx_create_n_sided_surface", boundary=[c.ref for c in loop])
    assert bad.error["details"]["check"] == "boundary"
    assert bad.error["details"]["actual"] == {"gap": pytest.approx(0.05)}
    assert "tolerance 0.025" in bad.error["hint"] and bad.mutation_outcome == "rolled_back"


def test_n_sided_warns_when_the_patch_bulges_far_outside_its_boundary():
    nx = freeform_nx()
    edges, _faces = frame_edges(nx)
    patch_commit(nx, box=(0, 0, -8, 10, 10, 0))  # reaches 8 below a 10 wide opening
    ok = run(nx, "nx_create_n_sided_surface", boundary=[e.ref for e in edges])
    assert ok.ok, ok
    assert [w["code"] for w in ok.warnings] == ["NX_UPDATE_WARNING"]
    assert "outside its boundary" in ok.warnings[0]["message"]
