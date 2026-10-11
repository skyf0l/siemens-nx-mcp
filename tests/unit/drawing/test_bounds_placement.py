"""Annotation bounds, NX_VIEW_OVERLAP warnings, default dimension text outside the view and
kind horizontal/vertical, on the fake."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.test_annotations import notes_nx
from tests.unit.drawing.test_dimensions import dimension_hook, dims_nx

TEXT = (30.0, 4.0)  # fake text block: length, height (part units = sheet mm)


def boxes(nx, extra=None):
    """UF boxes for the fake's notes: the text block right of the origin, the whole extent
    one unit larger; ``extra`` maps tags to whole extents."""

    def by_tag(tag):
        for obj in [*nx.part.Notes, *(extra or {}).values()]:
            if int(obj.Tag) == int(tag):
                return obj
        raise KeyError(tag)

    def whole(tag):
        if extra and int(tag) in extra:
            return extra[int(tag)].box
        o = by_tag(tag).AnnotationOrigin
        return [o.X - 1, o.Y - TEXT[1] / 2 - 1, 0.0, o.X + TEXT[0] + 1, o.Y + TEXT[1] / 2 + 1, 0.0]

    def text(tag):
        o = by_tag(tag).AnnotationOrigin
        return [o.X, o.Y + TEXT[1] / 2, 0.0], TEXT[0], TEXT[1]

    nx.uf.ModlGeneral = NS(AskBoundingBox=whole)
    nx.uf.Drf = NS(AskAnnotationTextBox=text)


def test_a_note_reports_its_bounds_and_warns_over_a_view_or_off_the_sheet():
    nx = notes_nx()
    boxes(nx)
    clear = run(nx, "nx_create_note", text=["CLEAR"], sheet=nx.sheet_obj.ref, origin=[200, 50])
    assert clear.ok and clear.data["bounds"] == [199.0, 47.0, 231.0, 53.0]
    assert not clear.warnings
    over = run(nx, "nx_create_note", text=["OVER"], sheet=nx.sheet_obj.ref, origin=[95, 100])
    assert over.ok and [w["code"] for w in over.warnings] == ["NX_VIEW_OVERLAP"]
    assert "over view" in over.warnings[0]["message"]
    off = run(nx, "nx_create_note", text=["OFF"], sheet=nx.sheet_obj.ref, origin=[400, 50])
    assert [w["code"] for w in off.warnings] == ["NX_VIEW_OVERLAP"]
    assert "off the sheet" in off.warnings[0]["message"]


def wide_view(nx):
    """The fake top view's border made to hold its geometry (x 90-190, y 90-160)."""
    borders = nx.uf.Draw.AskViewBorders
    nx.uf.Draw.AskViewBorders = lambda tag: (
        [90.0, 90.0, 190.0, 160.0] if int(tag) == int(nx.top.Tag) else borders(tag)
    )


def test_default_dimension_text_lands_outside_the_view_and_steps_past_others():
    nx = dims_nx()
    wide_view(nx)
    nx.on_commit("LinearDimensionBuilder", dimension_hook(nx, 70.0))
    first = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="linear",
                first=nx.back.ref, orientation="horizontal")  # fmt: skip
    assert first.ok, first
    assert first.data["origin"] == pytest.approx([135.0, 170.0])  # 10 mm above the border
    taken = Object("NOTE")
    taken.AnnotationOrigin = point(130.0, 170.0, 0.0)
    taken.box = [120.0, 165.0, 0.0, 150.0, 175.0, 0.0]
    nx.part.DrawingSheets[0].View.AskVisibleObjects = lambda: [taken]
    boxes(nx, {int(taken.Tag): taken})
    second = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="linear",
                 first=nx.back.ref, orientation="horizontal")  # fmt: skip
    assert second.data["origin"] == pytest.approx([135.0, 180.0])  # past the other text
    explicit = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="linear",
                   first=nx.back.ref, orientation="horizontal", origin=[135, 140])  # fmt: skip
    assert explicit.data["origin"] == pytest.approx([135.0, 140.0])  # kept as given


def test_default_radius_text_leaves_the_view_on_the_side_of_the_hole():
    nx = dims_nx()
    wide_view(nx)
    nx.on_commit("RadialDimensionBuilder", dimension_hook(nx, 8.0))
    ok = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="radius", first=nx.hole.ref)
    assert ok.ok, ok
    assert ok.data["origin"] == pytest.approx([80.0, 125.0])  # left of the border, level


def test_kind_horizontal_and_vertical_are_oriented_linear_dimensions():
    nx = dims_nx()
    nx.on_commit("LinearDimensionBuilder", dimension_hook(nx, 70.0))
    ok = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="horizontal",
             first=nx.back.ref)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("LinearDimensionBuilder")
    assert b.Measurement.Method is b.Measurement.MeasurementMethod.Horizontal
    nx.on_commit("LinearDimensionBuilder", dimension_hook(nx, 50.0))
    vertical = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="vertical",
                   first=nx.front.ref, second=nx.back.ref)  # fmt: skip
    assert vertical.ok and vertical.data["dimension"]["expected"] == 50
    clash = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="horizontal",
                first=nx.back.ref, orientation="vertical")  # fmt: skip
    assert clash.error["field"] == "/orientation" and clash.mutation_outcome == "not_started"
