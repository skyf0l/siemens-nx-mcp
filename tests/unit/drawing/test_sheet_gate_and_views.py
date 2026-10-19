"""The sheet-bounds gate, projected views that follow a parent's move, view fonts read as
NX draws them, views that lag the model, sheet symbols in the listing, dimension text
without markup and tolerance decimals, and edge-on leader faces, on the fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.drawing import dimension_text, dimensions, hooks, style
from nx_mcp.kit import drawing
from tests.kit import run
from tests.kit.objects import Object
from tests.unit.drawing.fakes import DEFAULT_STYLE, make_view
from tests.unit.drawing.test_sheet_sketch_hatch import sheet_nx
from tests.unit.drawing.test_view_placement import parent_and_child, view_of
from tests.unit.drawing.test_views import views_nx


def test_the_gate_refuses_points_off_the_sheet_and_names_the_edge():
    sheet = NS(Length=420.0, Height=297.0)
    drawing.check_on_sheet(sheet, [420, 0], "/origin")  # on an edge
    drawing.check_on_sheet(sheet, None, "/origin")
    with pytest.raises(NXError) as caught:
        drawing.check_on_sheet(sheet, [9000, -50], "/origin")
    error = caught.value
    assert error.field == "/origin" and error.details["sheet_size"] == [420.0, 297.0]
    assert "x = 9000 is past 420" in error.message and "y = -50 is below 0" in error.message


def test_a_parent_moved_along_its_childs_free_axis_drags_the_child_keeping_the_gap():
    nx = views_nx()
    top, below = parent_and_child(nx)
    parent, child = view_of(nx, top), view_of(nx, below)
    gap = drawing.borders(NS(uf=nx.uf), parent)[1] - drawing.borders(NS(uf=nx.uf), child)[3]
    offset = child.pos[0] - parent.pos[0]  # NX aligns the geometry, not the reference points
    moved = run(nx, "nx_edit_drawing_view", view=top["id"], position=[120, 250])
    assert moved.ok, moved
    assert [r["id"] for r in moved.data["moved"]] == [below["id"]]
    borders = nx.uf.Draw.AskViewBorders
    assert borders(parent.Tag)[1] - borders(child.Tag)[3] == pytest.approx(gap)
    assert child.pos[0] - parent.pos[0] == pytest.approx(offset)
    assert below["id"] in [r["id"] for r in moved.changes["modified"]]


def test_a_move_that_would_push_the_child_off_the_sheet_is_refused_before_nx():
    nx = views_nx()
    top, below = parent_and_child(nx)
    child = view_of(nx, below)
    at = list(child.pos)
    off = run(nx, "nx_edit_drawing_view", view=top["id"], position=[120, 20])
    assert off.error["field"] == "/position" and off.mutation_outcome == "not_started", off
    assert off.error["details"]["view"]["id"] == below["id"]
    assert "off the 420 x 297 sheet" in off.error["message"] and child.pos == at


def test_a_rescale_that_would_push_the_child_off_the_sheet_names_the_child_and_scale():
    nx = views_nx()
    top, below = parent_and_child(nx)
    parent = view_of(nx, top)
    big = run(nx, "nx_edit_drawing_view", view=top["id"], scale=12)
    assert big.error["field"] == "/scale" and big.error["code"] == "NX_INVALID_ARGUMENT", big
    assert "ORTHO@2" in big.error["message"] and "scale 12" in big.error["message"]
    assert big.mutation_outcome == "not_started" and parent.scale == 1.0
    predicted = big.error["details"]["predicted_position"]
    assert not drawing.point_on_sheet(NS(Length=420.0, Height=297.0), predicted)


def test_nx_refusing_a_view_off_the_drawing_is_explained():
    nx = views_nx()
    top, _below = parent_and_child(nx)

    class Native(Exception):
        ErrorCode = 1405072

    def refuse(tag, scale):
        raise Native("The view is not located on the drawing.")

    nx.uf.Draw.SetViewScale = refuse
    bad = run(nx, "nx_edit_drawing_view", view=top["id"], scale=1.5)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/scale"
    assert bad.error["nx_code"] == 1405072 and "ORTHO@2" in bad.error["message"]


def test_a_visible_font_of_zero_reads_original_and_can_be_set():
    assert style.font_name(0, "visible_font") == "original"
    assert style.font_name(0, "hidden_font") == "invisible"
    nx = views_nx()
    view = make_view(nx, nx.sheet_obj, pos=(120, 200))
    view.style = {g: dict(v) for g, v in DEFAULT_STYLE.items()}
    view.style["ViewStyleVisibleLines"]["VisibleFont"] = 0  # NX 2406's projected views
    got = run(nx, "nx_get_drawing_view", view=view.ref)
    assert got.data["style"]["visible_font"] == "original", got
    solid = run(nx, "nx_edit_drawing_view", view=view.ref, style={"visible_font": "solid"})
    assert solid.ok and solid.data["style"]["visible_font"] == "solid", solid
    back = run(nx, "nx_edit_drawing_view", view=view.ref, style={"visible_font": "original"})
    assert back.ok and back.data["style"]["visible_font"] == "original", back


def test_a_view_nx_flags_out_of_date_is_reported_stale_by_reads_and_updated_by_edits():
    nx = views_nx()
    view = make_view(nx, nx.sheet_obj, pos=(120, 200))
    view.IsOutOfDate = True
    got = run(nx, "nx_get_drawing_view", view=view.ref)
    assert got.data["stale"] is True, got
    assert any("older model" in w["message"] for w in got.warnings)
    assert nx.part.DraftingViews.updated == []  # a read never updates a view

    def update(views):
        nx.part.DraftingViews.updated.append(list(views))
        for v in views:
            v.IsOutOfDate = False

    nx.part.DraftingViews.UpdateViews = update
    moved = run(nx, "nx_edit_drawing_view", view=view.ref, position=[130, 200])
    assert moved.ok and "stale" not in moved.data, moved
    assert view in nx.part.DraftingViews.updated[0]


def test_every_sheet_symbol_kind_is_listed_as_an_annotation():
    kinds = {}
    groups = {}
    for name in ("IdSymbols", *hooks.SYMBOL_GROUPS):
        obj = Object(name.upper())
        kinds[int(obj.Tag)] = name
        groups[name] = [obj]
    part = NS(Notes=[], Labels=[], Annotations=NS(**groups))
    listed = {int(o.Tag) for o in hooks._annotations(part)}
    assert listed == set(kinds)


def test_limits_text_loses_its_markup_and_tolerance_decimals_follow_the_format():
    assert dimension_text.plain("<T60.10!59.90>") == "60.10/59.90"
    assert dimension_text.plain("20") == "20"
    assert dimensions.decimals_of(0.05) == 2 and dimensions.decimals_of(2) == 0
    tx = NS(nx=NS(Annotations=NS(ToleranceType=NS(LimitTwoLines=5, NotSet=0))))
    d = NS(ToleranceType=5, NominalDecimalPlaces=1, UpperMetricToleranceValue=0.1,
           LowerMetricToleranceValue=-0.1)  # fmt: skip
    original = dimensions.units.part_units
    dimensions.units.part_units = lambda tx: "mm"
    try:
        tol = dimensions._tolerance_decimals(
            tx, d, {"decimals": 2}, {"type": "limits", "upper": 0.1, "lower": -0.1}
        )
        assert tol["decimals"] == 2
        finer = dimensions._tolerance_decimals(tx, d, {}, {"upper": 0.025})
        assert finer["decimals"] == 3  # the deviation needs 3
        kept = dimensions._tolerance_decimals(tx, d, {}, {"decimals": 4, "upper": 0.1})
        assert kept["decimals"] == 4
        assert dimensions._tolerance_decimals(tx, d, {}, None) is None
    finally:
        dimensions.units.part_units = original


def test_a_sheet_sketch_curve_past_the_sheet_is_refused_before_nx():
    nx = sheet_nx()
    made = len(nx.builders)
    off = run(nx, "nx_create_sheet_sketch", sheet="SHT1",
              curves=[{"type": "circle", "center": [900, -50], "radius": 5}])  # fmt: skip
    assert off.error["field"] == "/curves/0" and off.mutation_outcome == "not_started", off
    assert off.error["details"]["sheet_size"] == [420.0, 297.0] and len(nx.builders) == made
    edge = run(nx, "nx_create_sheet_sketch", sheet="SHT1",
               curves=[{"type": "arc", "center": [410, 100], "radius": 20, "start_angle": 90,
                        "end_angle": 270}])  # fmt: skip
    assert edge.ok, edge  # the arc's bulge stays left of the edge


def test_a_planar_face_seen_edge_on_is_warned_as_edge_on_not_behind_material():
    from nx_mcp.domains.drawing import common

    warnings = []
    face = NS(Tag=7)  # a face: no edge type
    front = NS(Name="Front@1", Matrix=NS(Xx=1, Xy=0, Xz=0, Yx=0, Yy=0, Yz=1, Zx=0, Zy=-1, Zz=0))
    tx = NS(uf=NS(Modeling=NS(AskFaceData=lambda tag: (22, [0, 0, 20], [0, 0, 1]))),
            warn=lambda code, message: warnings.append(message))  # fmt: skip
    common.warn_unseen(tx, face, front)  # the top face in a front view
    assert len(warnings) == 1 and "edge-on" in warnings[0] and "behind" not in warnings[0]
    assert common._edge_on_face(tx, face, NS(Matrix=NS(Xx=1, Xy=0, Xz=0, Yx=0, Yy=1, Yz=0,
                                                       Zx=0, Zy=0, Zz=1))) is None  # fmt: skip
