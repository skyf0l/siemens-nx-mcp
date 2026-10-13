"""nx_create_emboss and nx_create_offset_emboss on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.fake import Node
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def emboss_nx():
    """A modeling fake whose Emboss builder has one region, limit and target (as NX's) and
    whose face properties report each face's ``area``."""
    nx = modeling_nx()
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "EmbossBuilder":
            target, limit, region = Node("target"), Node("limit"), Node("region")
            limit.GetTargets = lambda: [target]
            region.GetLimits = lambda: [limit]
            b.GetRegions = lambda: [region]
            b.parts = (region, limit, target)
        return b

    nx.builder = builder

    def face_props(_area, _length, _accuracy, faces):
        return NS(Area=sum(getattr(f, "area", 0.0) for f in faces), Dispose=lambda: None)

    nx.part.MeasureManager.NewFaceProperties = face_props
    return nx


def embosses(nx, body, added):
    def commit(_b):
        body.volume += added
        return feature(nx, "EMBOSS", [body])

    nx.on_commit("EmbossBuilder", commit)


def test_pad_sets_the_region_limit_and_target_and_reads_the_volume_back():
    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    top = body.face((0, 0, 1))
    sk = nx.sketch(origin=(0, 0, 30))
    embosses(nx, body, 3534.29)
    ok = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[top.ref], height=5)
    assert ok.ok, ok
    b = nx.last("EmbossBuilder")
    region, limit, target = b.parts
    F = nx.nx.Features
    assert region.Convexity is F.EmbossRegion.ConvexityOption.Above
    assert region.Section.rules == [("CurveFeature", [sk.Feature])]
    assert target.TargetFace.rules == [("FaceDumb", [top])]
    assert limit.EndcapOption is F.EmbossLimit.EndcapMethod.OffsetExact
    assert limit.SetDistance.calls == [(("5.0",), {})]
    assert b.Tolerance == pytest.approx(0.01) and nx.live_builders == 0
    assert ok.data["added"] == pytest.approx(3534.29)
    assert ok.data["volume"] == pytest.approx(64000 + 3534.29)


def test_pocket_takes_a_negative_distance_and_must_remove_material():
    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    top = body.face((0, 0, 1))
    sk = nx.sketch(origin=(0, 0, 30))
    embosses(nx, body, -600)
    ok = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[top.ref], side="pocket", height=3,
             end_cap="translate")  # fmt: skip
    assert ok.ok, ok
    region, limit, _target = nx.last("EmbossBuilder").parts
    F = nx.nx.Features
    assert region.Convexity is F.EmbossRegion.ConvexityOption.Below
    assert limit.SetDistance.calls == [(("-(3.0)",), {})]
    assert limit.EndcapOption is F.EmbossLimit.EndcapMethod.Translation
    assert ok.data["added"] == pytest.approx(-600)
    embosses(nx, body, +600)  # a pocket that added material is refused
    bad = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[top.ref], side="pocket", height=3)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


def test_emboss_arguments_are_checked_first():
    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    other = nx.block(10, 10, 10, origin=(100, 0, 0))
    sk = nx.sketch()
    zero = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[body.face((0, 0, 1)).ref], height=0)
    assert zero.error["field"] == "/height" and zero.mutation_outcome == "not_started"
    two = run(nx, "nx_create_emboss", sketch=sk.ref, height=2,
              faces=[body.face((0, 0, 1)).ref, other.face((0, 0, 1)).ref])  # fmt: skip
    assert two.error["field"] == "/faces" and two.mutation_outcome == "not_started"
    body.IsSolidBody = False
    sheet = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[body.face((0, 0, 1)).ref], height=2)
    assert sheet.error["code"] == "NX_WRONG_GEOMETRY" and "offset_emboss" in sheet.error["hint"]
    assert not nx.builders


def sheet_nx():
    nx = emboss_nx()
    sheet = new_body(nx, [0, 0, 0, 100, 60, 0], volume=0.0)
    sheet.IsSolidBody, sheet.IsSheetBody = False, True
    sheet.faces = [nx.remember(nx.face(normal=(0, 0, 1), body=sheet))]
    sheet.faces[0].area = 6000.0
    sheet.ref = nx.ref_of(sheet, "body")
    path = nx.remember(nx.curve())
    return nx, sheet, path


def raises(nx, sheet, z=3.0, faces=6, area=211.93):
    def commit(_b):
        sheet.box = [0, 0, min(0.0, z), 100, 60, max(0.0, z)]
        extra = []
        for _ in range(faces - 1):
            f = nx.remember(nx.face(normal=(0, 0, 1), body=sheet))
            f.area = area / (faces - 1)
            extra.append(f)
        sheet.faces = sheet.faces[:1] + extra
        return feature(nx, "OFFSET EMBOSS", [sheet])

    nx.on_commit("OffsetEmbossBuilder", commit)


def test_offset_emboss_raises_a_strip_and_reports_area_and_bounds():
    nx, sheet, path = sheet_nx()
    raises(nx, sheet)
    ok = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[path.ref], height=3,
             left_width=5, side_offset=2)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("OffsetEmbossBuilder")
    assert b.HeightOffset.RightHandSide == "3.0" and b.SideOffset.RightHandSide == "2.0"
    assert b.LeftWidth.RightHandSide == "5.0" and b.RightWidth.RightHandSide == "5.0"
    assert b.ReverseDirection is False
    assert b.Type is nx.nx.Features.OffsetEmbossBuilder.Types.Curve
    assert b.Target.ReplaceRules.calls[0][0][0] == [("FaceDumb", [sheet.faces[0]])]
    assert b.CenterLine.AddToSection.calls
    assert ok.data["area_before"] == pytest.approx(6000)
    assert ok.data["area"] == pytest.approx(6211.93)
    assert ok.data["bounds"][1][2] == pytest.approx(3) and len(ok.data["bodies"]) == 1


def test_groove_and_right_width():
    nx, sheet, path = sheet_nx()
    raises(nx, sheet, z=-2.0)
    ok = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[path.ref], height=2,
             left_width=4, right_width=3, groove=True)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("OffsetEmbossBuilder")
    assert b.ReverseDirection is True and b.RightWidth.RightHandSide == "3.0"
    assert ok.data["bounds"][0][2] == pytest.approx(-2)


def test_an_unchanged_sheet_rolls_back():
    nx, sheet, path = sheet_nx()
    raises(nx, sheet, faces=1)
    bad = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[path.ref], height=2,
              left_width=4)  # fmt: skip
    assert bad.error["details"]["check"] == "area" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"height": 0}, "/height"),
        ({"left_width": -1}, "/left_width"),
        ({"right_width": 0}, "/right_width"),
        ({"side_offset": -1}, "/side_offset"),
    ],
)
def test_offset_emboss_arguments_are_checked_first(extra, field):
    nx, sheet, path = sheet_nx()
    args = {"sheet": sheet.ref, "path": [path.ref], "height": 2, "left_width": 4, **extra}
    bad = run(nx, "nx_create_offset_emboss", **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"


def test_offset_emboss_refuses_a_solid_and_a_repeated_path():
    nx, _sheet, path = sheet_nx()
    solid = nx.block(10, 10, 10)
    bad = run(nx, "nx_create_offset_emboss", sheet=solid.ref, path=[path.ref], height=2,
              left_width=4)  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "nx_create_emboss" in bad.error["hint"]
    twice = run(nx, "nx_create_offset_emboss", sheet=_sheet.ref, path=[path.ref, path.ref],
                height=2, left_width=4)  # fmt: skip
    assert twice.error["field"].startswith("/path") and twice.mutation_outcome == "not_started"
