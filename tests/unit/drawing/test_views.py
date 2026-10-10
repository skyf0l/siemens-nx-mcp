"""nx_create_drawing_view, nx_edit_drawing_view and nx_get_drawing_view on the fake."""

import pytest

from tests.kit import run
from tests.unit.drawing.fakes import (
    DEFAULT_STYLE,
    arc_edge,
    drawing_nx,
    install_eval,
    line_edge,
    make_view,
)


def placed_at(nx, b):
    """The sheet point a builder's Placement/Origin was given (fake part units = sheet mm)."""
    return list(b.Placement.Placement.SetValue.calls[-1][0][2].__dict__.values())[:2]


def views_nx(units="mm", sheet_units="mm"):
    nx = drawing_nx(units)
    nx.block(80, 50, 20)
    sheet = nx.make_sheet(units=sheet_units, size=(420.0, 297.0) if sheet_units == "mm" else
                          (16.5, 11.7))  # fmt: skip
    k = 1.0 if units == "mm" else 25.4
    k /= 1.0 if sheet_units == "mm" else 25.4

    def base(b):
        x, y = placed_at(nx, b)
        return make_view(nx, sheet, "Top@1", pos=(x / k + 15, y / k), native="BaseView",
                         scale=b.Scale.Numerator / b.Scale.Denominator)  # fmt: skip

    def projected(b):
        x, y = placed_at(nx, b)
        horizontal = b.Placement.AlignmentMethod is b.Placement.Method.Horizontal
        parent = nx.views[int(b.Parent.View.Value.Tag)]
        # NX puts the view at its own aligned spot; the alignment locks one axis.
        pos = (x / k + 7, parent.pos[1]) if horizontal else (parent.pos[0] + 30, y / k + 7)
        return make_view(nx, sheet, "ORTHO@2", pos=pos, native="ProjectedView",
                         scale=parent.scale, locked=1 if horizontal else 0)  # fmt: skip

    nx.on_commit("BaseViewBuilder", base)
    nx.on_commit("ProjectedViewBuilder", projected)
    nx.sheet_obj = sheet
    return nx


def test_base_view_lands_at_the_requested_point_with_dashed_hidden_lines():
    nx = views_nx()
    ok = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref,
             orientation="top", position=[120, 200])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["position"] == [120.0, 200.0] and ok.data["type"] == "base"
    assert ok.data["inside_sheet"] and ok.units["sheet_length"] == "mm"
    b = nx.last("BaseViewBuilder")
    assert b.Style.ViewStyleHiddenLines.HiddenLine is True
    assert b.Style.ViewStyleHiddenLines.SelfHidden is True
    assert nx.live_builders == 0
    view = nx.views[max(nx.views)]
    assert view.GetStringAttribute("NX_MCP_DRAWING_CONSTRUCTION_V1") == "hide"


def test_default_base_position_is_100_mm_on_an_inch_sheet():
    nx = views_nx(sheet_units="in")
    ok = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref, orientation="front")
    assert ok.data["position"] == pytest.approx([100 / 25.4, 100 / 25.4])
    assert ok.units["sheet_length"] == "in"


def test_base_view_of_a_multi_body_part_is_refused_before_nx():
    nx = views_nx()
    nx.block(5, 5, 5, origin=(100, 0, 0))
    bad = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref, orientation="top")
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.mutation_outcome == "not_started"
    missing = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref)
    assert missing.error["field"] == "/orientation"
    assert not nx.builders


def test_projected_view_moves_only_along_its_free_axis_and_keeps_the_parent():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200), scale=2)
    moves = []
    move = nx.uf.Draw.MoveView
    nx.uf.Draw.MoveView = lambda tag, p: (moves.append((tag, list(p))), move(tag, p))
    ok = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, direction="bottom")
    assert ok.ok, ok
    child = nx.views[max(nx.views)]
    # The x NX chose; y leaves the default 20 mm between the borders (parent y0 190).
    assert moves[-1] == (child.Tag, [150.0, 160.0])
    assert ok.data["position"] == [150.0, 160.0] and ok.data["scale"] == 2.0
    assert ok.data["gap"] == pytest.approx(20) and ok.data["bounds"][3] == 170.0
    assert ok.data["parent"]["id"] == parent.ref and parent.pos == [120, 200]
    b = nx.last("ProjectedViewBuilder")
    assert b.Style.ViewStyleProjected.Scale is True  # inherits the parent's scale
    assert child.GetStringAttribute("NX_MCP_DRAWING_CONSTRUCTION_V1") == "hide"
    right = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
                direction="Right", spacing=100)  # fmt: skip
    assert right.data["position"] == [260.0, 200.0] and right.data["gap"] == pytest.approx(100)


def test_projected_spacing_is_the_gap_between_the_borders_and_position_is_honoured():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200))  # border x 110..130
    borders = nx.uf.Draw.AskViewBorders

    def narrow(tag):  # a side view narrower than its parent, off its reference point
        b = borders(tag)
        return b if int(tag) == int(parent.Tag) else [b[0] + 7, b[1], b[0] + 13, b[3]]

    nx.uf.Draw.AskViewBorders = narrow
    ok = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
             direction="right", spacing=15)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["bounds"][0] == pytest.approx(145) and ok.data["gap"] == pytest.approx(15)
    assert ok.data["position"] == [148.0, 200.0] and ok.data["center"] == [148.0, 200.0]
    at = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
             direction="right", position=[300, 50])  # fmt: skip
    assert at.data["position"] == [300.0, 200.0] and not at.data["overlaps"]
    assert "stays aligned" in at.warnings[0]["message"]
    for args, field in [
        ({"direction": "right", "position": [300, 200], "spacing": 5}, "/position"),
        ({"direction": "left", "position": [300, 200]}, "/position"),
        ({"direction": "sideways"}, "/direction"),
    ]:
        bad = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert "right" in run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
                          direction="rigth").error["message"]  # fmt: skip


def test_overlapping_views_are_reported_and_warned():
    nx = views_nx()
    first = run(nx, "nx_create_drawing_view", type="base", orientation="Top", position=[120, 200])
    assert first.ok and first.data["sheet"]["id"] == nx.sheet_obj.ref  # the open sheet
    assert first.data["orientation"] == "top" and first.data["overlaps"] == []
    again = run(nx, "nx_create_drawing_view", type="base", orientation="plan", position=[125, 200])
    assert [r["id"] for r in again.data["overlaps"]] == [first.data["view"]["id"]]
    assert "overlaps 1 other view" in again.warnings[0]["message"]
    info = run(nx, "nx_get_drawing_view", view=first.data["view"]["id"])
    assert [r["id"] for r in info.data["overlaps"]] == [again.data["view"]["id"]]


def test_an_omitted_sheet_is_the_open_or_only_sheet():
    nx = views_nx()
    nx.part.DrawingSheets.CurrentDrawingSheet = None
    ok = run(nx, "nx_create_drawing_view", type="base", orientation="front", scale="1:2")
    assert ok.ok and ok.data["scale"] == 0.5 and ok.data["sheet"]["id"] == nx.sheet_obj.ref
    nx.make_sheet("SHT2")
    nx.part.DrawingSheets.CurrentDrawingSheet = None
    bad = run(nx, "nx_create_drawing_view", type="base", orientation="front")
    assert bad.error["field"] == "/sheet" and len(bad.error["details"]["sheets"]) == 2
    assert bad.mutation_outcome == "not_started"
    for scale in ("1:0", "x:2", "2000"):
        bad = run(nx, "nx_create_drawing_view", type="base", sheet=nx.sheet_obj.ref,
                  orientation="front", scale=scale)  # fmt: skip
        assert bad.error["field"] == "/scale" and bad.mutation_outcome == "not_started", scale


def test_a_view_type_names_everything_it_still_needs_at_once():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200))
    bad = run(nx, "nx_create_drawing_view", type="section", parent=parent.ref)
    assert bad.error["message"] == "A section view needs arrow_direction, position, cut"
    assert bad.error["field"] == "/arrow_direction" and "cut_point" in bad.error["hint"]
    stepped = run(nx, "nx_create_drawing_view", type="stepped_section", parent=parent.ref,
                  arrow_direction=[0, 1], position=[300, 100])  # fmt: skip
    assert stepped.error["message"] == "A stepped section view needs cuts"
    detail = run(nx, "nx_create_drawing_view", type="detail")
    assert detail.error["message"] == "A detail view needs parent, center, radius, position"


def test_projected_view_off_the_sheet_is_refused_before_nx():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(110, 200))
    bad = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
              direction="left", spacing=150)  # fmt: skip
    assert bad.error["field"] == "/spacing" and bad.mutation_outcome == "not_started"
    assert bad.error["details"]["parent_bounds"][0] == 100.0


def test_default_projection_gap_is_20_mm_on_an_inch_sheet():
    nx = views_nx(sheet_units="in")
    parent = make_view(nx, nx.sheet_obj, pos=(4, 5), scale=0.1)  # border x 3..5
    ok = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, direction="right")
    assert ok.data["gap"] == pytest.approx(20 / 25.4)
    assert ok.data["position"][0] == pytest.approx(5 + 20 / 25.4 + 1)


def test_section_cut_rules_are_checked_before_nx():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200))
    hole = arc_edge(nx, (25, 25, 20), 8)
    line = line_edge(nx, (0, 50, 20), (70, 50, 20))
    install_eval(nx, [hole, line])
    common = {"type": "section", "parent": parent.ref, "position": [300, 100],
              "step_direction": [1, 0], "arrow_direction": [0, 1]}  # fmt: skip
    for args, field, code in [
        (
            {"cut": hole.ref, "cut_point": "start", "step_direction": None},
            "/cut_point",
            "NX_WRONG_GEOMETRY",
        ),  # step_direction defaults to the arrow turned clockwise
        ({"cut": line.ref, "step_direction": [1, 1]}, "/arrow_direction", "NX_INVALID_ARGUMENT"),
        ({"cut": line.ref, "cut_point": "center"}, "/cut", "NX_WRONG_GEOMETRY"),
        ({"cut": line.ref, "arrow_direction": [1, 1]}, "/arrow_direction", "NX_INVALID_ARGUMENT"),
        ({"cut": line.ref, "arrow_direction": [0, 0]}, "/arrow_direction", "NX_INVALID_ARGUMENT"),
        ({"cut": parent.ref}, "/cut", "NX_WRONG_KIND"),
    ]:
        call = {k: v for k, v in {**common, **args}.items() if v is not None}
        bad = run(nx, "nx_create_drawing_view", **call)
        assert bad.error["field"] == field and bad.error["code"] == code, (args, bad)
        assert bad.mutation_outcome == "not_started"


def test_edit_reports_an_aligned_axis_and_reads_back_scale_and_position():
    nx = views_nx()
    view = make_view(nx, nx.sheet_obj, pos=(120, 200), native="ProjectedView", locked=1)
    ok = run(nx, "nx_edit_drawing_view", view=view.ref, position=[150, 200], scale=1.5)
    assert ok.ok, ok
    assert ok.data["position"] == [150.0, 200.0] and ok.data["scale"] == 1.5
    bad = run(nx, "nx_edit_drawing_view", view=view.ref, position=[150, 120])
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/position"
    assert bad.mutation_outcome == "rolled_back"
    none = run(nx, "nx_edit_drawing_view", view=view.ref)
    assert none.error["category"] == "invalid_argument" and none.mutation_outcome == "not_started"


def test_get_view_reports_type_bounds_and_hidden_line_warning():
    nx = views_nx()
    view = make_view(nx, nx.sheet_obj, pos=(120, 200), scale=2, native="DetailView")
    ok = run(nx, "nx_get_drawing_view", view=view.ref)
    assert ok.ok, ok
    assert ok.data["type"] == "detail" and ok.data["bounds"] == [100.0, 190.0, 140.0, 210.0]
    assert ok.data["center"] == [120.0, 200.0] and ok.data["parent"] is None
    assert ok.data["construction_geometry"] is None and ok.mutation_outcome == "not_applicable"
    sheet_ref = nx.sheet_obj.ref
    wrong = run(nx, "nx_get_drawing_view", view=sheet_ref)
    assert wrong.error["category"] == "wrong_kind" and wrong.error["field"] == "/view"


def test_edit_takes_a_ratio_scale_and_warns_about_derived_views_and_style_spill():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200))
    made = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, direction="top")
    child = nx.views[max(nx.views)]
    ok = run(nx, "nx_edit_drawing_view", view=parent.ref, scale="1:2")
    assert ok.ok and ok.data["scale"] == 0.5 and child.scale == 1.0
    assert any(made.data["view"]["id"] in w["message"] and "keep their scale" in w["message"]
               for w in ok.warnings)  # fmt: skip
    make = nx.part.SettingsManager.CreateDrawingEditViewSettingsBuilder

    def spilling(views):
        b = make(views)
        commit = b.Commit

        def both():
            commit()
            if views[0] is parent:  # NX changing a derived view along with its parent
                child.style = {g: dict(v) for g, v in DEFAULT_STYLE.items()}
                child.style["ViewStyleVisibleLines"]["VisibleFont"] = 0

        b.Commit = both
        return b

    nx.part.SettingsManager.CreateDrawingEditViewSettingsBuilder = spilling
    styled = run(nx, "nx_edit_drawing_view", view=parent.ref, style={"hidden_font": 0})
    assert styled.ok, styled
    spill = [w["message"] for w in styled.warnings if "also changed" in w["message"]]
    assert spill and "visible_font" in spill[0] and made.data["view"]["id"] in spill[0]


def test_listed_views_carry_their_sheet_and_border():
    nx = views_nx()
    make_view(nx, nx.sheet_obj, pos=(120, 200))
    ok = run(nx, "nx_list_objects", kind="drawing_view")
    assert ok.ok, ok
    row = ok.data["items"][0]
    assert row["details"]["bounds"] == [110.0, 195.0, 130.0, 205.0]
    assert row["details"]["sheet"] == "SHT1"
