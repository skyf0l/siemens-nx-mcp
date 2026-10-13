"""Symmetrical and automatic centerlines (nx_create_centerline) on the fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import facts
from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view


class Centerlines(list):
    def __init__(self, nx):
        super().__init__()
        self.nx = nx

    def CreateSymmetricalCenterlineBuilder(self, existing):
        return self.nx.builder("SymmetricalCenterlineBuilder", existing)

    def CreateAutomaticCenterlineBuilder(self):
        return self.nx.builder("AutomaticCenterlineBuilder")


def mark(nx, x, y):
    obj = Object("CENTERLINE")
    obj.AnnotationOrigin = point(x, y, 0.0)
    obj.OwningPart, obj.IsOccurrence = nx.part, False
    nx.dependent[obj.Tag] = (1, "SHT1@0")
    return obj


def centerlines_nx():
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    nx.top = make_view(nx, nx.sheet_obj, pos=(100, 100))
    nx.side = make_view(nx, nx.sheet_obj, "Front@2", pos=(100, 40),
                        axes=((1, 0, 0), (0, 0, 1), (0, -1, 0)))  # fmt: skip
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], Centerlines=Centerlines(nx))
    nx.h1 = arc_edge(nx, (25, 25, 20), 8)
    nx.h2 = arc_edge(nx, (60, 25, 20), 5)
    nx.back = line_edge(nx, (0, 50, 20), (70, 50, 20))
    install_eval(nx, [nx.h1, nx.h2, nx.back])
    nx.origin = None

    def symmetrical(b):
        obj = mark(nx, *nx.origin)
        nx.part.Annotations.Centerlines.append(obj)
        return obj

    def automatic(b):  # NX commits no object: the marks just appear
        for hole in nx.holes:
            nx.part.Annotations.Centerlines.append(mark(nx, *hole))
        return None

    nx.holes = [(125.0, 125.0), (160.0, 125.0)]
    nx.on_commit("SymmetricalCenterlineBuilder", symmetrical)
    nx.on_commit("AutomaticCenterlineBuilder", automatic)
    return nx


def test_a_symmetrical_centerline_between_two_hole_centers():
    nx = centerlines_nx()
    nx.origin = (142.5, 125.0)  # midway between the hole centers in the top view
    ok = run(nx, "nx_create_centerline", view=nx.top.ref, kind="symmetrical",
             objects=[nx.h1.ref, nx.h2.ref])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["origin"] == pytest.approx([142.5, 125])
    b = nx.last("SymmetricalCenterlineBuilder")
    assert b.Start.SetValue.calls and b.End.SetValue.calls and nx.live_builders == 0
    nx.origin = (100.0, 100.0)
    off = run(nx, "nx_create_centerline", view=nx.top.ref, kind="symmetrical",
              objects=[nx.h1.ref, nx.h2.ref])  # fmt: skip
    assert off.error["code"] == "NX_VERIFICATION_FAILED" and off.mutation_outcome == "rolled_back"


def test_a_symmetrical_centerline_on_a_cylinder_seen_from_the_side(monkeypatch):
    nx = centerlines_nx()
    monkeypatch.setattr(facts, "face_point", lambda tx, face, steps=8: [25.0, 33.0, 10.0])
    cylinder = nx.face(normal=(0, 0, 1), origin=(25, 25, 0), kind="cylinder")
    nx.origin = (125.0, 50.0)  # on the axis x = 125 in the side view
    ok = run(nx, "nx_create_centerline", view=nx.side.ref, kind="symmetrical",
             objects=[cylinder.ref])  # fmt: skip
    assert ok.ok, ok
    assert nx.last("SymmetricalCenterlineBuilder").Face.SetValue.calls
    end_on = run(nx, "nx_create_centerline", view=nx.top.ref, kind="symmetrical",
                 objects=[cylinder.ref])  # fmt: skip
    assert end_on.error["code"] == "NX_UNSUPPORTED_GEOMETRY"
    assert end_on.mutation_outcome == "not_started"


def test_symmetrical_refusals():
    nx = centerlines_nx()
    for objects, field, code in [
        ([nx.back.ref, nx.h1.ref], "/objects/0", "NX_WRONG_GEOMETRY"),
        ([nx.h1.ref, nx.h1.ref], "/objects/1", "NX_INVALID_ARGUMENT"),
    ]:
        bad = run(nx, "nx_create_centerline", view=nx.top.ref, kind="symmetrical", objects=objects)
        assert (bad.error["field"], bad.error["code"]) == (field, code), bad
        assert bad.mutation_outcome == "not_started"
    same = arc_edge(nx, (25, 25, 0), 8)  # another circle on the same axis
    install_eval(nx, [nx.h1, same])
    bad = run(nx, "nx_create_centerline", view=nx.top.ref, kind="symmetrical",
              objects=[nx.h1.ref, same.ref])  # fmt: skip
    assert bad.error["field"] == "/objects" and bad.mutation_outcome == "not_started"
    missing = run(nx, "nx_create_centerline", view=nx.top.ref, kind="axis")
    assert missing.error["field"] == "/objects" and missing.mutation_outcome == "not_started"


def test_automatic_centerlines_report_the_marks_nx_added():
    nx = centerlines_nx()
    ok = run(nx, "nx_create_centerline", view=nx.top.ref, kind="automatic")
    assert ok.ok, ok
    assert len(ok.data["annotations"]) == 2
    assert ok.data["origins"] == [pytest.approx([125, 125]), pytest.approx([160, 125])]
    b = nx.last("AutomaticCenterlineBuilder")
    assert b.Views.Add.calls[-1][0][0] is nx.top and nx.live_builders == 0
    nx.holes = []
    none = run(nx, "nx_create_centerline", view=nx.top.ref, kind="automatic")
    assert none.ok and none.data["annotations"] == []
    assert none.warnings[0]["code"] == "NX_NO_EFFECT"
    bad = run(nx, "nx_create_centerline", view=nx.top.ref, kind="automatic", objects=[nx.h1.ref])
    assert bad.error["field"] == "/objects" and bad.mutation_outcome == "not_started"
