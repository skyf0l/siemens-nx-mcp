"""nx_list_view_edges on the fake: the drafting curves' parent edges in sheet coordinates."""

import json
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing.common import DETAIL
from tests.kit import run
from tests.kit.objects import matrix
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view


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


def trace_through_top(nx, edges, top=20.0):
    """Every edge on ``body``; a ray toward the top-view viewer (+z) hits material when it
    starts below the block's top face."""
    body = NS(Tag=4242)
    for e in edges:
        e.GetBody = lambda: body

    def trace(count, tags, origin, direction, matrix, flag):
        if origin[2] < top - 1e-6:
            return 1, [NS(HitPoint=[origin[0], origin[1], top])]
        return 0, []

    nx.uf.Modeling = NS(TraceARay=trace)
    return body


def test_edges_behind_material_are_left_out_or_flagged_on_request():
    nx, view, e = edges_nx()
    hidden = e.hidden
    nx.uf.Draw.AskDraftingCurveParents = _with_curve(nx, view, hidden)
    trace_through_top(nx, [e.front, e.left, e.slant, e.hole, hidden])
    ok = run(nx, "nx_list_view_edges", view=view.ref)
    assert ok.ok, ok
    assert hidden.ref not in [i["id"] for i in ok.data["items"]]
    every = run(nx, "nx_list_view_edges", view=view.ref, hidden=True)
    row = next(i for i in every.data["items"] if i["id"] == hidden.ref)
    assert row["hidden"] is True and row["length"] == pytest.approx(160.0)


def _with_curve(nx, view, edge):
    """Give ``edge`` a drafting curve in ``view`` (NX drafts hidden edges too)."""
    body = view.DraftingBodies[0]
    curve = NS(Tag=9100, GetLength=lambda: 80.0)
    body.DraftingCurves.append(curve)
    old = nx.uf.Draw.AskDraftingCurveParents
    return lambda tag: (1, [int(edge.Tag)]) if int(tag) == 9100 else old(tag)


def test_a_circle_seen_edge_on_is_listed_as_the_line_it_is_drawn_as():
    nx, view, e = edges_nx(scale=1.0)
    view.Matrix = matrix((1, 0, 0), (0, 0, 1), (0, -1, 0))  # a front view
    ok = run(nx, "nx_list_view_edges", view=view.ref, geometry_type="line",
             orientation="horizontal")  # fmt: skip
    hole = next(i for i in ok.data["items"] if i["id"] == e.hole.ref)
    assert hole["edge_on"] is True and hole["type"] == "line"
    assert hole["length"] == pytest.approx(16.0) and hole["center"] == pytest.approx([125, 120])


def test_a_detail_view_clips_its_edges_to_the_boundary_circle():
    nx, view, e = edges_nx(scale=1.0)
    detail = make_view(nx, nx.part.DrawingSheets[0], "DETAIL@2", pos=(300, 100), scale=3.0,
                       native="DetailView")  # fmt: skip
    detail.SetAttribute(DETAIL, json.dumps({"parent": "h1", "center": [80, 0, 20], "radius": 10}))
    detail.DraftingBodies = view.DraftingBodies
    ok = run(nx, "nx_list_view_edges", view=detail.ref)
    assert ok.ok, ok
    rows = {i["id"]: i for i in ok.data["items"]}
    front = rows[e.front.ref]  # x 300..540 on the sheet, cut at the circle (center 540, r 30)
    assert front["partial"] is True and front["start"] == pytest.approx([510, 100])
    assert front["end"] == pytest.approx([540, 100]) and front["model_length"] == pytest.approx(10)
    assert e.left.ref not in rows and e.hole.ref not in rows  # outside the boundary
