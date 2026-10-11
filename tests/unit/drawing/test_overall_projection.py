"""Overall-size notes on linear dimensions, projected views by orientation, out-of-date
views and the drawing-view rows of nx_list_objects."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import views
from tests.kit import run
from tests.unit.drawing.fakes import drawing_nx, install_eval, line_edge, make_view

FRONT = ((1, 0, 0), (0, 0, 1), (0, -1, 0))  # x, y, toward the viewer
TOP = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
RIGHT = ((0, 1, 0), (0, 0, 1), (1, 0, 0))

# ---- projected views by orientation -------------------------------------------------------


def test_projected_side_follows_the_projection_angle():
    assert views.projected_side(FRONT, "top", "third_angle") == "top"
    assert views.projected_side(FRONT, "top", "first_angle") == "bottom"
    assert views.projected_side(FRONT, "right", "third_angle") == "right"
    assert views.projected_side(FRONT, "right", "first_angle") == "left"
    assert views.projected_side(FRONT, "bottom", "first_angle") == "top"
    assert views.projected_side(TOP, "front", "third_angle") == "bottom"
    assert views.projected_side(FRONT, "back", "third_angle") is None  # along the view
    assert views.projected_side(FRONT, "isometric", "third_angle") is None
    assert views.projected_sides(FRONT, "first_angle") == {
        "top": "bottom", "bottom": "top", "right": "left", "left": "right"
    }  # fmt: skip


def projection_nx(angle, shows=TOP):
    nx = drawing_nx()
    nx.block(80, 50, 20)
    sheet = nx.make_sheet(angle=angle)
    parent = make_view(nx, sheet, "Front@1", pos=(200, 150), axes=FRONT)
    made = []

    def projected(b):
        x, y = list(b.Placement.Placement.SetValue.calls[-1][0][2].__dict__.values())[:2]
        horizontal = b.Placement.AlignmentMethod is b.Placement.Method.Horizontal
        pos = (x, parent.pos[1]) if horizontal else (parent.pos[0], y)
        view = make_view(nx, sheet, "ORTHO@2", pos=pos, native="ProjectedView",
                         axes=shows, locked=1 if horizontal else 0)  # fmt: skip
        made.append(view)
        return view

    nx.on_commit("ProjectedViewBuilder", projected)
    return nx, parent, made


def test_orientation_places_a_projected_view_on_the_side_of_the_sheet_angle():
    nx, parent, made = projection_nx("first")
    ok = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, orientation="top")
    assert ok.ok, ok
    assert ok.data["orientation"] == "top" and ok.data["position"][1] < 150  # below: 1st angle
    assert ok.data["gap"] == pytest.approx(20)
    nx, parent, made = projection_nx("third")
    ok = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, orientation="top",
             direction="top")  # fmt: skip
    assert ok.ok and ok.data["position"][1] > 150, ok  # above: 3rd angle, both agree


def test_orientation_contradicting_direction_or_not_projectable_is_refused_before_nx():
    nx, parent, made = projection_nx("first")
    both = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
               orientation="top", direction="top")  # fmt: skip
    assert both.error["field"] == "/direction" and both.mutation_outcome == "not_started"
    assert "first angle sheet the top view lies bottom" in both.error["message"]
    assert both.error["details"]["projects"]["right"] == "left"
    back = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
               orientation="back")  # fmt: skip
    assert back.error["field"] == "/orientation" and "base view" in back.error["hint"]
    none = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref)
    assert none.error["field"] == "/orientation" and "direction" in none.error["hint"]
    assert not made


def test_a_projected_view_showing_another_orientation_fails_verification():
    nx, parent, made = projection_nx("third", shows=RIGHT)
    bad = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, orientation="top")
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back"


# ---- overall-size notes ---------------------------------------------------------------------

L, W, H, R = 120.0, 100.0, 10.0, 5.0
CORNERS = [  # center, x axis (arc start), y axis (toward the arc end)
    ((R, R), (-1, 0, 0), (0, -1, 0)),
    ((L - R, R), (0, -1, 0), (1, 0, 0)),
    ((L - R, W - R), (1, 0, 0), (0, 1, 0)),
    ((R, W - R), (0, 1, 0), (-1, 0, 0)),
]


def quarter(nx, center, xa, ya):
    import math

    e = nx.edge(center, center, linear=False)
    e.curve = ("arc", list(map(float, center)), R, list(map(float, xa)), list(map(float, ya)),
               math.pi / 2)  # fmt: skip
    return e


def plate_nx():
    """A 120 x 100 x 10 plate with R5 vertical corner fillets, seen from the top, plus an
    80 mm inner edge (a slot)."""
    nx = drawing_nx()
    sheet = nx.make_sheet()
    view = make_view(nx, sheet, "TOP@1", pos=(200, 150), axes=TOP)
    e = NS(edges=[])
    for z in (0.0, H):
        lines = [
            line_edge(nx, (R, 0, z), (L - R, 0, z)),
            line_edge(nx, (L, R, z), (L, W - R, z)),
            line_edge(nx, (L - R, W, z), (R, W, z)),
            line_edge(nx, (0, W - R, z), (0, R, z)),
        ]
        arcs = [quarter(nx, (cx, cy, z), xa, ya) for (cx, cy), xa, ya in CORNERS]
        e.edges += lines + arcs
        if z == H:
            e.bottom, e.right, e.top, e.left = lines
    for x, y in ((R, 0), (L - R, 0), (L, R), (L, W - R), (L - R, W), (R, W), (0, W - R), (0, R)):
        e.edges.append(line_edge(nx, (x, y, 0), (x, y, H)))
    e.slot = line_edge(nx, (20, 50, H), (100, 50, H))
    e.edges.append(e.slot)
    body = NS(GetEdges=lambda: list(e.edges))
    for edge in e.edges:
        edge.GetBody = lambda: body
    install_eval(nx, e.edges)
    return nx, view, e


def drawn(nx, view, edges):
    """The view draws ``edges`` (drafting curves whose parents are those edges)."""
    curves = [NS(Tag=9000 + i, GetLength=lambda: 1.0) for i in range(len(edges))]
    parents = {9000 + i: (1, [int(edge.Tag)]) for i, edge in enumerate(edges)}
    view.DraftingBodies = [NS(DraftingCurves=curves)]
    nx.uf.Draw.AskDraftingCurveParents = lambda tag: parents[int(tag)]
    by_tag = {int(edge.Tag): edge for edge in edges}
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: by_tag[int(tag)])


def dimension(nx, value):
    def commit(b):
        d = NS(Tag=7000 + len(nx.part.Dimensions), Name="DIM", ComputedSize=value,
               AnnotationOrigin=b.Origin.OriginPoint, OwningPart=nx.part, IsOccurrence=False)  # fmt: skip
        nx.part.Dimensions.append(d)
        return d

    nx.on_commit("LinearDimensionBuilder", commit)


def notes(out):
    return [w for w in out.warnings if w["code"] == "NX_NOTE"]


def test_a_straight_edge_between_fillets_notes_the_overall_extent_and_the_outer_edges():
    nx, view, e = plate_nx()
    drawn(nx, view, [e.bottom, e.right, e.top, e.left])
    dimension(nx, L - 2 * R)
    ok = run(
        nx, "nx_create_drawing_dimension", view=view.ref, kind="horizontal", first=e.bottom.ref
    )
    assert ok.ok and ok.data["dimension"]["value"] == 110, ok
    [note] = notes(ok)
    assert "measures 110 but the body spans 120" in note["message"]
    assert f"first {e.left.ref} and second {e.right.ref}" in note["message"]
    dimension(nx, W - 2 * R)
    vertical = run(nx, "nx_create_drawing_dimension", view=view.ref, kind="vertical",
                   first=e.left.ref)  # fmt: skip
    [note] = notes(vertical)
    assert "spans 100" in note["message"] and f"first {e.bottom.ref}" in note["message"]


def test_overall_and_feature_sizes_stay_quiet():
    nx, view, e = plate_nx()
    dimension(nx, L)
    full = run(nx, "nx_create_drawing_dimension", view=view.ref, kind="horizontal",
               first=e.left.ref, second=e.right.ref)  # fmt: skip
    assert full.ok and not notes(full), full
    dimension(nx, 80.0)
    inner = run(nx, "nx_create_drawing_dimension", view=view.ref, kind="horizontal",
                first=e.slot.ref)  # fmt: skip
    assert inner.ok and not notes(inner), inner  # not on the outline


def test_without_drawn_outer_edges_the_note_names_the_ends():
    nx, view, e = plate_nx()
    dimension(nx, L - 2 * R)
    ok = run(nx, "nx_create_drawing_dimension", view=view.ref, kind="linear", first=e.bottom.ref)
    [note] = notes(ok)
    assert "two edges or points at the body's ends" in note["message"]


# ---- out-of-date views and the listed view rows ------------------------------------------------


def stale_nx():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    a = make_view(nx, sheet, "Front@1", pos=(100, 100))
    b = make_view(nx, sheet, "ORTHO@2", pos=(100, 200), native="ProjectedView")
    a.IsOutOfDate, b.IsOutOfDate = True, False

    def update(views_):
        nx.part.DraftingViews.updated.append(list(views_))
        for v in views_:
            v.IsOutOfDate = False

    nx.part.DraftingViews.UpdateViews = update
    return nx, sheet, a, b


def test_export_updates_and_reports_out_of_date_views():
    nx, sheet, a, b = stale_nx()
    nx.part.PlotManager = NS(CreatePrintPdfbuilder=lambda: nx.builder("PrintPdfBuilder"))
    nx.on_commit("PrintPdfBuilder", lambda b_: Path(b_.Filename).write_bytes(b"%PDF-1.7 fake"))
    nx.part.SaveOptions = NS(DrawingCgmData=False)
    ok = run(nx, "nx_export_drawing", path="out/a.pdf")
    assert ok.ok, ok
    assert [v["id"] for v in ok.data["updated_views"]] == [a.ref] and not a.IsOutOfDate
    again = run(nx, "nx_export_drawing", path="out/b.pdf")
    assert again.data["updated_views"] == []


def test_update_annotations_updates_out_of_date_views():
    nx, sheet, a, b = stale_nx()
    ok = run(nx, "nx_update_annotations")
    assert ok.ok, ok
    assert [r["id"] for r in ok.data["refreshed"]] == [a.ref] and not a.IsOutOfDate


def test_listed_views_carry_their_border_center():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    make_view(nx, sheet, "ORTHO@2", pos=(100, 200), native="ProjectedView")
    borders = nx.uf.Draw.AskViewBorders
    nx.uf.Draw.AskViewBorders = lambda tag: [b + 30 for b in borders(tag)]  # off the point
    ok = run(nx, "nx_list_objects", kind="drawing_view")
    details = ok.data["items"][0]["details"]
    assert details["position"] == [100.0, 200.0] and details["center"] == [130.0, 230.0]
    assert details["bounds"] == [120.0, 225.0, 140.0, 235.0]
