"""nx_create_primitive (block, cylinder, cone, sphere) on the shared fake, real kernel."""

import math
from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body

MM = NS(Name="MilliMeter", Abbreviation="mm")


def made(nx, box, volume=None, centroid=None):
    """Commit hook: the builder makes one new body."""

    def hook(b):
        body = new_body(nx, box, volume)
        if centroid is not None:
            body.centroid = centroid
        return feature(nx, b.kind.replace("Builder", "").upper(), [body])

    return hook


def test_block_at_a_corner_drives_its_sizes_and_reads_back():
    nx = modeling_nx()
    e = nx.expression("thickness", "10", 10.0)
    e.Units = MM
    nx.on_commit("BlockFeatureBuilder", made(nx, [5, 0, 0, 45, 20, 10]))
    ok = run(nx, "nx_create_primitive", length=40, width=20, height="thickness", origin=[5, 0, 0])
    assert ok.ok, ok
    b = nx.last("BlockFeatureBuilder")
    corner, *sizes = b.SetOriginAndLengths.calls[0][0]
    assert (corner.X, corner.Y, corner.Z) == (5.0, 0.0, 0.0)
    assert sizes == ["40.0", "20.0", "thickness"]  # the expression keeps driving the height
    x, y = b.SetOrientation.calls[0][0]
    assert (x.X, x.Y, x.Z, y.X, y.Y, y.Z) == (1, 0, 0, 0, 1, 0)
    types = nx.nx.GeometricUtilities.BooleanOperation.BooleanType
    assert b.BooleanOption.Type is types.Create
    assert ok.data["volume"] == pytest.approx(8000) and len(ok.data["bodies"]) == 1
    assert nx.live_builders == 0


def test_centered_block_and_a_wrong_volume_rolls_back():
    nx = modeling_nx()
    nx.on_commit("BlockFeatureBuilder", made(nx, [-20, -10, -5, 20, 10, 5]))
    ok = run(nx, "nx_create_primitive", length=40, width=20, height=10, centered=True)
    assert ok.ok, ok
    corner = nx.last("BlockFeatureBuilder").SetOriginAndLengths.calls[0][0][0]
    assert (corner.X, corner.Y, corner.Z) == (-20.0, -10.0, -5.0)
    nx2 = modeling_nx()
    nx2.on_commit("BlockFeatureBuilder", made(nx2, [0, 0, 0, 40, 20, 5]))
    bad = run(nx2, "nx_create_primitive", length=40, width=20, height=10)
    assert (
        bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["details"]["check"] == "volume"
    )
    assert bad.mutation_outcome == "rolled_back" and list(nx2.part.Bodies) == []


def test_cylinder_along_an_axis_with_a_centroid_check():
    nx = modeling_nx()
    volume = math.pi * 25 * 30
    nx.on_commit(
        "CylinderBuilder",
        made(nx, [-5, -5, 0, 5, 5, 30], volume=volume, centroid=[0, 0, 15]),
    )
    ok = run(nx, "nx_create_primitive", shape="cylinder", diameter=10, height=30)
    assert ok.ok, ok
    b = nx.last("CylinderBuilder")
    assert b.Diameter.RightHandSide == "10.0" and b.Height.RightHandSide == "30.0"
    # The centroid must sit half way up the axis: a cylinder built along -z fails.
    nx2 = modeling_nx()
    nx2.on_commit(
        "CylinderBuilder",
        made(nx2, [-5, -5, -30, 5, 5, 0], volume=volume, centroid=[0, 0, -15]),
    )
    bad = run(nx2, "nx_create_primitive", shape="cylinder", diameter=10, height=30)
    assert bad.error["details"]["check"] == "centroid", bad


def test_cone_frustum_and_apex_and_sphere():
    nx = modeling_nx()
    big, small, h = 10.0, 5.0, 12.0
    volume = math.pi * h * (big * big + big * small + small * small) / 3
    rise = (
        h
        * (big * big + 2 * big * small + 3 * small * small)
        / (4 * (big * big + big * small + small * small))
    )
    nx.on_commit(
        "ConeBuilder", made(nx, [-10, -10, 0, 10, 10, 12], volume=volume, centroid=[0, 0, rise])
    )
    ok = run(nx, "nx_create_primitive", shape="cone", diameter=20, top_diameter=10, height=12)
    assert ok.ok, ok
    b = nx.last("ConeBuilder")
    assert (b.BaseDiameter.RightHandSide, b.TopDiameter.RightHandSide) == ("20.0", "10.0")
    apex = math.pi * 12 * 100 / 3
    nx.on_commit(
        "ConeBuilder", made(nx, [-10, -10, 0, 10, 10, 12], volume=apex, centroid=[0, 0, 3])
    )
    ok = run(nx, "nx_create_primitive", shape="cone", diameter=20, height=12)
    assert ok.ok, ok
    assert nx.last("ConeBuilder").TopDiameter.RightHandSide == "0.0"
    nx.on_commit(
        "SphereBuilder",
        made(nx, [4, -6, -6, 16, 6, 6], volume=math.pi * 12**3 / 6, centroid=[10, 0, 0]),
    )
    ok = run(nx, "nx_create_primitive", shape="sphere", diameter=12, origin=[10, 0, 0])
    assert ok.ok, ok
    assert nx.last("SphereBuilder").Diameter.RightHandSide == "12.0"


def test_combined_block_subtracts_from_a_target():
    nx = modeling_nx()
    plate = nx.block(40, 20, 10)

    def cut(b):
        plate.volume = 7000
        return feature(nx, "BLOCK", [plate])

    nx.on_commit("BlockFeatureBuilder", cut)
    ok = run(
        nx, "nx_create_primitive", length=10, width=10, height=10, origin=[0, 0, 0],
        combine={"operation": "subtract", "targets": [plate.ref]},
    )  # fmt: skip
    assert ok.ok, ok
    b = nx.last("BlockFeatureBuilder")
    types = nx.nx.GeometricUtilities.BooleanOperation.BooleanType
    assert b.BooleanOption.Type is types.Subtract
    assert b.BooleanOption.SetTargetBodies.calls[0][0][0] == [plate]
    assert ok.data["volume"] == 7000


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"length": 1, "width": 1}, "/height"),
        ({"length": 1, "width": 1, "height": 1, "diameter": 2}, "/diameter"),
        ({"shape": "sphere"}, "/diameter"),
        ({"shape": "cylinder", "diameter": 2, "height": 0}, "/height"),
        ({"shape": "cone", "diameter": 2, "top_diameter": 2, "height": 1}, "/top_diameter"),
        ({"shape": "sphere", "diameter": 2, "centered": True}, "/centered"),
        (
            {"shape": "cylinder", "diameter": 2, "height": 1, "x_direction": [1, 0, 0]},
            "/x_direction",
        ),
        ({"length": 1, "width": 1, "height": 1, "x_direction": [0, 0, 1]}, "/x_direction"),
        ({"length": "3 deg", "width": 1, "height": 1}, "/length"),
    ],
)
def test_argument_errors_are_not_started(args, field):
    nx = modeling_nx()
    bad = run(nx, "nx_create_primitive", **args)
    assert bad.error["category"] == "invalid_argument", bad
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"
    assert nx.builders == []
