"""Builder arguments, coordinate frames and transaction contracts, not kernel tests."""

from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Curve, Feature, Sketch, point

pytestmark = pytest.mark.fake_nx


def setup_sketch_builder(rig, mismatch=False, fail=False):
    part = rig.part
    built = []
    part.CoordinateSystems = NS(CreateCoordinateSystem=lambda p, m, _: NS(Origin=p, Orientation=m))

    def builder(_):
        b = NS(Destroy=Mock())

        def commit():
            if fail:
                raise RuntimeError("builder failed")
            sk = Sketch(rig.session)
            sk.Origin = b.Csystem.Origin
            sk.Orientation.Element = b.Csystem.Orientation
            if mismatch:
                sk.Origin = point(99, 99, 99)
            part.Sketches.append(sk)
            return sk

        b.Commit = commit
        built.append(b)
        return b

    part.Sketches.CreateSketchInPlaceBuilder2 = builder

    def line(start, end):
        c = Curve()
        c.StartPoint = start
        c.EndPoint = end
        part.Curves.append(c)
        return c

    def arc(center, x, y, r, start, end):
        c = Curve()
        c.CenterPoint = center
        c.Radius = r
        part.Curves.append(c)
        return c

    part.Curves.CreateLine = line
    part.Curves.CreateArc = arc
    return built


def feature_builders(rig):
    f = Feature()
    rig.part.Features.append(f)

    def expr():
        return NS(RightHandSide="")

    spacing = NS(NCopies=expr(), PitchDistance=expr())
    b = NS(
        Limits=NS(EndExtend=NS(Value=expr())),
        PatternService=NS(RectangularDefinition=NS(XSpacing=spacing, YSpacing=NS(NCopies=expr()))),
        FeatureList=NS(Add=Mock()),
        CommitFeature=Mock(return_value=f),
        Destroy=Mock(),
    )
    rig.part.Features.CreateExtrudeBuilder = lambda _: b
    rig.part.Features.CreatePatternFeatureBuilder = lambda _: b
    rig.nx.Features.PatternFeatureBuilder = NS(PatternMethodOptions=NS(Simple="simple"))
    rig.nx.Features.Feature.Null = None
    rig.nx.GeometricUtilities = NS(PatternDefinition=NS(PatternEnum=NS(Linear="linear")))
    import sys

    sys.modules["NXOpen.GeometricUtilities"] = rig.nx.GeometricUtilities
    rig.nx.SmartObject = NS(UpdateOption=NS(WithinModeling="model"))
    rig.part.Directions = NS(CreateDirection=lambda p, v, _: v)
    rig.e._active_mark = 1
    return f, b


@pytest.mark.parametrize("rotation", [[], [[1, 0, 0]] * 3, [[1, 0, 0], [0, 1, 0], [0, 0, -1]]])
def test_rotation_rejects_scale_shear_or_reflection(rig, rotation):
    with pytest.raises(NXToolError):
        rig.e._validate_rotation(rotation)


@pytest.mark.parametrize(
    ("method", "params"),
    [("nx_save_part", {}), ("nx_save_part", {"path": "copy.prt"}), ("nx_close_part", {})],
)
def test_save_and_close_refuse_parts_outside_the_workspace(
    rig, tmp_path, method, params, monkeypatch
):
    from nx_mcp.core import kernel
    from nx_mcp.host.dispatch import KernelError

    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    outside = tmp_path.parent / f"{tmp_path.name}-engineer" / "bracket.prt"
    rig.part.FullPath = str(outside)
    rig.part.Save = Mock()
    rig.part.SaveAs = Mock()
    rig.part.Close = Mock()
    with pytest.raises(KernelError) as error:
        rig.e.execute(method, params)
    assert error.value.code == "NX_PATH_OUTSIDE_WORKSPACE"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert error.value.envelope["error"]["details"]["path"] == str(outside)
    rig.part.Save.assert_not_called()
    rig.part.SaveAs.assert_not_called()
    rig.part.Close.assert_not_called()
    assert not outside.exists() and not (tmp_path / "copy.prt").exists()


def test_unsaved_part_without_a_file_may_only_be_saved_as_into_the_workspace(
    rig, tmp_path, monkeypatch
):
    from nx_mcp.core import kernel
    from nx_mcp.host.dispatch import KernelError, services_for

    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    rig.part.FullPath = "model1"
    services_for(rig.e).own(rig.part)  # a new part the agent created (gate 6)
    for method in ["nx_save_part", "nx_close_part"]:
        with pytest.raises(KernelError) as error:
            rig.e.execute(method, {})
        assert error.value.code == "NX_PATH_OUTSIDE_WORKSPACE"
    with pytest.raises(KernelError) as error:
        rig.e.execute("nx_save_part", {"path": str(tmp_path.parent / "elsewhere.prt")})
    assert error.value.code == "NX_PATH_OUTSIDE_WORKSPACE"
    destination = tmp_path / "smoke" / "model1.prt"

    def save_as(path):
        Path(path).write_bytes(b"saved")
        rig.part.FullPath = str(path)
        return NS(Dispose=Mock())

    rig.part.SaveAs = save_as
    saved_as = rig.e.execute("nx_save_part", {"path": str(destination)})
    assert saved_as["data"]["path"] == "smoke/model1.prt"
    assert rig.e.execute("nx_save_part", {})["data"]["saved_files"] == ["smoke/model1.prt"]
