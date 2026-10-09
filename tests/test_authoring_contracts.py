"""Builder arguments, coordinate frames and transaction contracts, not kernel tests."""

from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.hardened import IDENTITY, matmul, rows
from nx_mcp.runtime import NXToolError
from tests.fakes import Component, Curve, Feature, Sketch, point

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


def test_transform_readback_and_relative_composition(rig, tmp_path):
    root = Component("root")
    c = Component("instance", parent=root)
    rig.part.ComponentAssembly.RootComponent = root

    def move(obj, delta, rotation):
        # Measured on NX 2406: MoveComponent rotates the component's position
        # about the assembly origin, then adds the translation.
        p = [obj.position.X, obj.position.Y, obj.position.Z]
        q = [sum(rows(rotation)[i][k] * p[k] for k in range(3)) for i in range(3)]
        obj.position = point(q[0] + delta.X, q[1] + delta.Y, q[2] + delta.Z)
        obj.rotation = rig.e._nx_matrix(matmul(rows(rotation), rows(obj.rotation)))

    rig.part.ComponentAssembly.MoveComponent = move
    ref = rig.ref(c, "component")
    r = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    result = rig.e._set_component_transform(ref, [1, 2, 3], r)
    assert result["translation"] == [1, 2, 3] and result["rotation_matrix"] == r
    # Rotating a component away from the origin must keep the requested origin.
    result = rig.e._reposition_component(ref, dx=4, rz=90)
    assert result["translation"] == pytest.approx([5, 2, 3])
    assert abs(result["rotation_matrix"][0][0] + 1) < 1e-8
    rig.part.ComponentAssembly.MoveComponent = Mock()
    with pytest.raises(NXToolError) as error:
        rig.e._set_component_transform(ref, [9, 9, 9], IDENTITY)
    assert error.value.code == "NX_PLACEMENT_MISMATCH"
    c.Parent = Component("other")
    with pytest.raises(NXToolError):
        rig.e._set_component_transform(ref, [0, 0, 0], IDENTITY)
    c.Parent = root
    rig.part.ComponentAssembly.AddComponent = lambda *args: (c, NS(Dispose=Mock()))
    assert rig.e._add_component(str(tmp_path / "proto.prt"), translation=[1, 2, 3])[
        "translation"
    ] == [1, 2, 3]
    assert rig.e._list_components()["count"] == 1
    new = rig.e._rename_object(ref, "renamed")["object"]["id"]
    assert new != ref and c.Name == "renamed"
    with pytest.raises(NXToolError):
        rig.e._rename_object(new, "")


@pytest.mark.parametrize("rotation", [[], [[1, 0, 0]] * 3, [[1, 0, 0], [0, 1, 0], [0, 0, -1]]])
def test_rotation_rejects_scale_shear_or_reflection(rig, rotation):
    with pytest.raises(NXToolError):
        rig.e._validate_rotation(rotation)


def test_batch_preflight_cancellation_and_progress(rig):
    rig.e._current_operation = "batch-operation"
    rig.e.store.put({"operation_id": "batch-operation", "state": "running"})
    calls = []
    rig.e._handlers["nx_add_component"] = lambda start, end: calls.append((start, end)) or {}
    ops = [{"method": "nx_add_component", "params": {"start": 1, "end": 2}}] * 2
    assert rig.e._batch(ops)["count"] == 2 and len(calls) == 2
    assert rig.e.store.get("batch-operation")["progress"] == {"completed": 2, "total": 2}
    for bad in [
        [],
        [{"method": "nx_bad", "params": {}}],
        [{"method": "nx_add_component", "params": {}}],
    ]:
        with pytest.raises((NXToolError, TypeError)):
            rig.e._batch(bad)
    assert len(calls) == 2
    rig.e.store.path("batch-operation").with_suffix(".cancel").touch()
    with pytest.raises(NXToolError) as error:
        rig.e._batch(ops)
    assert error.value.code == "NX_CANCELLED" and len(calls) == 2


def test_batch_children_pass_the_version_gate_and_argument_binding(rig):
    rig.e._current_operation = "batch-gate"
    rig.e.store.put({"operation_id": "batch-gate", "state": "running"})
    calls = []
    rig.e._handlers["nx_add_component"] = lambda start, end: calls.append((start, end)) or {}
    rig.e._handlers["nx_reposition_component"] = lambda center, radius: calls.append(radius) or {}
    rig.e.nx_version = "v2406"
    rig.e._manifest_tools = {
        "nx_reposition_component": {"unavailable_on": {"v2406": "API missing"}}
    }
    ops = [
        {"method": "nx_add_component", "params": {"start": 1, "end": 2}},
        {"method": "nx_reposition_component", "params": {"center": 0, "radius": 1}},
    ]
    with pytest.raises(NXToolError) as error:
        rig.e._batch(ops)
    assert error.value.code == "NX_UNAVAILABLE_ON_VERSION" and not calls  # Refused in preflight.
    with pytest.raises(NXToolError) as error:
        rig.e._batch([{"method": "nx_add_component", "params": {"start": 1}}])
    assert error.value.code == "NX_INVALID_ARGUMENT" and not calls
    # The gate also applies when the child runs, not only in preflight.
    with pytest.raises(NXToolError) as error:
        rig.e._call_child("nx_reposition_component", {"center": 0, "radius": 1})
    assert error.value.code == "NX_UNAVAILABLE_ON_VERSION" and not calls


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
