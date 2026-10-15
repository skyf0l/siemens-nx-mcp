"""Loft alignment, guides, tangency and combine; non-uniform scale; threads on several faces;
blend and chamfer edges in nx_get_feature; expressions that keep driving shell, draft, tube,
datum plane, rib and transform. Shared fake, real kernel."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling import lofts
from nx_mcp.kit import geom
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body
from tests.unit.modeling.test_feature_shorthands import arc, evaluable, extruded, seg, square
from tests.unit.modeling.test_holes_threads import end_faces, table, thread_nx  # noqa: F401

MM = NS(Name="MilliMeter", Abbreviation="mm")
DEG = NS(Name="Degrees", Abbreviation="deg")


def expression(nx, name, value, unit=MM):
    e = nx.expression(name, repr(value), value)
    e.Units = unit
    return e


# ---- loft: section starts ------------------------------------------------------------


def circle(r=10.0, z=0.0, start=0.0):
    return [arc((0, 0), r, start, start + 2 * math.pi, z=z)]


def loops_of(*paths):
    out = []
    for path in paths:
        pieces = [(i, [f(k / 16)[0] for k in range(17)]) for i, f in enumerate(path)]
        out.append(lofts.chain(pieces, 1e-6))
    return out


def test_plan_starts_a_circle_at_the_square_corner_angle():
    loops = loops_of(circle(), square(10, z=30))
    first, second = lofts.plan(loops, [0, 0, 1])
    assert second["start"] == [-10.0, -10.0, 30.0] and second["shift"] == 0.0  # the anchor
    assert first["start"] == pytest.approx([10.0, 0.0, 0.0])  # NX keeps a circle's start
    assert first["shift"] == pytest.approx(0.625, abs=1e-3)  # 225 deg further on
    assert first["direction"] == pytest.approx([0.0, 1.0, 0.0], abs=0.2)


def test_plan_runs_every_loop_counter_clockwise_and_takes_start_points():
    loops = loops_of(square(10), square(10, z=30, reverse=True))
    a, b = lofts.plan(loops, [0, 0, 1])
    assert a["start"] == [-10.0, -10.0, 0.0] and a["direction"] == [1.0, 0.0, 0.0]
    # The reversed square runs back along its chain from the matching corner.
    assert b["start"] == [-10.0, -10.0, 30.0] and b["direction"] == [1.0, 0.0, 0.0]
    a, b = lofts.plan(loops, [0, 0, 1], targets=[[9, 9, 0], None])
    assert a["start"] == [10.0, 10.0, 0.0] and b["start"] == [10.0, 10.0, 30.0]


def test_open_or_broken_sections_have_no_loop():
    assert lofts.chain([(0, [[0, 0, 0], [1, 0, 0]])], 1e-6) is None
    two = [(0, [[0, 0, 0], [1, 0, 0]]), (1, [[5, 5, 0], [6, 5, 0]])]
    assert lofts.chain(two, 1e-6) is None


def loft_nx(volume=10750.0, top=None, bottom=None):
    nx = modeling_nx()
    a, b = nx.sketch(), nx.sketch(origin=(0, 0, 30))
    a.geometry = evaluable(nx, bottom or circle())
    b.geometry = evaluable(nx, top or square(10, z=30))
    nx.on_commit("ThroughCurvesBuilder", extruded(nx, [-10, -10, 0, 10, 10, 30], volume))
    nx.on_commit("SweptBuilder", extruded(nx, [-10, -10, 0, 10, 10, 30], volume))
    return nx, a, b


def test_loft_starts_sections_and_shifts_a_circle_s_alignment_points():
    nx, a, b = loft_nx()
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref])
    assert ok.ok and not ok.warnings, ok
    built = nx.last("ThroughCurvesBuilder")
    first, second = (call[0][0] for call in built.SectionsList.Append.calls)
    assert pytest.approx(10.0) == first.start[1].X and second.start[1].X == -10.0
    points = built.Alignment.points
    assert built.Alignment.AlignType is nx.nx.GeometricUtilities.AlignmentMethodBuilder.Type.Points
    assert [float(q.Expression.RightHandSide) for q in points[:4]] == pytest.approx(
        [62.5, 87.5, 12.5, 37.5], abs=0.2
    )
    assert all(q.IsPercentUsed for q in points[:4]) and not points[4].IsPercentUsed


def test_loft_start_points_must_match_the_sections():
    nx, a, b = loft_nx()
    short = run(nx, "nx_create_loft", sections=[a.ref, b.ref], start_points=[[10, 0, 0]])
    assert short.error["field"] == "/start_points" and short.mutation_outcome == "not_started"
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref],
             start_points=[[10, 0, 0], [10, 10, 30]])  # fmt: skip
    assert ok.ok, ok
    second = nx.last("ThroughCurvesBuilder").SectionsList.Append.calls[1][0][0]
    assert (second.start[1].X, second.start[1].Y) == (10.0, 10.0)


def test_loft_with_guides_is_a_swept_and_end_tangency_is_through_curves_only():
    nx, a, b = loft_nx(12000.0, bottom=square(10))
    guide = nx.sketch()
    guide.geometry = evaluable(nx, [seg((-10, -10, 0), (-10, -10, 30))])
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref], guides=[guide.ref])
    assert ok.ok, ok
    swept = nx.last("SweptBuilder")
    assert len(swept.SectionList.Append.calls) == 2 and len(swept.GuideList.Append.calls) == 1
    bad = run(nx, "nx_create_loft", sections=[a.ref, b.ref], guides=[guide.ref], tangent="normal")
    assert bad.error["field"] == "/tangent" and bad.mutation_outcome == "not_started"
    square_ends = run(nx, "nx_create_loft", sections=[a.ref, b.ref], tangent="normal")
    assert square_ends.ok and nx.last("ThroughCurvesBuilder").NormalToEndSections is True
    del nx.part.Bodies[:]  # no body for the sections to lie on
    nothing = run(nx, "nx_create_loft", sections=[a.ref, b.ref], tangent="faces")
    assert nothing.error["code"] == "NX_WRONG_GEOMETRY" and nothing.error["field"] == "/tangent"


def test_tangent_faces_are_the_body_faces_an_end_section_lies_on():
    nx, a, b = loft_nx()
    stock = new_body(nx, [-10, -10, -20, 10, 10, 0], 6283.0)
    side = nx.face(normal=(0, 0, 1), kind="cylinder", body=stock)
    cap = nx.face(normal=(0, 0, 1), origin=(0, 0, 0), kind="plane", body=stock)
    far = nx.face(normal=(0, 0, -1), origin=(0, 0, -20), kind="plane", body=stock)
    stock.GetFaces = lambda: [side, cap, far]
    touching = {int(side.Tag), int(cap.Tag)}
    nx.uf.Modeling.AskMinimumDist = lambda t1, t2, *rest: (
        (0.0 if int(t1) in touching else 20.0),
        [0.0] * 3,
        [0.0] * 3,
    )
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref], tangent="faces")
    assert ok.ok, ok
    built = nx.last("ThroughCurvesBuilder")
    g1 = nx.nx.GeometricUtilities.Continuity.ContinuityTypes.G1
    assert built.FirstSectionContinuity.ContinuityType is g1
    rule = built.FirstSectionContinuity.ConstraintFaces.ReplaceRules.calls[0][0][0][0]
    assert rule == ("FaceDumb", [side])  # the cap in the section's plane is skipped


def test_loft_combines_with_one_target_through_a_boolean():
    nx, a, b = loft_nx()
    lid = nx.block(20, 20, 10, origin=(-10, -10, 30))
    lofted = []

    def made(b_):
        lofted.append(extruded(nx, [-10, -10, 0, 10, 10, 30], 10750.0)(b_))
        return lofted[-1]

    def united(b_):
        nx.part.Bodies.remove(lofted[-1].bodies[0])
        lid.volume = 14750.0
        return feature(nx, "UNITE", [lid])

    nx.on_commit("ThroughCurvesBuilder", made)
    nx.on_commit("BooleanBuilder", united)
    combine = {"operation": "unite", "targets": [lid.ref]}
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref], combine=combine)
    assert ok.ok, ok
    assert ok.data["volume"] == 14750.0 and ok.data["feature"]["name"] == lofted[-1].Name
    sheet = run(nx, "nx_create_loft", sections=[a.ref, b.ref], solid=False, combine=combine)
    assert sheet.error["field"] == "/combine" and sheet.mutation_outcome == "not_started"


# ---- scale ---------------------------------------------------------------------------


def test_scale_per_axis_uses_the_general_type_and_reads_back():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)

    def scaled(b):
        block.volume, block.centroid = 3000.0, [10.0, 15.0, 2.5]
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factors=[2, 3, 0.5])
    assert ok.ok, ok
    b = nx.last("ScaleBuilder")
    assert b.Type is nx.nx.Features.ScaleBuilder.Types.General
    rhs = [b.ScaleXdirection.RightHandSide, b.ScaleYdirection.RightHandSide]
    assert rhs == ["2.0", "3.0"] and b.ScaleZdirection.RightHandSide == "0.5"
    block.volume, block.centroid = 1000.0, [5.0, 5.0, 5.0]
    wrong = run(nx, "nx_create_scale", bodies=[block.ref], factors=[2, 1, 1])  # NX did 3000
    assert wrong.error["details"]["check"] == "volume[0]"
    both = run(nx, "nx_create_scale", bodies=[block.ref], factor=2, factors=[2, 1, 1])
    assert both.error["category"] == "invalid_argument" and both.mutation_outcome == "not_started"
    ones = run(nx, "nx_create_scale", bodies=[block.ref], factors=[1, 1, 1])
    assert ones.error["field"] == "/factors"


def test_scale_along_a_datum_csys_takes_its_axes_and_origin():
    nx = modeling_nx()
    block = nx.block(10, 10, 10, origin=(100, 0, 0))
    frame = NS(Origin=NS(X=100.0, Y=0.0, Z=0.0),
               Orientation=NS(Element=NS(Xx=0.0, Xy=1.0, Xz=0.0, Yx=-1.0, Yy=0.0, Yz=0.0,
                                         Zx=0.0, Zy=0.0, Zz=1.0)))  # fmt: skip
    ref = nx.ref_of(frame, "datum_csys")
    frame.OwningPart, frame.IsOccurrence = nx.part, False

    def scaled(b):
        block.volume, block.centroid = 2000.0, [105.0, 10.0, 5.0]  # doubled along world y
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factors=[2, 1, 1], csys=ref)
    assert ok.ok, ok
    assert nx.last("ScaleBuilder").Csys is frame


def test_equal_factors_and_a_formula_factor_scale_uniformly():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)

    def scaled(b):
        block.volume, block.centroid = 8000.0, [10.0, 10.0, 10.0]
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factors=[2, 2, 2])
    assert ok.ok, ok
    assert nx.last("ScaleBuilder").Type is nx.nx.Features.ScaleBuilder.Types.Uniform


# ---- threads on several faces ----------------------------------------------------------


def test_threads_on_several_faces_share_one_call_and_follow_the_full_face(table):  # noqa: F811
    nx, body, cyl, start = thread_nx()
    end_faces(nx, cyl, start)
    other = nx.face(normal=(0, 0, 1), kind="cylinder", body=body)
    nx.remember(other)
    other.radius = 3.0
    end_faces(nx, other, start)
    ok = run(nx, "nx_create_thread", faces=[cyl.ref, other.ref], spec={"size": "M6"})
    assert ok.ok, ok
    built = [b for b in nx.builders if b.kind == "ThreadBuilder"]
    assert len(built) == 2 and ok.data["threads"][1]["length"] == 20
    full = nx.nx.Features.ThreadBuilder.LimitOption.Full
    assert all(b.ThreadLimit is full for b in built)
    expression(nx, "depth", 8.0)
    one = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"), spec={"size": "M6"},
              length="depth")  # fmt: skip
    assert one.ok and "threads" not in one.data, one
    b = nx.last("ThreadBuilder")
    assert b.ThreadLength.RightHandSide == "depth"
    assert b.ThreadLimit is nx.nx.Features.ThreadBuilder.LimitOption.Value
    both = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"),
               faces=[nx.ref_of(other, "face")], spec={"size": "M6"})  # fmt: skip
    assert both.error["category"] == "invalid_argument" and both.mutation_outcome == "not_started"


# ---- nx_get_feature: blend and chamfer edges -------------------------------------------


def test_get_feature_lists_the_edges_of_a_blend_and_a_chamfer():
    nx = modeling_nx()
    block = nx.block(20, 20, 20)
    e1, e2, e3 = block.edges[:3]
    blend = feature(nx, "BLEND", [block])
    blend.GetExpressions = lambda: []
    chamfer = feature(nx, "CHAMFER", [block])
    chamfer.GetExpressions = lambda: []

    def opened(b):  # the edit builder of the blend: two chainsets
        sets = [(NS(GetObjects=lambda: [e1, e2]), NS()), (NS(GetObjects=lambda: [e3]), NS())]
        object.__setattr__(b, "GetNumberOfValidChainsets", lambda: len(sets))
        object.__setattr__(b, "GetChainset", lambda i: sets[i])

    made = nx.builder

    def builder(kind, *args):
        b = made(kind, *args)
        if kind == "EdgeBlendBuilder":
            opened(b)
        if kind == "ChamferBuilder":
            object.__setattr__(b, "SmartCollector", NS(GetObjects=lambda: [e3, e1]))
        return b

    nx.builder = builder
    read = run(nx, "nx_get_feature", feature=nx.ref_of(blend, "feature"))
    assert read.ok and read.data["edge_count"] == 3, read
    assert [r["kind"] for r in read.data["edges"]] == ["edge"] * 3
    assert nx.last("EdgeBlendBuilder").destroyed and nx.last("EdgeBlendBuilder").commits == 0
    read = run(nx, "nx_get_feature", feature=nx.ref_of(chamfer, "feature"))
    assert read.data["edge_count"] == 2
    plain = run(nx, "nx_get_feature", feature=nx.ref_of(feature(nx, "BLOCK", [block]), "feature"))
    assert "edges" not in plain.data


# ---- expressions keep driving ----------------------------------------------------------


def changes(nx, kind, body, volume):
    def commit(b):
        body.volume = volume
        return feature(nx, kind.replace("Builder", "").upper(), [body])

    nx.on_commit(kind, commit)


def test_shell_draft_and_transform_keep_expression_values():
    nx = modeling_nx()
    expression(nx, "wall", 2.0)
    expression(nx, "taper", 5.0, DEG)
    block = nx.block(40, 20, 10)
    changes(nx, "ShellBuilder", block, 2000.0)
    ok = run(nx, "nx_create_shell", body=block.ref, thickness="wall",
             remove_faces=[block.face((0, 0, 1)).ref])  # fmt: skip
    assert ok.ok and nx.last("ShellBuilder").DefaultThickness.RightHandSide == "wall", ok
    side = nx.ref_of(block.face((1, 0, 0)), "face")
    bottom = nx.ref_of(block.face((0, 0, -1)), "face")
    from tests.unit.modeling.test_details_bodies import tilts

    tilts(nx, block, block.face((1, 0, 0)), 10, 1900.0)  # taper*2: the wall's read-back
    drafted = run(nx, "nx_create_draft", faces=[side], stationary_face=bottom,
                  direction="z", angle="taper*2")  # fmt: skip
    assert drafted.ok, drafted
    group = nx.last("DraftBuilder").FaceSetAngleExpressionList.Append.calls[0][0][0]
    assert group.args[1] == "taper*2"
    nx.nx.Features.MoveObject = NS

    def turned(b):  # the expression's 5 degrees about world z
        c = block.centroid if getattr(block, "centroid", None) else [20.0, 10.0, 5.0]
        a = math.radians(5.0)
        block.centroid = [c[0] * math.cos(a) - c[1] * math.sin(a),
                          c[0] * math.sin(a) + c[1] * math.cos(a), c[2]]  # fmt: skip
        return feature(nx, "MOVE_OBJECT", [block])

    nx.on_commit("MoveObjectBuilder", turned)
    turn = {"rotation": {"axis": "z", "angle": "taper"}}
    moved = run(nx, "nx_create_transform", bodies=[nx.ref_of(block, "body")], transform=turn)
    assert moved.ok, moved
    assert nx.last("MoveObjectBuilder").TransformMotion.Angle.RightHandSide == "taper"


def test_tube_rib_and_datum_offset_keep_expression_values():
    nx = modeling_nx()
    expression(nx, "od", 4.0)
    expression(nx, "gap", 15.0)
    (line,) = evaluable(nx, [seg((0, 0, 0), (50, 0, 0))])
    line.GetLength = lambda: 50.0
    want = math.pi / 4 * 16 * 50
    nx.on_commit(
        "TubeBuilder", lambda b: feature(nx, "TUBE", [new_body(nx, [0, -2, -2, 50, 2, 2], want)])
    )
    ok = run(nx, "nx_create_tube", path=line.ref, outer_diameter="od")
    assert ok.ok and nx.last("TubeBuilder").OuterDiameter.RightHandSide == "od", ok
    face = nx.block(10, 10, 10).face((0, 0, 1))
    datum = nx.datum_plane(origin=(0, 0, 25), normal=(0, 0, 1))
    plane = NS(Expression=NS(RightHandSide=None), SetUpdateOption=lambda o: None,
               SetMethod=lambda m: None, SetGeometry=lambda g: None, Evaluate=lambda: None)  # fmt: skip
    made = nx.builder

    def builder(kind, *args):
        b = made(kind, *args)
        if kind == "DatumPlaneBuilder":
            object.__setattr__(b, "GetPlane", lambda: plane)
        return b

    nx.builder = builder

    def committed(b):
        f = feature(nx, "DATUM_PLANE", [])
        f.DatumPlane = datum
        return f

    nx.on_commit("DatumPlaneBuilder", committed)
    off = run(nx, "nx_create_datum_plane", method="offset", reference=face.ref, distance="gap")
    assert off.ok, off
    assert plane.Expression.RightHandSide == "gap"


def test_geometry_helpers_used_by_the_plan():
    area, centre = lofts.polygon([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]])
    assert area == pytest.approx([0, 0, 4]) and centre == pytest.approx([1, 1, 0])
    assert geom.norm(lofts._flat([1, 1, 1], [0, 0, 1])) == pytest.approx(math.sqrt(2))


def test_a_thread_running_out_of_a_subtracted_bore_is_built_reversed(table):  # noqa: F811
    from tests.kit.objects import NXException

    nx, body, cyl, start = thread_nx()
    end_faces(nx, cyl, start)
    made = nx.builder
    refusals = [NXException(674805, "Thread depth must be greater than zero.")]

    def builder(kind, *args):
        b = made(kind, *args)
        if kind == "ThreadBuilder" and refusals:
            object.__setattr__(b, "error", refusals.pop())
        return b

    nx.builder = builder
    ok = run(nx, "nx_create_thread", face=cyl.ref, spec={"size": "M6"}, start_face=start.ref)
    assert ok.ok, ok
    built = [b for b in nx.builders if b.kind == "ThreadBuilder"]
    assert len(built) == 2 and built[1].ReverseThreadDirection is True
    assert all(b.destroyed for b in built)
