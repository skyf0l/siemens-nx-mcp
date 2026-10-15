"""Sheet sketches (curves in sheet coordinates) and hatches of their closed loops."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import sheet_sketch
from tests.kit import run
from tests.kit.fake import Factories
from tests.kit.objects import Object, Sketch, matrix
from tests.unit.drawing.fakes import drawing_nx


class Line(Object):
    def __init__(self, a, b):
        super().__init__("LINE")
        self.StartPoint, self.EndPoint = a, b

    def GetLength(self):
        a, b = self.StartPoint, self.EndPoint
        return math.dist((a.X, a.Y, a.Z), (b.X, b.Y, b.Z))


class Arc(Object):
    def __init__(self, center, radius, start, end):
        super().__init__("ARC")
        self.CenterPoint, self.Radius = center, radius
        self.StartAngle, self.EndAngle = start, end
        self.Matrix = NS(Element=matrix())

    def GetLength(self):
        return self.Radius * (self.EndAngle - self.StartAngle)


class Hatches(Factories):
    """``Annotations.Hatches``: a reopened builder shows what the hatch was built with."""

    def CreateHatchBuilder(self, existing):
        b = self.fake.builder("HatchBuilder", existing)
        if isinstance(existing, Object):  # reopened: what the hatch was built with
            for name, value in existing.values.items():
                setattr(b, name, value)
            b.Boundary.CurveBoundaries.Length = 1
        return b


def sheet_nx(units="mm", sheet_units="mm"):
    nx = drawing_nx(units)
    nx.sheet_obj = nx.make_sheet(units=sheet_units)
    nx.nx.Line, nx.nx.Arc, nx.nx.Ellipse = Line, Arc, type("Ellipse", (), {})
    nx.boxes = {}

    def own(c, box):
        c.OwningPart, c.IsOccurrence = nx.part, False
        nx.dependent[c.Tag] = (1, "SHT1@0")
        nx.boxes[c.Tag] = box
        return c

    def line(a, b):
        return own(Line(a, b), [min(a.X, b.X), min(a.Y, b.Y), 0, max(a.X, b.X), max(a.Y, b.Y), 0])

    def arc(c, _x, _y, r, s, e):
        return own(Arc(c, r, s, e), [c.X - r, c.Y - r, 0, c.X + r, c.Y + r, 0])

    class Curves(list):
        def CreateLine(self, a, b):
            self.append(line(a, b))
            return self[-1]

        def CreateArc(self, *args):
            self.append(arc(*args))
            return self[-1]

    nx.part.Curves = Curves()

    def sketch(b):
        s = Sketch(nx.session, f"SKETCH_SHT1_{len(nx.part.Sketches):03d}")
        s.IsDraftingSketch, s.OwningPart, s.IsOccurrence = True, nx.part, False
        nx.part.Sketches.append(s)
        return s

    nx.on_commit("SketchInDraftingBuilder", sketch)
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], Hatches=Hatches(nx))

    def hatch(b):
        h = Object("HATCH")
        h.OwningPart, h.IsOccurrence = nx.part, False
        names = ("AnnotationType", "Pattern", "Angle", "Distance", "Material", "AreafillAngle",
                 "Scale")  # fmt: skip
        h.values = {n: b.__dict__[n] for n in names if n in b.__dict__}
        defaults = {"Pattern": "Iron/General Use", "Angle": 45.0, "Distance": 5.0,
                    "AreafillAngle": 0.0, "Scale": 1.0}  # NX 2406's  # fmt: skip
        h.values = {**defaults, **h.values}
        boxes = [nx.boxes[c.Tag] for c in nx.section_curves]
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes), 0,
               max(b[3] for b in boxes), max(b[4] for b in boxes), 0]  # fmt: skip
        nx.boxes[h.Tag] = box
        h.AnnotationOrigin = NS(X=(box[0] + box[3]) / 2, Y=(box[1] + box[4]) / 2, Z=0)
        nx.dependent[h.Tag] = (1, "SHT1@0")
        if not isinstance(b.args[0], Object):  # a new hatch joins the part's hatches
            nx.part.Annotations.Hatches.append(h)
        return h

    nx.on_commit("HatchBuilder", hatch)
    nx.uf.ModlGeneral = NS(AskBoundingBox=lambda tag: nx.boxes[int(tag)])

    class Section:
        def __init__(self, *tolerances):
            pass

        def SetAllowedEntityTypes(self, allowed):
            pass

        def AddToSection(self, rules, *_args):
            nx.section_curves = [c for _kind, objs in rules for c in objs]

    nx.part.Sections = NS(CreateSection=Section)
    return nx


RECT = {"type": "rectangle", "corner1": [300, 40], "corner2": [380, 80]}


def test_sheet_sketch_places_curves_in_sheet_coordinates_and_reads_them_back():
    nx = sheet_nx()
    ok = run(nx, "nx_create_sheet_sketch", sheet="SHT1", name="LAYOUT", curves=[
        RECT,
        {"type": "circle", "center": [340, 150], "radius": 15},
        {"type": "arc", "center": [100, 100], "radius": 10, "start_angle": 0, "end_angle": 90},
        {"type": "polyline", "points": [[10, 10], [50, 10], [50, 30]]},
    ])  # fmt: skip
    assert ok.ok, ok
    rows = ok.data["curves"]
    assert len(rows) == 4 + 1 + 1 + 2 and [r["item"] for r in rows] == [0] * 4 + [1, 2, 3, 3]
    assert rows[0]["start"] == pytest.approx([300, 40]) and rows[2]["end"] == pytest.approx(
        [300, 80]
    )
    assert rows[4]["type"] == "circle" and rows[4]["radius"] == pytest.approx(15)
    assert rows[5]["start"] == pytest.approx([110, 100]) and rows[5]["end"] == pytest.approx(
        [100, 110]
    )
    closed = sorted((loop["curves"], loop["closed"]) for loop in ok.data["loops"])
    assert closed == [(1, False), (1, True), (2, False), (4, True)]
    assert ok.data["bounds"] == pytest.approx([10, 10, 380, 165])
    sk = nx.part.Sketches[-1]
    assert sk.Name == "LAYOUT" and nx.session.ActiveSketch is None and nx.live_builders == 0
    assert not ok.warnings


def test_an_inch_sheet_scales_points_to_part_units():
    nx = sheet_nx(units="mm", sheet_units="in")
    ok = run(nx, "nx_create_sheet_sketch", curves=[{"type": "line", "start": [1, 1],
             "end": [3, 1]}], sheet="SHT1")  # fmt: skip
    assert ok.ok, ok
    line = nx.part.Sketches[-1].geometry[0]
    assert pytest.approx((25.4, 76.2)) == (line.StartPoint.X, line.EndPoint.X)
    assert ok.data["curves"][0]["end"] == pytest.approx([3, 1])


def test_sheet_sketch_rules_are_checked_before_nx():
    nx = sheet_nx()
    cases = [
        ({"type": "rectangle", "corner1": [0, 0], "corner2": [0, 5]}, "/curves/0/corner2"),
        ({"type": "polyline", "points": [[0, 0], [1, 1]], "closed": True}, "/curves/0/points"),
        ({"type": "arc", "center": [5, 5], "radius": 2, "start_angle": 90, "end_angle": 90},
         "/curves/0/end_angle"),
        ({"type": "line", "start": [1, 1]}, "/curves/0/end"),
        ({"type": "line", "start": [1, 1], "end": [2, 2], "radius": 3}, "/curves/0/radius"),
        ({"type": "line", "start": [1, 1], "end": [1, 1]}, "/curves/0/end"),
    ]  # fmt: skip
    for item, field in cases:
        bad = run(nx, "nx_create_sheet_sketch", sheet="SHT1", curves=[item])
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (item, bad)
    assert not nx.part.Sketches


def test_a_sheet_sketch_nx_reactivates_on_opening_the_sheet_is_set_aside():
    nx = sheet_nx()
    old = Sketch(nx.session, "SKETCH_SHT1_OLD")
    old.IsDraftingSketch = True
    opened = nx.sheet_obj.Open

    def open_and_reactivate():  # NX 2406 re-activates the sheet's last sketch
        opened()
        old.Activate()

    nx.sheet_obj.Open = open_and_reactivate
    ok = run(nx, "nx_create_sheet_sketch", sheet="SHT1", curves=[RECT])
    assert ok.ok, ok
    assert nx.session.ActiveSketch is None and len(nx.part.Sketches[-1].geometry) == 4


def test_plan_expands_rectangles_and_closed_polylines():
    lines = sheet_sketch.plan({"type": "polyline", "points": [[0, 0], [4, 0], [4, 3], [0, 0]],
                               "closed": True}, "/c")  # fmt: skip
    assert [p[1] for p in lines] == [[0, 0], [4, 0], [4, 3]]


def _rect(nx):
    made = run(nx, "nx_create_sheet_sketch", sheet="SHT1", curves=[RECT])
    return [r["curve"]["id"] for r in made.data["curves"]]


def test_crosshatch_fills_a_closed_loop_with_its_pattern_angle_and_spacing():
    nx = sheet_nx()
    curves = _rect(nx)
    ok = run(nx, "nx_create_hatch", boundary=curves, pattern="steel", angle=30, spacing=4)
    assert ok.ok, ok
    assert ok.data["pattern"] == "steel" and ok.data["angle"] == pytest.approx(30)
    assert ok.data["spacing"] == pytest.approx(4) and ok.data["center"] == pytest.approx([340, 60])
    assert ok.data["bounds"] == pytest.approx([300, 40, 380, 80])
    assert ok.data["boundary"]["size"] == pytest.approx([80, 40])
    b = nx.builders[-2]  # the hatch builder (the last one read it back)
    assert b.Pattern == "Steel" and b.Boundary.SetView.calls[0][0][0] is nx.sheet_obj.View
    assert nx.live_builders == 0


def test_hatches_are_listed_with_the_annotations():
    nx = sheet_nx()
    ok = run(nx, "nx_create_hatch", boundary=_rect(nx), pattern="steel")
    assert ok.ok, ok
    listed = run(nx, "nx_list_objects", kind="annotation")
    rows = [r for r in listed.data["items"] if r["id"] == ok.data["annotation"]["id"]]
    assert len(rows) == 1, listed.data


def test_area_fill_takes_a_material_and_refuses_crosshatch_fields():
    nx = sheet_nx()
    curves = _rect(nx)
    ok = run(nx, "nx_create_hatch", boundary=curves, type="area_fill", material="concrete",
             scale=2)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["material"] == "concrete" and ok.data["scale"] == pytest.approx(2)
    assert ok.data["angle"] == pytest.approx(0)
    bad = run(nx, "nx_create_hatch", boundary=curves, type="area_fill", spacing=3)
    assert bad.error["field"] == "/spacing" and bad.mutation_outcome == "not_started"
    bad = run(nx, "nx_create_hatch", boundary=curves, material="sand")
    assert bad.error["field"] == "/material"


def test_a_hatch_needs_one_closed_loop_on_a_sheet():
    nx = sheet_nx()
    curves = _rect(nx)
    open_loop = run(nx, "nx_create_hatch", boundary=curves[:3])
    assert open_loop.error["field"] == "/boundary" and open_loop.mutation_outcome == "not_started"
    model = nx.part.Curves.CreateLine(NS(X=0, Y=0, Z=0), NS(X=1, Y=0, Z=0))
    nx.dependent.pop(model.Tag)
    off_sheet = run(nx, "nx_create_hatch", boundary=[nx.ref_of(model, "curve")])
    assert off_sheet.error["code"] == "NX_WRONG_OWNER"
    assert off_sheet.error["field"] == "/boundary/0"


def test_a_patterned_area_fill_may_stop_short_of_the_boundary():
    nx = sheet_nx()
    curves = _rect(nx)
    made = nx.hooks["HatchBuilder"]

    def short(b):  # whole pattern cells: the box lies inside the region
        h = made(b)
        box = nx.boxes[h.Tag]
        nx.boxes[h.Tag] = [box[0] + 0.7, box[1], 0, box[3], box[4] - 0.21, 0]
        return h

    nx.on_commit("HatchBuilder", short)
    ok = run(nx, "nx_create_hatch", boundary=curves, type="area_fill", material="concrete")
    assert ok.ok, ok
    assert ok.data["bounds"] == pytest.approx([300.7, 40, 380, 79.79])
    solid = run(nx, "nx_create_hatch", boundary=curves, type="area_fill")
    assert (
        solid.error["code"] == "NX_VERIFICATION_FAILED" and solid.mutation_outcome == "rolled_back"
    )
