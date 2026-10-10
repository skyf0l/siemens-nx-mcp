"""nx_create_sew, nx_create_thicken and nx_create_trim_sheet on the shared fake."""

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import curve, face, flat, freeform_nx, made, sheet
from tests.unit.modeling.fakes import new_body

# ---------------------------------------------------------------------------
# Sew


def two_sheets(nx):
    return sheet(nx, (0, 0, 0, 20, 10, 0)), sheet(nx, (20, 0, 0, 40, 10, 0))


def sew_commit(nx, target, *, unsewn=(), solid=False):
    def commit(b):
        b.GetUnsewnBodies = lambda: list(unsewn)
        target.box = [0, 0, 0, 40, 10, 10 if solid else 0]
        if solid:
            target.IsSolidBody, target.IsSheetBody, target.volume = True, False, 4000.0
        return made(nx, "SEW", [target])

    nx.on_commit("SewBuilder", commit)


def test_sew_sheets_with_scaled_default_tolerance():
    nx = freeform_nx(units="in")
    a, b = two_sheets(nx)
    sew_commit(nx, a)
    ok = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref])
    assert ok.ok, ok
    sb = nx.last("SewBuilder")
    assert sb.Tolerance == pytest.approx(0.001 / 25.4)
    assert sb.Type is nx.nx.Features.SewBuilder.Types.Sheet
    assert sb.BodyPreference is nx.nx.Features.SewBuilder.BodyPreferenceTypes.Sheet
    assert ok.data["solid"] is False and ok.data["volume"] is None
    assert ok.data["bounds"]["max"] == [40, 10, 0] and nx.live_builders == 0


def test_sew_solid_and_incomplete_results():
    nx = freeform_nx()
    a, b = two_sheets(nx)
    sew_commit(nx, a, solid=True)
    ok = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref], solid=True, tolerance=0.01)
    assert ok.ok and ok.data["solid"] is True and ok.data["volume"] == 4000.0, ok
    assert nx.last("SewBuilder").BodyPreference is (
        nx.nx.Features.SewBuilder.BodyPreferenceTypes.Solid
    )

    nx = freeform_nx()
    a, b = two_sheets(nx)
    sew_commit(nx, a)
    sheet_only = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref], solid=True)
    assert sheet_only.error["code"] == "NX_INCOMPLETE_RESULT"
    assert sheet_only.mutation_outcome == "rolled_back"

    nx = freeform_nx()
    a, b = two_sheets(nx)
    sew_commit(nx, a, unsewn=[b])
    partial = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref])
    assert partial.error["code"] == "NX_INCOMPLETE_RESULT" and nx.live_builders == 0


def test_sew_rejections_and_native_refusal():
    nx = freeform_nx()
    a, b = two_sheets(nx)
    itself = run(nx, "nx_create_sew", target=a.ref, tools=[a.ref])
    assert itself.error["field"] == "/tools" and itself.mutation_outcome == "not_started"
    zero = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref], tolerance=0)
    assert zero.error["field"] == "/tolerance"
    block = nx.block(5, 5, 5, origin=(100, 0, 0))
    solid = run(nx, "nx_create_sew", target=a.ref, tools=[block.ref])
    assert solid.error["code"] == "NX_WRONG_GEOMETRY" and solid.error["field"] == "/tools/0"
    nx.refuse("SewBuilder", 4635012, "Sheets do not touch")
    bad = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref])
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 4635012


# ---------------------------------------------------------------------------
# Thicken


def thick_nx(units="mm", volume=None):
    nx = freeform_nx(units=units)
    top = face(nx, flat(), area=200.0)
    sheet(nx, (0, 0, 0, 20, 10, 0), [top])

    def commit(b):
        thickness = abs(float(b.FirstOffset.RightHandSide) - float(b.SecondOffset.RightHandSide))
        body = new_body(nx, [0, 0, 0, 20, 10, thickness])
        body.volume = volume if volume is not None else 200.0 * thickness
        return made(nx, "THICKEN", [body])

    nx.on_commit("ThickenBuilder", commit)
    return nx, top


def test_thicken_offsets_and_planar_volume_readback():
    nx, top = thick_nx()
    ok = run(nx, "nx_create_thicken", faces=[top.ref], offset1=2, offset2=-1)
    assert ok.ok and ok.data["volume"] == 600.0, ok
    b = nx.last("ThickenBuilder")
    assert b.FirstOffset.RightHandSide == "2.0" and b.SecondOffset.RightHandSide == "-1.0"
    assert b.Tolerance == 0.001 and b.FaceCollector.ReplaceRules.calls[0][0][0] == [
        ("FaceDumb", [top])
    ]


def test_thicken_converts_units_and_checks_volume():
    nx, top = thick_nx(units="in", volume=200.0 * 0.1 * 25.4)  # fake mm^2 area, mm^3 volume
    ok = run(nx, "nx_create_thicken", faces=[top.ref], offset1="2.54 mm")
    assert ok.ok, ok
    assert nx.last("ThickenBuilder").FirstOffset.RightHandSide == repr(0.1)
    assert nx.last("ThickenBuilder").Tolerance == pytest.approx(0.001 / 25.4)
    nx, top = thick_nx(volume=123.0)
    bad = run(nx, "nx_create_thicken", faces=[top.ref], offset1=2)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


def test_thicken_rejects_equal_offsets_before_nx():
    nx, top = thick_nx()
    bad = run(nx, "nx_create_thicken", faces=[top.ref], offset1=1, offset2=1)
    assert bad.error["field"] == "/offset2" and not nx.builders
    nx.refuse("ThickenBuilder", 670015)
    native = run(nx, "nx_create_thicken", faces=[top.ref], offset1=1)
    assert native.error["code"] == "NX_BUILD_FAILED" and nx.live_builders == 0


# ---------------------------------------------------------------------------
# Trim sheet


def trim_nx(kept_box):
    nx = freeform_nx()
    target = sheet(nx, (0, 0, 0, 20, 10, 0))
    cut = curve(nx, [10, -1, 0], [10, 11, 0])

    def commit(b):
        target.box = list(kept_box)
        return made(nx, "TRIM_SHEET", [target])

    nx.on_commit("TrimsheetBuilder", commit)
    return nx, target, cut


def test_trim_keeps_the_region_point_side():
    nx, target, cut = trim_nx((0, 0, 0, 10, 10, 0))
    ok = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref],
             region_point=[5, 5, 0])  # fmt: skip
    assert ok.ok and ok.data["bounds"]["max"] == [10, 10, 0], ok
    b = nx.last("TrimsheetBuilder")
    assert b.KeepDiscardMethod is nx.nx.Features.TrimSheetBuilder.KeepDiscardOption.Keep
    assert b.TargetBodies.Add.calls[0][0] == (target,) and b.OutputExactGeometry is True
    section = b.BoundaryObjects.Add.calls[0][0][0]
    assert section.rules == [("CurveDumb", [cut])]
    region = b.Regions.Append.calls[0][0][0]
    assert region.body is target and region.point.Coordinates.X == 5


def test_trim_discard_and_wrong_side_readback():
    nx, target, cut = trim_nx((10, 0, 0, 20, 10, 0))
    ok = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref],
             region_point=[5, 5, 0], keep=False)  # fmt: skip
    assert ok.ok and ok.data["bounds"]["min"] == [10, 0, 0], ok
    assert nx.last("TrimsheetBuilder").KeepDiscardMethod is (
        nx.nx.Features.TrimSheetBuilder.KeepDiscardOption.Discard
    )
    nx, target, cut = trim_nx((10, 0, 0, 20, 10, 0))
    bad = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref],
              region_point=[5, 5, 0])  # fmt: skip
    assert bad.error["details"]["check"] == "kept_region" and bad.mutation_outcome == "rolled_back"


def test_trim_rejections_and_native_refusal():
    nx, target, cut = trim_nx((0, 0, 0, 10, 10, 0))
    block = nx.block(5, 5, 5, origin=(100, 0, 0))
    solid = run(nx, "nx_create_trim_sheet", body=block.ref, boundaries=[cut.ref],
                region_point=[5, 5, 0])  # fmt: skip
    assert solid.error["code"] == "NX_WRONG_GEOMETRY" and solid.error["field"] == "/body"
    twice = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref, cut.ref],
                region_point=[5, 5, 0])  # fmt: skip
    assert twice.error["category"] == "invalid_argument" and not nx.builders
    nx.refuse("TrimsheetBuilder", 671228, "Boundary does not intersect the sheet")
    bad = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref],
              region_point=[5, 5, 0])  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 671228
    assert nx.live_builders == 0


# ---------------------------------------------------------------------------
# Diagnostics, thickness and projection


def test_sew_refusal_reports_each_tool_gap():
    nx = freeform_nx()
    a = sheet(nx, (0, 0, 0, 20, 10, 0))
    near = sheet(nx, (20, 0, 0, 40, 10, 0))
    far = sheet(nx, (43, 0, 0, 60, 10, 0))
    nx.refuse("SewBuilder", 4635012, "Tool body does not share a common boundary")
    bad = run(nx, "nx_create_sew", target=a.ref, tools=[near.ref, far.ref])
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 4635012
    gaps = bad.error["details"]["gaps"]
    assert gaps == [{"tool": near.ref, "gap": 0.0}, {"tool": far.ref, "gap": 3.0}]
    assert far.ref in bad.error["hint"] and "tolerance" in bad.error["hint"]
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


def test_unsewn_tools_report_gaps_and_success_reports_the_sheet():
    nx = freeform_nx()
    a, b = two_sheets(nx)
    sew_commit(nx, a, unsewn=[b])
    partial = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref])
    assert partial.error["code"] == "NX_INCOMPLETE_RESULT"
    assert partial.error["details"]["unsewn"] == 1
    assert partial.error["details"]["gaps"] == [{"tool": b.ref, "gap": 0.0}]
    nx = freeform_nx()
    a, b = two_sheets(nx)
    sew_commit(nx, a)
    ok = run(nx, "nx_create_sew", target=a.ref, tools=[b.ref], tolerance="0.01mm")
    assert ok.ok and ok.data["sheet"] is True and ok.data["faces"] == 0, ok
    assert nx.last("SewBuilder").Tolerance == pytest.approx(0.01)


@pytest.mark.parametrize(
    ("args", "offsets", "side"),
    [
        ({"thickness": 2}, ("2.0", "0.0"), "along_normal"),
        ({"thickness": "-2 mm"}, ("-2.0", "0.0"), "against_normal"),
        ({"offset1": 1, "offset2": -1}, ("1.0", "-1.0"), "both_sides"),
        ({"offset1": 2}, ("2.0", "0.0"), "along_normal"),
    ],
)
def test_thicken_thickness_shortcut_and_side(args, offsets, side):
    nx, top = thick_nx()
    ok = run(nx, "nx_create_thicken", faces=[top.ref], **args)
    assert ok.ok and ok.data["side"] == side and ok.data["volume"] == 400.0, ok
    b = nx.last("ThickenBuilder")
    assert (b.FirstOffset.RightHandSide, b.SecondOffset.RightHandSide) == offsets
    assert ok.data["sheet"] is False and ok.data["faces"] == 1


def test_thicken_needs_one_way_of_giving_the_thickness():
    nx, top = thick_nx()
    none = run(nx, "nx_create_thicken", faces=[top.ref])
    assert none.error["field"] == "/thickness" and none.mutation_outcome == "not_started"
    assert "thickness" in none.error["hint"]
    both = run(nx, "nx_create_thicken", faces=[top.ref], thickness=2, offset1=2)
    assert both.error["field"] == "/thickness"
    zero = run(nx, "nx_create_thicken", faces=[top.ref], thickness=0)
    assert zero.error["field"] == "/thickness" and not nx.builders


def test_trim_projects_along_a_direction_and_snaps_the_region_point():
    nx, target, cut = trim_nx((0, 0, 0, 10, 10, 0))
    ok = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref],
             region_point=[5, 5, 3], direction=[0, 0, 2])  # fmt: skip
    assert ok.ok and ok.data["sheet"] is True, ok
    b = nx.last("TrimsheetBuilder")
    methods = nx.nx.GeometricUtilities.ProjectionOptions.DirectionType
    assert b.ProjectionDirection.ProjectDirectionMethod is methods.Vector
    assert b.ProjectionDirection.ProjectVector is not None
    region = b.Regions.Append.calls[0][0][0]
    assert [region.point.Coordinates.X, region.point.Coordinates.Z] == [5, 0]  # on the sheet
    zero = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[cut.ref],
               region_point=[5, 5, 0], direction=[0, 0, 0])  # fmt: skip
    assert zero.error["field"] == "/direction" and zero.mutation_outcome == "not_started"


def test_trim_default_projection_is_face_normal_and_refusal_explains():
    nx, target, _cut = trim_nx((0, 0, 0, 10, 10, 0))
    below = curve(nx, [10, -1, -4], [10, 11, -4])
    nx.refuse("TrimsheetBuilder", 671232, "Invalid retained/discarded regions")
    bad = run(nx, "nx_create_trim_sheet", body=target.ref, boundaries=[below.ref],
              region_point=[5, 5, 2])  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 671232
    details = bad.error["details"]
    assert details["region_gap"] == 2.0 and details["projection"] == "face_normal"
    assert details["boundary_gaps"] == [{"boundary": below.ref, "gap": 4.0}]
    assert "direction" in bad.error["hint"] and below.ref in bad.error["hint"]
    assert not nx.last("TrimsheetBuilder").ProjectionDirection.ProjectDirectionMethod.calls
