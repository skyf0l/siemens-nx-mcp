"""Bolt-circle centerlines and Section in View on the fake."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import annotations
from tests.kit import run
from tests.kit.objects import Object
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, make_view


class Centerlines(list):
    def __init__(self, nx):
        super().__init__()
        self.nx = nx

    def CreateBoltCircleCenterlineBuilder(self, existing):
        return self.nx.builder("BoltCircleCenterlineBuilder", existing)


def bolt_nx(scale=1.0, radius=30.0):
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    nx.top = make_view(nx, nx.sheet_obj, pos=(140, 200), scale=scale)
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], Centerlines=Centerlines(nx))
    nx.holes = [
        arc_edge(nx, (50 + radius * math.cos(a), 50 + radius * math.sin(a), 20), 4)
        for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2)
    ]
    install_eval(nx, nx.holes)
    nx.info = {}

    def commit(b):
        obj = Object("BOLT")
        obj.OwningPart, obj.IsOccurrence = nx.part, False
        nx.dependent[obj.Tag] = (1, "SHT1@0")
        nx.part.Annotations.Centerlines.append(obj)
        center, r = nx.info.get("circle", (None, None))
        if center is None:  # NX's circle through the picked hole centers
            pts = [nx.uf.View.MapModelToDrawing(nx.top.Tag, e.curve[1]) for e in nx.holes]
            center, r = annotations.circle_through(pts)
        arc = NS(ArcCenter=[center[0], center[1], 0.0], Radius=r)
        nx.info[obj.Tag] = NS(ArcInfo=[arc])
        return obj

    nx.on_commit("BoltCircleCenterlineBuilder", commit)
    nx.uf.Drf = NS(AskCenterlineInfo=lambda tag: (4, [0.0, 0.0, 0.0], nx.info[int(tag)]))
    return nx


def test_a_bolt_circle_through_four_holes_reads_back_its_center_and_radius():
    nx = bolt_nx(scale=0.5)
    refs = [e.ref for e in nx.holes]
    ok = run(nx, "nx_create_centerline", view=nx.top.ref, kind="bolt_circle", objects=refs)
    assert ok.ok, ok
    assert ok.data["center"] == pytest.approx([165, 225])  # model (50, 50) at half scale
    assert ok.data["radius"] == pytest.approx(30)  # the pitch circle in the model
    b = nx.last("BoltCircleCenterlineBuilder")
    assert b.FullCircle is True and len(b.Locations.Add.calls) == 4 and nx.live_builders == 0


def test_bolt_circle_refusals_come_before_nx():
    nx = bolt_nx()
    refs = [e.ref for e in nx.holes]
    two = run(nx, "nx_create_centerline", view=nx.top.ref, kind="bolt_circle", objects=refs[:2])
    assert two.error["field"] == "/objects" and two.mutation_outcome == "not_started"
    stray = arc_edge(nx, (90, 90, 20), 4)
    install_eval(nx, [*nx.holes, stray])
    off = run(nx, "nx_create_centerline", view=nx.top.ref, kind="bolt_circle",
              objects=[*refs[:3], stray.ref])  # fmt: skip
    assert off.error["field"] == "/objects/3" and off.mutation_outcome == "not_started"


def test_nx_drawing_another_circle_fails_the_read_back():
    nx = bolt_nx()
    nx.info["circle"] = ([140.0, 200.0], 25.0)
    bad = run(nx, "nx_create_centerline", view=nx.top.ref, kind="bolt_circle",
              objects=[e.ref for e in nx.holes])  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"


def test_circle_through_three_points():
    center, radius = annotations.circle_through([[1, 0], [0, 1], [-1, 0]])
    assert center == pytest.approx([0, 0]) and radius == pytest.approx(1)
    assert annotations.circle_through([[0, 0], [1, 1], [2, 2]]) is None


# ---- Section in View ---------------------------------------------------------------------


def section_nx():
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    nx.base = make_view(nx, nx.sheet_obj, "Top@1", pos=(140, 200))
    nx.sx = make_view(nx, nx.sheet_obj, "SX@2", pos=(140, 80), native="SectionView")
    nx.shaft, nx.housing = nx.block(10, 10, 10), nx.block(20, 20, 20, origin=(30, 0, 0))
    nx.cut = {int(nx.shaft.Tag), int(nx.housing.Tag)}
    objects = {}
    for body in (nx.shaft, nx.housing):  # a section solid draws its body's edges
        solid = Object("DRAFTING_BODY")
        solid.DraftingCurves = [Object("CURVE")]
        solid.edge = body.edges[0]
        objects.update({solid.Tag: solid, body.Tag: body, solid.edge.Tag: solid.edge})
        objects[solid.DraftingCurves[0].Tag] = solid
        body.solid = solid
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: objects[int(tag)])
    nx.nx.Drawings.SectionInViewBuilder.EditSxtype = NS(
        MakeNonSectioned="non", MakeSectioned="sec", RemoveViewSpecific="default"
    )

    def commit(b):
        mode = b.EditType
        for (body,), _ in b.EditObjects.Objects.Add.calls:
            if mode == "non":
                nx.cut.discard(int(body.Tag))
            elif mode == "sec":
                nx.cut.add(int(body.Tag))
        return None

    nx.on_commit("SectionInViewBuilder", commit)
    solids = {int(b.Tag): b.solid.Tag for b in (nx.shaft, nx.housing)}
    nx.uf.Draw.AskSxsolidsOfSxview = lambda tag, leg: (
        len(nx.cut),
        [solids[t] for t in sorted(nx.cut)],
    )
    nx.uf.Draw.AskDraftingCurveParents = lambda tag: (1, [objects[int(tag)].edge.Tag])
    return nx


def test_a_body_made_non_sectioned_leaves_the_cut_list():
    nx = section_nx()
    ok = run(nx, "nx_set_view_sectioning", view=nx.sx.ref, objects=[nx.shaft.ref],
             mode="non_sectioned")  # fmt: skip
    assert ok.ok, ok
    assert [r["id"] for r in ok.data["cut"]] == [nx.housing.ref]
    b = nx.last("SectionInViewBuilder")
    assert b.Views.Add.calls[0][0][0] is nx.sx and nx.live_builders == 0
    assert nx.part.DraftingViews.updated[-1] == [nx.sx]
    back = run(nx, "nx_set_view_sectioning", view=nx.sx.ref, objects=[nx.shaft.ref],
               mode="sectioned")  # fmt: skip
    assert back.ok and len(back.data["cut"]) == 2


def test_section_in_view_refuses_other_views_and_reads_back_what_nx_did():
    nx = section_nx()
    base = run(nx, "nx_set_view_sectioning", view=nx.base.ref, objects=[nx.shaft.ref],
               mode="non_sectioned")  # fmt: skip
    assert base.error["code"] == "NX_WRONG_KIND" and base.mutation_outcome == "not_started"
    nx.on_commit("SectionInViewBuilder", lambda b: None)  # NX ignored the change
    ignored = run(nx, "nx_set_view_sectioning", view=nx.sx.ref, objects=[nx.shaft.ref],
                  mode="non_sectioned")  # fmt: skip
    assert ignored.error["code"] == "NX_VERIFICATION_FAILED"
    assert ignored.mutation_outcome == "rolled_back"
