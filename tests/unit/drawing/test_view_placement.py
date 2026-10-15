"""View positions on and off the sheet, aligned parents and children, rescaling refusals,
empty detail views and view or sheet names in every drawing tool."""

from types import SimpleNamespace as NS

from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import install_eval, line_edge, make_view
from tests.unit.drawing.test_annotations import notes_nx
from tests.unit.drawing.test_views import views_nx


def parent_and_child(nx):
    top = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref,
              orientation="top", position=[120, 200])  # fmt: skip
    below = run(nx, "nx_create_drawing_view", type="projected", parent=top.data["view"]["id"],
                direction="bottom", spacing=20)  # fmt: skip
    assert top.ok and below.ok, (top, below)
    return top.data["view"], below.data["view"]


def view_of(nx, row):
    return next(v for v in nx.views.values() if v.Name == row["name"])


def test_a_parent_held_by_a_projected_view_is_refused_a_move_across_the_alignment():
    nx = views_nx()
    top, below = parent_and_child(nx)
    view_of(nx, top).locked = 0  # NX holds this parent's x
    across = run(nx, "nx_edit_drawing_view", view=top["id"], position=[160, 200])
    assert across.error["code"] == "NX_UNSUPPORTED_OPTION" and across.error["field"] == "/position"
    assert across.mutation_outcome == "rolled_back"
    assert [r["id"] for r in across.error["details"]["aligned_views"]] == [below["id"]]
    assert "along y" in across.error["hint"] and "ORTHO@2" in across.error["message"]
    child = run(nx, "nx_edit_drawing_view", view=below["id"], position=[200, 100])
    assert (
        child.error["code"] == "NX_UNSUPPORTED_OPTION" and child.mutation_outcome == "not_started"
    )
    up = run(nx, "nx_edit_drawing_view", view=top["id"], position=[120, 220])
    assert up.ok, up
    assert up.data["position"] == [120.0, 220.0]
    assert top["id"] in [r["id"] for r in up.changes["modified"]]


def test_a_parent_drags_its_projected_view_along_when_nx_allows():
    nx = views_nx()
    top, below = parent_and_child(nx)
    parent, child = view_of(nx, top), view_of(nx, below)
    move = nx.uf.Draw.MoveView

    def drag(tag, p):  # NX keeps the view below aligned: it follows the parent's x
        if int(tag) == int(parent.Tag):
            child.pos[0] += float(p[0]) - parent.pos[0]
        move(tag, p)

    nx.uf.Draw.MoveView = drag
    ok = run(nx, "nx_edit_drawing_view", view=top["id"], position=[140, 200])
    assert ok.ok, ok
    assert ok.data["position"] == [140.0, 200.0]
    assert {top["id"], below["id"]} <= {r["id"] for r in ok.changes["modified"]}


def test_positions_off_the_sheet_are_refused_before_nx():
    nx = views_nx()
    off = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref,
              orientation="front", position=[500, 100])  # fmt: skip
    assert off.error["field"] == "/position" and off.mutation_outcome == "not_started"
    assert off.error["details"]["sheet_size"] == [420.0, 297.0] and not nx.builders
    view = make_view(nx, nx.sheet_obj, pos=(120, 200))
    moved = run(nx, "nx_edit_drawing_view", view=view.ref, position=[600, 200])
    assert moved.error["field"] == "/position" and moved.mutation_outcome == "not_started"


def detail_nx(curves):
    """A parent view and a detail builder whose view draws ``curves`` drafting curves."""
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200))

    def detail(b):
        view = make_view(nx, nx.sheet_obj, "DETAIL@2", pos=(300, 60), scale=2.0,
                         native="DetailView")  # fmt: skip
        view.DraftingBodies = [NS(DraftingCurves=[NS(Tag=9500 + i) for i in range(curves)])]
        return view

    nx.part.DraftingViews.CreateDetailViewBuilder = lambda existing: nx.builder(
        "DetailViewBuilder", existing
    )
    nx.on_commit("DetailViewBuilder", detail)
    nx.part.Points = NS(CreatePoint=lambda p: NS(Blank=lambda: None))
    return nx, parent


def test_a_detail_view_with_no_geometry_is_refused_and_rolled_back():
    nx, parent = detail_nx(curves=0)
    empty = run(nx, "nx_create_drawing_view", type="detail", parent=parent.ref,
                center=[500, 500, 30], radius=5, position=[300, 60])  # fmt: skip
    assert empty.error["field"] == "/center" and empty.mutation_outcome == "rolled_back"
    assert "empty" in empty.error["message"]
    nx2, parent2 = detail_nx(curves=3)
    ok = run(nx2, "nx_create_drawing_view", type="detail", parent=parent2.ref,
             center=[80, 50, 20], radius=10, position=[300, 60])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["scale"] == 2.0  # the default detail scale


def test_views_and_sheets_are_taken_by_name_in_every_drawing_tool():
    nx = notes_nx()
    ok = run(nx, "nx_create_note", text=["N"], origin=[40, 40], sheet="sht1")
    assert ok.ok and ok.data["sheet"]["id"] == nx.sheet_obj.ref, ok
    info = run(nx, "nx_get_drawing_view", view="top")
    assert info.ok and info.data["view"]["id"] == nx.top.ref, info
    missing = run(nx, "nx_get_drawing_view", view="side")
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/view"
    nosheet = run(nx, "nx_create_note", text=["N"], origin=[40, 40], sheet="SHT9")
    assert nosheet.error["code"] == "NX_NOT_FOUND" and nosheet.error["field"] == "/sheet"


def test_a_leader_to_an_edge_behind_material_is_warned():
    nx = notes_nx()
    nx.sheet_obj.Open()
    body = NS(Tag=4343)
    edge = line_edge(nx, (0, 50, 0), (80, 50, 0))  # the bottom edge, seen from the top
    edge.GetBody = lambda: body
    install_eval(nx, [edge])

    def trace(count, tags, origin, direction, matrix, flag):
        hidden = origin[2] < 20 - 1e-6
        return (1, [NS(HitPoint=[origin[0], origin[1], 20.0])]) if hidden else (0, [])

    nx.uf.Modeling = NS(TraceARay=trace)
    nx.part.Annotations.CreateLeaderData = lambda: NS(
        Leader=NS(SetValue=lambda *a: None), StubSide=None
    )
    ok = run(nx, "nx_create_note", text=["HIDDEN"], origin=[40, 40], view=nx.top.ref,
             leader_to=edge.ref)  # fmt: skip
    assert any("behind material" in w["message"] for w in ok.warnings), ok


def test_turning_hidden_line_removal_off_is_warned():
    from tests.unit.drawing.test_view_settings import settings_nx

    nx, view, _front = settings_nx()
    ok = run(nx, "nx_set_view_settings", views=[view.ref], settings={"hidden_lines": False})
    assert ok.ok, ok
    assert any("hidden_font" in w["message"] for w in ok.warnings)


def test_update_lists_dimensions_whose_value_changed():
    nx = notes_nx()
    dimension = Object("DIM")
    dimension.OwningPart, dimension.IsOccurrence, dimension.ComputedSize = nx.part, False, 100.0
    dimension.AnnotationOrigin = point(10, 10, 0)
    dimension.NumberOfAssociativities = 1  # measured in the top view
    dimension.GetAssociativity = lambda i: NS(ObjectView=nx.top)
    nx.dependent[dimension.Tag] = (1, "SHT1@0")
    nx.part.Dimensions.append(dimension)

    def stale_view_update(views):  # the model changed: the view update re-measures
        dimension.ComputedSize = 120.0
        nx.top.IsOutOfDate = False

    nx.part.DraftingViews.UpdateViews = stale_view_update
    nx.top.IsOutOfDate = True
    ref = nx.ref_of(dimension, "dimension")
    ok = run(nx, "nx_update_annotations", objects=[ref])
    assert ok.ok, ok
    assert ref in [r["id"] for r in ok.data["refreshed"]]
    assert ref in [r["id"] for r in ok.changes["modified"]]
    assert "skipped" not in ok.data
