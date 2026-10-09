"""nx_create_drawing_view, nx_edit_drawing_view and nx_get_drawing_view on the fake."""

import pytest

from tests.kit import run
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view


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
        return make_view(nx, sheet, "Top@1", pos=(x / k + 15, y / k), native="BaseView")

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
    assert moves[-1] == (child.Tag, [150.0, 140.0])  # the x NX chose, the requested y
    assert ok.data["position"] == [150.0, 140.0] and ok.data["scale"] == 2.0
    assert parent.pos == [120, 200]
    b = nx.last("ProjectedViewBuilder")
    assert b.Style.ViewStyleProjected.Scale is True  # inherits the parent's scale
    right = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
                direction="right", spacing=100)  # fmt: skip
    assert right.data["position"] == [220.0, 200.0]


def test_projected_view_off_the_sheet_is_refused_before_nx():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(110, 200))
    bad = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref,
              direction="left", spacing=150)  # fmt: skip
    assert bad.error["field"] == "/spacing" and bad.mutation_outcome == "not_started"
    assert bad.error["details"]["center"] == [-40.0, 200.0]


def test_default_projection_spacing_is_60_mm_on_an_inch_sheet():
    nx = views_nx(sheet_units="in")
    parent = make_view(nx, nx.sheet_obj, pos=(4, 5))
    ok = run(nx, "nx_create_drawing_view", type="projected", parent=parent.ref, direction="right")
    assert ok.data["position"][0] == pytest.approx(4 + 60 / 25.4)


def test_section_cut_rules_are_checked_before_nx():
    nx = views_nx()
    parent = make_view(nx, nx.sheet_obj, pos=(120, 200))
    hole = arc_edge(nx, (25, 25, 20), 8)
    line = line_edge(nx, (0, 50, 20), (70, 50, 20))
    install_eval(nx, [hole, line])
    common = {"type": "section", "parent": parent.ref, "position": [300, 100],
              "step_direction": [1, 0], "arrow_direction": [0, 1]}  # fmt: skip
    for args, field, code in [
        ({"cut": hole.ref, "cut_point": "start"}, "/cut_point", "NX_WRONG_GEOMETRY"),
        ({"cut": line.ref, "cut_point": "center"}, "/cut", "NX_WRONG_GEOMETRY"),
        ({"cut": line.ref, "arrow_direction": [1, 1]}, "/arrow_direction", "NX_INVALID_ARGUMENT"),
        ({"cut": line.ref, "arrow_direction": [0, 0]}, "/arrow_direction", "NX_INVALID_ARGUMENT"),
        ({"cut": parent.ref}, "/cut", "NX_WRONG_KIND"),
    ]:
        bad = run(nx, "nx_create_drawing_view", **{**common, **args})
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
    assert ok.data["construction_geometry"] is None and ok.mutation_outcome == "not_applicable"
    sheet_ref = nx.sheet_obj.ref
    wrong = run(nx, "nx_get_drawing_view", view=sheet_ref)
    assert wrong.error["category"] == "wrong_kind" and wrong.error["field"] == "/view"
