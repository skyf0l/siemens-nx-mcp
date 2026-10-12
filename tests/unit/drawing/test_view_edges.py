"""nx_list_view_edges on the fake: the drafting curves' parent edges in sheet coordinates."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge


def edges_nx(scale=2.0):
    nx = drawing_nx()
    sheet = nx.make_sheet()
    view = nx.make_view(sheet, "TOP@1", pos=(100, 100), scale=scale)
    front = line_edge(nx, (0, 0, 20), (80, 0, 20))
    left = line_edge(nx, (0, 0, 20), (0, 50, 20))
    slant = line_edge(nx, (70, 50, 20), (80, 40, 20))
    upright = line_edge(nx, (0, 0, 0), (0, 0, 20))  # seen end-on from the top
    hole = arc_edge(nx, (25, 25, 20), 8)
    hidden = line_edge(nx, (0, 50, 0), (80, 50, 0))  # no drafting curve: hidden
    install_eval(nx, [front, left, slant, upright, hole, hidden])
    # Drafting curves: the left edge is drawn in two visible pieces (partly hidden).
    drawn = [(front, 80.0), (left, 20.0), (left, 10.0), (slant, 200 ** 0.5), (upright, 20.0),
             (hole, 50.0)]  # fmt: skip
    curves = [NS(Tag=9000 + i, GetLength=lambda n=n: n) for i, (_, n) in enumerate(drawn)]
    parents = {9000 + i: (1, [int(e.Tag)]) for i, (e, _) in enumerate(drawn)}
    view.DraftingBodies = [NS(DraftingCurves=curves)]
    nx.uf.Draw.AskDraftingCurveParents = lambda tag: parents[int(tag)]
    by_tag = {int(e.Tag): e for e in (front, left, slant, upright, hole, hidden)}
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: by_tag[int(tag)])
    return nx, view, NS(front=front, left=left, slant=slant, hole=hole, hidden=hidden)


def test_visible_edges_come_longest_first_with_sheet_geometry():
    nx, view, e = edges_nx()
    ok = run(nx, "nx_list_view_edges", view=view.ref)
    assert ok.ok, ok
    items = ok.data["items"]
    assert [i["id"] for i in items] == [e.front.ref, e.left.ref, e.slant.ref, e.hole.ref]
    front = items[0]
    assert front["start"] == [100.0, 100.0] and front["end"] == [260.0, 100.0]
    assert front["length"] == 160.0 and front["model_length"] == pytest.approx(80.0)
    assert front["orientation"] == "horizontal" and front["center"] == [180.0, 100.0]
    assert items[1]["orientation"] == "vertical" and items[1]["partial"] is True
    assert items[2]["orientation"] == "inclined" and "partial" not in items[2]
    assert items[3]["type"] == "circle" and items[3]["radius"] == 8.0
    assert items[3]["center"] == [150.0, 150.0]
    assert ok.data["scale"] == 2.0 and ok.data["units"] == "mm"
    assert ok.page["total"] == 4 and ok.mutation_outcome == "not_applicable"
    assert ok.page["limit"] == 50  # a part's hole edges fit one page (20 made agents page)


def test_view_edges_filter_by_type_and_orientation_and_take_a_view_name():
    nx, view, e = edges_nx()
    vertical = run(nx, "nx_list_view_edges", view="top", orientation="vertical")
    assert [i["id"] for i in vertical.data["items"]] == [e.left.ref]
    circles = run(nx, "nx_list_view_edges", view=view.ref, geometry_type="circle")
    assert [i["id"] for i in circles.data["items"]] == [e.hole.ref]
    paged = run(nx, "nx_list_view_edges", view=view.ref, limit=1, offset=1)
    assert [i["id"] for i in paged.data["items"]] == [e.left.ref]
    assert paged.page["next_offset"] == 2


def test_view_edges_of_a_view_without_drafting_curves_is_an_empty_page():
    nx, view, _ = edges_nx()
    view.DraftingBodies = []
    ok = run(nx, "nx_list_view_edges", view=view.ref)
    assert ok.ok and ok.data["items"] == [] and ok.page["total"] == 0
