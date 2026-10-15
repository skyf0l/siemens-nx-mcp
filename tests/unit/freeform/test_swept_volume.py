"""nx_create_swept_volume on the shared fake."""

from types import SimpleNamespace as NS

import pytest

from tests.unit.freeform.fakes import curve, face, flat, freeform_nx, sheet
from tests.unit.modeling.fakes import call, feature, new_body


def scene(volume_after=7762.301):
    """A d4 cylinder tool, a 70 x 20 x 6 block and a 50 line; the commit sets the block's
    volume (NX's CommitFeature returns None; the feature is the part's new one)."""
    nx = freeform_nx()
    nx.part.Features.SweepFeatureCollection = nx.part.Features
    tool = new_body(nx, [-2, 48, 0, 2, 52, 6], volume=75.398)
    side = face(nx, flat(), kind="cylinder", body=tool)
    nx.face_data[side.Tag] = ("cylinder", [0, 50, 0], [0, 0, 1])  # the tool's axis
    tool.GetFaces = lambda: [side]
    block = new_body(nx, [-10, 40, -3, 60, 60, 3])
    path = curve(nx, [0, 50, 0], [50, 50, 0])

    def commit(_b):
        block.volume = volume_after
        feature(nx, "Swept Volume", [block])
        return None

    nx.on_commit("SweptVolumeBuilder", commit)
    return nx, tool, block, path


def refs(nx, *objs):
    return [nx.ref_of(o, "body") for o in objs]


def test_swept_volume_subtracts_from_the_targets():
    nx, tool, block, path = scene()
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    ok = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert ok.ok, ok
    b = nx.last("SweptVolumeBuilder")
    G = nx.nx.GeometricUtilities.BooleanOperation.BooleanType
    assert b.ToolBody.rules == [("BodyDumb", [tool])]
    assert b.BooleanData.Type is G.Subtract
    assert b.BooleanData.SetTargetBodies.calls[0][0][0] == [block]
    assert b.DistanceTolerance == pytest.approx(0.01)
    assert nx.license_queries == ["nx_freeform_1"]
    data = ok.data
    assert data["volume_before"] == pytest.approx(8400) and data["volume"] == pytest.approx(
        7762.301
    )
    assert data["swept"] == pytest.approx(637.699) and data["operation"] == "subtract"
    assert data["feature"]["name"].startswith("Swept Volume") and nx.live_builders == 0


def test_swept_volume_intersect_keeps_the_envelope():
    nx, tool, block, path = scene(volume_after=637.699)
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    ok = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target],
              "operation": "intersect", "tolerance": "0.02 mm"})  # fmt: skip
    assert ok.ok, ok
    b = nx.last("SweptVolumeBuilder")
    assert b.BooleanData.Type is nx.nx.GeometricUtilities.BooleanOperation.BooleanType.Intersect
    assert b.DistanceTolerance == pytest.approx(0.02)
    assert ok.data["swept"] == pytest.approx(637.699)


def test_swept_volume_that_misses_the_targets_rolls_back():
    nx, tool, block, path = scene(volume_after=8400.0)
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    bad = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert bad.error["details"]["check"] == "volume", bad
    assert bad.mutation_outcome == "rolled_back"


def test_swept_volume_refusal_gets_a_hint():
    nx, tool, block, path = scene()
    nx.refuse("SweptVolumeBuilder", 3505014, "Swept Volume feature cannot be created.")
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    bad = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 3505014
    assert "cylinder, revolve" in bad.error["hint"]


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"targets": ["@tool"]}, "/targets"),
        ({"tool": "@sheet"}, "/tool"),
        ({"targets": ["@sheet"]}, "/targets/0"),
        ({"tolerance": 0.0001}, "/tolerance"),
        ({"tolerance": "0.001 mm"}, "/tolerance"),
        ({"path": ["@path", "@path"]}, "/path"),
    ],
)
def test_swept_volume_rejections_before_nx(args, field):
    nx, tool, block, path = scene()
    flat = sheet(nx, (0, 0, 0, 10, 10, 0))
    names = {"@tool": nx.ref_of(tool, "body"), "@sheet": flat.ref, "@path": path.ref}
    given = {"tool": "@tool", "path": ["@path"], "targets": [nx.ref_of(block, "body")], **args}

    def put(value):
        if isinstance(value, list):
            return [names.get(v, v) for v in value]
        return names.get(value, value)

    bad = call(nx, "nx_create_swept_volume", {k: put(v) for k, v in given.items()})
    assert bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_swept_volume_without_license_refuses_before_the_builder():
    nx, tool, block, path = scene()
    nx.licenses["nx_freeform_1"] = False
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    bad = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and not nx.builders


@pytest.mark.parametrize("given", [0.001, "0.009 mm", "0.0001 in"])
def test_swept_volume_refuses_a_tolerance_below_0_01_mm_before_nx(given):
    nx, tool, block, path = scene()
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    args = {"tool": t, "path": [path.ref], "targets": [target], "tolerance": given}
    bad = call(nx, "nx_create_swept_volume", args)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/tolerance"
    assert "0.01 mm" in bad.error["message"] and "0.01 mm" in bad.error["hint"], bad
    assert bad.mutation_outcome == "not_started" and not nx.builders
    assert nx.license_queries == []  # refused before NX is asked for anything


def test_swept_volume_tolerance_floor_is_the_part_distance_tolerance_when_larger():
    nx, tool, block, path = scene()
    nx.part.Preferences = NS(Modeling=NS(DistanceToleranceData=0.0254))
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    base = {"tool": t, "path": [path.ref], "targets": [target]}
    bad = call(nx, "nx_create_swept_volume", {**base, "tolerance": 0.02})
    assert bad.error["field"] == "/tolerance" and "0.0254 mm" in bad.error["message"], bad
    ok = call(nx, "nx_create_swept_volume", base)  # the default follows the floor
    assert ok.ok, ok
    assert nx.last("SweptVolumeBuilder").DistanceTolerance == pytest.approx(0.0254)


@pytest.mark.parametrize(
    ("start", "end", "lead"),
    [([-10, 50, 20], [70, 50, 20], -10.0), ([20, 25, 20], [50, 25, 20], 20.0)],
)
def test_swept_volume_refuses_a_path_not_level_with_the_tool_axis(start, end, lead):
    nx, tool, block, _path = scene()
    path = curve(nx, start, end)
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    bad = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert bad.error["field"] == "/path/0" and bad.mutation_outcome == "not_started", bad
    assert bad.error["details"]["lead"] == pytest.approx(lead)
    assert bad.error["details"]["shift"] == pytest.approx([-lead, 0, 0])
    assert not nx.builders


def test_swept_volume_path_beside_the_axis_but_level_is_accepted():
    nx, tool, block, _path = scene()
    path = curve(nx, [0, 150, 20], [50, 150, 20])  # 100 off sideways, level with the axis
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    ok = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert ok.ok, ok.error


@pytest.mark.parametrize(("code", "word"), [(4120009, "level"), (4120021, "crosses")])
def test_swept_volume_maps_nx_refusals_to_hints(code, word):
    nx, tool, block, path = scene()
    nx.refuse("SweptVolumeBuilder", code, "refused")
    (t,), (target,) = refs(nx, tool), refs(nx, block)
    bad = call(nx, "nx_create_swept_volume", {"tool": t, "path": [path.ref], "targets": [target]})
    assert bad.error["nx_code"] == code and word in bad.error["hint"], bad.error
