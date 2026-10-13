"""Sheet borders, zones and margins (nx_set_sheet_borders) on the fake."""

import math
from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import drawing_nx

DEFAULTS = {
    "border": {"CreateBorders": True, "BorderLineWidth": 5.0},
    "zone": {"CreateZones": True, "CreateZoneMarkings": True, "CreateZoneLabels": True,
             "HorizontalSize": 50.0, "VerticalSize": 50.0, "Origin": "BottomRight"},
    "margin": {"LeftTrimmedMargin": 15.0, "BottomTrimmedMargin": 5.0,
               "RightTrimmedMargin": 5.0, "TopTrimmedMargin": 5.0},
}  # fmt: skip


class Line:
    def __init__(self, a, b):
        self.StartPoint, self.EndPoint = point(*a, 0.0), point(*b, 0.0)


class Note(Object):
    def __init__(self, text):
        super().__init__("NOTE")
        self.text = text

    def GetText(self):
        return [self.text]


def frame(x0, y0, x1, y1):
    return [Line((x0, y0), (x1, y0)), Line((x1, y0), (x1, y1)), Line((x1, y1), (x0, y1)),
            Line((x0, y1), (x0, y0))]  # fmt: skip


def style(values):
    return NS(
        SheetBorderSettingsStyle=NS(**values["border"]),
        SheetZoneSettingsStyle=NS(**values["zone"]),
        SheetMarginSettingsStyle=NS(**values["margin"]),
    )


def borders_nx(draw=True):
    """An A3 sheet; NX draws the frames at the margins and even zone counts (``draw``
    False: NX draws nothing)."""
    nx = drawing_nx()
    sheet = nx.sheet_obj = nx.make_sheet()
    nx.nx.Drawings.ZoneOrigin = NS(BottomRight="BottomRight", BottomLeft="BottomLeft",
                                   TopLeft="TopLeft", TopRight="TopRight")  # fmt: skip
    nx.notes = [Note("1")]  # a user note that looks like a label
    shown = []
    sheet.View.AskVisibleObjects = lambda: list(shown) + nx.notes
    stored = {}

    def factory(existing):
        b = nx.builder("BordersAndZonesBuilder", existing)
        values = stored.get(int(existing.Tag)) if isinstance(existing, Object) else None
        b.BorderAndZoneStyle = style(values or {k: dict(v) for k, v in DEFAULTS.items()})
        return b

    def commit(b):
        s = b.BorderAndZoneStyle
        values = {
            "border": vars(s.SheetBorderSettingsStyle).copy(),
            "zone": vars(s.SheetZoneSettingsStyle).copy(),
            "margin": vars(s.SheetMarginSettingsStyle).copy(),
        }
        obj = sheet.__dict__.get("BordersAndZones") or Object("BORDERS")
        stored[int(obj.Tag)] = values
        sheet.BordersAndZones = obj
        shown.clear()
        if not draw:
            return obj
        m, w = values["margin"], values["border"]["BorderLineWidth"]
        outer = [m["LeftTrimmedMargin"], m["BottomTrimmedMargin"],
                 420 - m["RightTrimmedMargin"], 297 - m["TopTrimmedMargin"]]  # fmt: skip
        if values["border"]["CreateBorders"]:
            shown.extend(frame(*outer) + frame(outer[0] + w, outer[1] + w, outer[2] - w,
                                               outer[3] - w))  # fmt: skip
        if values["zone"]["CreateZones"]:
            z = values["zone"]
            columns = 2 * round((outer[2] - outer[0]) / z["HorizontalSize"] / 2)
            rows = 2 * round((outer[3] - outer[1]) / z["VerticalSize"] / 2)
            shown.extend(Note(str(i + 1)) for i in range(columns) for _ in (0, 1))
            shown.extend(Note("ABCDEFGHJKLMN"[i]) for i in range(rows) for _ in (0, 1))
        return obj

    nx.on_commit("BordersAndZonesBuilder", commit)
    nx.part.Drafting.BordersAndZonesObjects = NS(CreateBordersAndZonesBuilder=factory)
    return nx


def test_default_borders_read_back_frames_and_zone_labels():
    nx = borders_nx()
    nx.sheet_obj.Open()
    ok = run(nx, "nx_set_sheet_borders")
    assert ok.ok, ok
    assert ok.data["outer"] == pytest.approx([15, 5, 415, 292])
    assert ok.data["inner"] == pytest.approx([20, 10, 410, 287])
    assert ok.data["columns"] == [str(i) for i in range(1, 9)] and ok.data["rows"] == list("ABCDEF")
    assert ok.data["replaced"] is False and nx.live_builders == 0


def test_settings_replace_the_existing_borders():
    nx = borders_nx()
    nx.sheet_obj.Open()
    assert run(nx, "nx_set_sheet_borders").ok
    ok = run(nx, "nx_set_sheet_borders", sheet="sht1", margins=[20, 10, 10, 10],
             zone_size=[70, 60], zone_origin="top_left", border_width=6)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["replaced"] is True
    assert ok.data["outer"] == pytest.approx([20, 10, 410, 287])
    assert ok.data["inner"] == pytest.approx([26, 16, 404, 281])
    assert ok.data["zone_origin"] == "top_left" and ok.data["zone_size"] == pytest.approx([70, 60])
    assert len(ok.data["columns"]) == 2 * round(390 / 70 / 2)
    edited = [b for b in nx.builders if b.kind == "BordersAndZonesBuilder"][2]
    assert edited.args[0] is nx.sheet_obj.BordersAndZones  # the existing one is edited
    off = run(nx, "nx_set_sheet_borders", zones=False)
    assert off.ok and off.data["columns"] == [] and off.data["zones"] is False


def test_bad_values_are_refused_before_nx():
    nx = borders_nx()
    nx.sheet_obj.Open()
    for args, field in [
        ({"margins": [200, 10, 230, 10]}, "/margins/0"),
        ({"margins": [10, 148.5, 10, 148.5]}, "/margins"),
        ({"zone_size": [0, 50]}, "/zone_size/0"),
        ({"border_width": 80}, "/border_width"),
        ({"sheet": "NOPE"}, "/sheet"),
    ]:
        bad = run(nx, "nx_set_sheet_borders", **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (args, bad)
    assert not nx.builders
    assert math.isclose(nx.sheet_obj.Length, 420)


def test_a_frame_nx_did_not_draw_fails_the_read_back():
    nx = borders_nx(draw=False)
    nx.sheet_obj.Open()
    bad = run(nx, "nx_set_sheet_borders")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
