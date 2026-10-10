"""Auxiliary, broken, half/stepped/revolved section and break-out views on the fake."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view


def kinds_nx():
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet(size=(594.0, 420.0))
    nx.objects = {}
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: nx.objects[int(tag)])
    nx.bounds = {}  # view tag -> [x0, y0, x1, y1] overriding the fake's 20 x 10 border

    def borders(tag):
        v = nx.views[int(tag)]
        if int(tag) in nx.bounds:
            return list(nx.bounds[int(tag)])
        return [v.pos[0] - 10 * v.scale, v.pos[1] - 5 * v.scale, v.pos[0] + 10 * v.scale,
                v.pos[1] + 5 * v.scale]  # fmt: skip

    def to_model(tag, p):
        v = nx.views[int(tag)]
        m = v.Matrix
        u, w = (p[0] - v.pos[0]) / v.scale, (p[1] - v.pos[1]) / v.scale
        return [u * a + w * b for a, b in zip((m.Xx, m.Xy, m.Xz), (m.Yx, m.Yy, m.Yz), strict=True)]

    nx.uf.Draw.AskViewBorders = borders
    nx.uf.View.MapDrawingToModel = to_model
    return nx


TOP = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


class Collection(list):
    """An iterable NX collection with factory methods."""

    def __init__(self, **methods):
        super().__init__()
        self.__dict__.update(methods)


# ---- auxiliary ---------------------------------------------------------------------------


def aux_nx(scale=2.0):
    nx = kinds_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200), scale=scale, axes=TOP)
    hinge = line_edge(nx, (0, 0, 0), (30, 40, 0))  # seen along sheet (0.6, 0.8)
    install_eval(nx, [hinge, arc_edge(nx, (5, 5, 0), 2)])

    def projected(b):
        p = b.Placement.Placement.SetValue.calls[-1][0][2]
        # NX aligns the view on the hinge normal; the move puts it at the requested point.
        return make_view(nx, nx.sheet_obj, "AUXIL@2", pos=(p.X + 3, p.Y - 2),
                         scale=parent.scale, native="ProjectedView",
                         axes=((0.6, 0.8, 0), (0, 0, 1), (0.8, -0.6, 0)))  # fmt: skip

    nx.on_commit("ProjectedViewBuilder", projected)
    return nx, parent, hinge


def test_auxiliary_view_looks_normal_to_the_hinge_at_the_parent_scale():
    nx, parent, hinge = aux_nx()
    ok = run(nx, "nx_create_drawing_view", type="auxiliary", parent=parent.ref,
             hinge=hinge.ref, position=[300, 300])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["position"] == [300.0, 300.0] and ok.data["scale"] == 2.0
    assert ok.data["hinge"] == {"model_length": 50.0, "view_length": 50.0}
    assert ok.data["toward_viewer"] == [0.8, -0.6, 0.0]
    assert parent.pos == [120, 200]
    b = nx.last("ProjectedViewBuilder")
    line = b.Style.ViewStyleOrientation.HingeLine
    assert line.SpecifyVector.Vector.__dict__ == {"X": 0.6, "Y": 0.8, "Z": 0.0}  # sheet vector
    assert line.Associative is False and b.Style.ViewStyleProjected.Scale is True
    assert nx.live_builders == 0


def test_auxiliary_hinge_and_position_are_checked_before_nx():
    nx, parent, _ = aux_nx()
    end_on = line_edge(nx, (0, 0, 0), (0, 0, 10))
    arc = arc_edge(nx, (5, 5, 0), 2)
    on_line = line_edge(nx, (0, 0, 0), (30, 40, 0))
    install_eval(nx, [end_on, arc, on_line])
    for hinge, position, field in [
        (end_on, [300, 300], "/hinge"),
        (arc, [300, 300], "/hinge"),
        (on_line, [120 + 60, 200 + 80], "/position"),  # on the hinge line through the parent
    ]:
        bad = run(nx, "nx_create_drawing_view", type="auxiliary", parent=parent.ref,
                  hinge=hinge.ref, position=position)  # fmt: skip
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not nx.builders


def test_auxiliary_view_that_is_not_true_length_fails_verification():
    nx, parent, hinge = aux_nx()
    nx.on_commit("ProjectedViewBuilder", lambda b: make_view(
        nx, nx.sheet_obj, "AUXIL@2", pos=(300, 300), scale=2.0, native="ProjectedView",
        axes=TOP))  # fmt: skip
    hinge.curve = ("line", [0.0, 0.0, 0.0], [30.0, 0.0, 40.0])  # tilted out of the sheet
    bad = run(nx, "nx_create_drawing_view", type="auxiliary", parent=parent.ref,
              hinge=hinge.ref, position=[300, 300])  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    assert bad.error["details"]["check"] == "hinge_length"


# ---- broken ------------------------------------------------------------------------------


def broken_nx(shrink=True):
    nx = kinds_nx()
    view = make_view(nx, nx.sheet_obj, "Front@1", pos=(150, 100), axes=TOP)
    nx.bounds[view.Tag] = [43.65, 78.65, 256.35, 121.35]
    nx.part.Annotations.Centerlines = []

    def broken(b):
        a = b.BreakLine1ModelAnchor.Coordinates
        c = b.BreakLine2ModelAnchor.Coordinates
        k = 0 if b.DirectionVector.Vector.X else 1
        removed = abs((c.X - a.X) if k == 0 else (c.Y - a.Y)) - float(b.Gap.RightHandSide)
        if shrink:
            nx.bounds[view.Tag][k + 2] -= removed
        return NS(kind="view_break")

    nx.on_commit("BrokenViewBuilder", broken)
    return nx, view


def test_broken_view_removes_each_band_and_keeps_its_position():
    nx, view = broken_nx()
    ok = run(nx, "nx_create_drawing_view", type="broken", parent=view.ref,
             breaks=[[160, 200], [100, 120]])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["bounds"] == [43.65, 78.65, 256.35 - 30 - 10, 121.35]
    assert ok.data["breaks"][0] == {"axis": "x", "start": 160.0, "end": 200.0, "gap": 10.0}
    b = nx.last("BrokenViewBuilder")
    assert b.IsBreakLine1Associative is False and b.MasterView.Value is view
    assert b.BreakLine1ModelAnchor.Coordinates.__dict__ == {"X": -50.0, "Y": 0.0, "Z": 0.0}
    y = run(nx, "nx_create_drawing_view", type="broken", parent=view.ref, axis="y",
            breaks=[[85, 95]], gap=4)  # fmt: skip
    assert y.ok and y.data["bounds"][3] == 121.35 - 6
    assert nx.live_builders == 0


def test_all_break_anchors_are_mapped_before_the_first_break():
    nx, view = broken_nx()
    original, committed = nx.uf.View.MapDrawingToModel, []
    nx.on_commit("BrokenViewBuilder", lambda b, done=nx.hooks["BrokenViewBuilder"]: (
        committed.append(b), done(b))[1])  # fmt: skip
    # After a break NX maps sheet points of the shortened view differently.
    nx.uf.View.MapDrawingToModel = lambda tag, p: [
        original(tag, p)[0] + 100 * len(committed), *original(tag, p)[1:]]  # fmt: skip
    ok = run(nx, "nx_create_drawing_view", type="broken", parent=view.ref,
             breaks=[[160, 200], [100, 120]])  # fmt: skip
    assert ok.ok, ok
    second = nx.last("BrokenViewBuilder")
    assert second.BreakLine1ModelAnchor.Coordinates.X == -50.0  # from the unbroken view
    assert second.BreakLine2ModelAnchor.Coordinates.X == -30.0


def test_broken_view_bands_are_checked_before_nx():
    nx, view = broken_nx()
    line = Object("CENTERLINE")
    line.AnnotationOrigin = point(180, 100, 0)
    nx.ref_of(line, "annotation")
    for breaks, gap, field in [
        ([[30, 60]], None, "/breaks/0"),  # outside the view
        ([[100, 105]], None, "/breaks/0"),  # narrower than the gap
        ([[100, 130], [120, 140]], None, "/breaks/1"),  # overlap
        ([[100, 130]], 2000, "/gap"),
    ]:
        bad = run(nx, "nx_create_drawing_view", type="broken", parent=view.ref, breaks=breaks,
                  **({"gap": gap} if gap else {}))  # fmt: skip
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    nx.part.Annotations.Centerlines = [line]
    hidden = run(nx, "nx_create_drawing_view", type="broken", parent=view.ref, breaks=[[160, 200]])
    assert hidden.error["field"] == "/breaks/0" and "centerline" in hidden.error["message"]
    assert not nx.builders


def test_broken_view_that_nx_did_not_shorten_rolls_back():
    nx, view = broken_nx(shrink=False)
    bad = run(nx, "nx_create_drawing_view", type="broken", parent=view.ref, breaks=[[160, 200]])
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"


# ---- half, stepped and revolved sections -------------------------------------------------


def section_nx():
    nx = kinds_nx()
    parent = make_view(nx, nx.sheet_obj, "ORTHO@2", pos=(150, 120), axes=TOP)
    holes = [arc_edge(nx, c, 4) for c in ((40, 10, 40), (100, 20, 40), (160, 8, 40))]
    install_eval(nx, holes)
    cut = nx.nx.UF.Draw.SxsegType.SXSEG_CUT
    segments = {}

    def make(*args):
        child = make_view(nx, nx.sheet_obj, "SX@3", pos=(150, 120), native="SectionView",
                          axes=((1, 0, 0), (0, 0, 1), (0, -1, 0)))  # fmt: skip
        # One cut segment per cut point, along the step direction (sheet x) at its y.
        for i, y in enumerate((110.0, 120.0)):
            tag = 900 + i
            nx.objects[tag] = NS(StartPoint=point(30.0, y, 0.0), EndPoint=point(270.0, y, 0.0))
            segments[700 + i] = (NS(SxsegType=cut), tag, None)
        nx.objects[child.Tag] = child
        return child.Tag

    draw = nx.uf.Draw
    draw.CreateHalfSxview = Mock(side_effect=make)
    draw.CreateSteppedSxview = Mock(side_effect=make)
    draw.CreateRevolvedSxview = Mock(side_effect=make)
    draw.AskSxlineOfSxview = lambda tag: 55
    draw.AskSimpleSxline = Mock(side_effect=RuntimeError("not a simple section line"))
    draw.AskHalfSxline = Mock(side_effect=RuntimeError("not a half section line"))
    draw.AskSteppedSxline = lambda line: ([1, 0, 0], [0, 1, 0], 1, 1, [3], 2, [700, 701], 0)
    draw.AskSxlineSxseg = lambda tag: segments[tag]
    draw.SetViewScale = lambda tag, s: setattr(nx.views[int(tag)], "scale", float(s))
    return nx, parent, holes


def section_args(parent, **extra):
    return {"parent": parent.ref, "step_direction": [1, 0], "arrow_direction": [0, 1],
            "position": [150, 45], **extra}  # fmt: skip


def test_stepped_section_reports_its_cut_planes():
    nx, parent, holes = section_nx()
    cuts = [{"edge": holes[0].ref}, {"edge": holes[1].ref, "at": "center"}]
    ok = run(nx, "nx_create_drawing_view", type="stepped_section",
             **section_args(parent, cuts=cuts, scale=0.5))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["position"] == [150.0, 45.0] and ok.data["scale"] == 0.5
    assert ok.data["cut_planes"] == [
        {"normal": [0.0, 1.0, 0.0], "offset": -10.0},
        {"normal": [0.0, 1.0, 0.0], "offset": 0.0},
    ]
    args = nx.uf.Draw.CreateSteppedSxview.call_args[0]
    assert args[1] == 0.5 and args[5] == 2 and args[7] == [150.0, 45.0]


def test_half_section_passes_one_struct_per_segment():
    nx, parent, holes = section_nx()
    ok = run(nx, "nx_create_drawing_view", type="half_section",
             **section_args(parent, cut=holes[1].ref, cut_point="center",
                            bend={"edge": holes[1].ref}))  # fmt: skip
    assert ok.ok, ok
    args = nx.uf.Draw.CreateHalfSxview.call_args[0]
    assert args[5] == 2 and len(args[6]) == 2  # cut and bend
    missing = run(nx, "nx_create_drawing_view", type="half_section",
                  **section_args(parent, cut=holes[1].ref))  # fmt: skip
    assert missing.error["field"] == "/bend" and missing.mutation_outcome == "not_started"


def test_revolved_section_refuses_legs_on_one_line_before_nx():
    nx, parent, holes = section_nx()
    rotation = {"edge": holes[1].ref}
    ok = run(nx, "nx_create_drawing_view", type="revolved_section",
             **section_args(parent, rotation=rotation,
                            cuts=[{"edge": holes[0].ref}, {"edge": holes[2].ref}]))  # fmt: skip
    assert ok.ok, ok
    assert nx.uf.Draw.CreateRevolvedSxview.call_args[0][6:8] == (2, 1)
    calls = nx.uf.Draw.CreateRevolvedSxview.call_count
    straight = arc_edge(nx, (160, 30, 40), 4)
    install_eval(nx, [*holes, straight])
    for cuts, field in [
        ([{"edge": holes[0].ref}, {"edge": straight.ref}], "/cuts"),  # 40,10 / 100,20 / 160,30
        ([{"edge": holes[0].ref}, {"edge": holes[1].ref}], "/cuts"),  # on the rotation point
        ([{"edge": holes[0].ref}] * 3, "/cuts"),
        ([{"edge": holes[0].ref}, {"edge": holes[2].ref, "at": "start"}], "/cuts/1/at"),
    ]:
        bad = run(nx, "nx_create_drawing_view", type="revolved_section",
                  **section_args(parent, rotation=rotation, cuts=cuts))  # fmt: skip
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert nx.uf.Draw.CreateRevolvedSxview.call_count == calls


# ---- break-out ---------------------------------------------------------------------------


def break_out_nx(floor_offset=0.0):
    nx = kinds_nx()
    view = make_view(nx, nx.sheet_obj, "Front@1", pos=(150, 360),
                     axes=((1, 0, 0), (0, 0, 1), (0, -1, 0)))  # fmt: skip
    nx.objects[view.Tag] = view
    expand = []
    nx.uf.View.ExpandView = lambda tag: expand.append(("expand", int(tag)))
    nx.uf.View.UnexpandWorkView = lambda: expand.append(("unexpand",))
    nx.expand = expand
    part = nx.part
    part.NXMatrices = NS(Create=lambda m: ("matrix", m))
    part.Curves = Collection(CreateArc=Mock(return_value=NS(Tag=801)))
    part.Scalars = NS(CreateScalar=lambda v, *_: v)
    created = {}

    def create_point(*args):
        if len(args) == 1:
            return NS(kind="point", Coordinates=args[0])
        created["base"] = list(args[:3])
        return NS(Tag=802)

    part.Points = Collection(CreatePoint=create_point)

    def breakout(tag, data):
        x, y, z = created["base"]
        nx.objects[803] = NS(Coordinates=point(x, y - floor_offset, z))
        nx.breakout = data
        return 900

    nx.uf.Draw.CreateBreakout = breakout
    nx.uf.Draw.AskBreakoutData = lambda tag: (view.Tag, NS(BasePoint=803, NumCurves=1))
    return nx, view


def test_break_out_cuts_to_the_depth_below_center_in_the_view():
    nx, view = break_out_nx()
    ok = run(nx, "nx_create_drawing_view", type="break_out", parent=view.ref,
             center=[40, 0, 20], radius=8, depth=10)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["break_out"] == {"depth": 10.0, "radius": 8.0, "curves": 1}
    assert nx.expand == [("expand", view.Tag), ("unexpand",)]
    assert nx.breakout.Curves == [801] and nx.breakout.BasePoint == 802
    arc = nx.part.Curves.CreateArc.call_args[0]
    assert arc[2:] == (8.0, 0.0, 2 * 3.141592653589793)


def test_break_out_with_a_wrong_floor_rolls_back():
    nx, view = break_out_nx(floor_offset=1.0)
    bad = run(nx, "nx_create_drawing_view", type="break_out", parent=view.ref,
              center=[40, 0, 20], radius=8, depth=10)  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    missing = run(nx, "nx_create_drawing_view", type="break_out", parent=view.ref,
                  center=[40, 0, 20], radius=8)  # fmt: skip
    assert missing.error["field"] == "/depth" and missing.mutation_outcome == "not_started"
