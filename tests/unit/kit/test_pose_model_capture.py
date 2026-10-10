"""kit.pose (pure pose math), kit.model and kit.capture."""

import math
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import capture, geom, model
from nx_mcp.kit import pose as kpose
from tests.kit import fake_nx

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
I3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def flat(m):
    return [v for row in m for v in row]


def nx_move(current, shift, delta):
    """NX 2406 MoveComponent: rotate about the assembly origin, then translate."""
    t, r = current
    return geom.add(geom.matvec(delta, t), shift), geom.matmul(delta, r)


def test_move_args_compensate_the_assembly_origin_pivot():
    current = ([20.0, 0.0, 0.0], I3)
    target = ([30.0, 10.0, 0.0], RZ90)
    shift, delta = kpose.move_args(current, target)
    got = nx_move(current, shift, delta)
    assert got[0] == pytest.approx(target[0]) and flat(got[1]) == pytest.approx(flat(RZ90))


def test_relative_turns_about_the_component_origin():
    t, r = kpose.relative(([30.0, 10.0, 0.0], I3), [5.0, 0.0, 0.0], RZ90)
    assert t == [35.0, 10.0, 0.0] and r == RZ90
    assert kpose.relative(([1.0, 2.0, 3.0], RZ90), [0.0, 0.0, 1.0]) == ([1.0, 2.0, 4.0], RZ90)


def test_compose_inverse_and_local_delta():
    base = ([10.0, 0.0, 5.0], RZ90)
    target = ([0.0, 0.0, 100.0], I3)
    m = kpose.local_delta(base, target)
    delta = ([m[0][3], m[1][3], m[2][3]], [row[:3] for row in m[:3]])
    got = kpose.compose(base, delta)
    assert kpose.close(got, target, 1e-12) and m[3] == [0.0, 0.0, 0.0, 1.0]
    assert kpose.close(kpose.compose(kpose.inverse(base), base), ([0, 0, 0], I3), 1e-12)


def test_axis_rotation_and_rotate_about():
    assert flat(kpose.axis_rotation([0, 0, 2], 90)) == pytest.approx(flat(RZ90))
    t, r = kpose.rotate_about(([60.0, 0.0, 0.0], I3), [10.0, 0.0, 0.0], [0, 0, 1], 180)
    assert t == pytest.approx([-40.0, 0.0, 0.0]) and r[0][0] == pytest.approx(-1)
    with pytest.raises(NXError):
        kpose.axis_rotation([0, 0, 0], 10)


def test_as_pose_defaults_and_validation():
    assert kpose.as_pose({"translation": [1, 2, 3]}) == ([1.0, 2.0, 3.0], I3)
    with pytest.raises(NXError) as error:
        kpose.as_pose({"translation": [0, 0, 0], "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, -1]]})
    assert error.value.field == "/pose/rotation"


@pytest.mark.parametrize("axis", range(3))
def test_slerp_stays_orthonormal_through_a_half_turn(axis):
    end = [[float(i == j) * (1 if i == axis else -1) for j in range(3)] for i in range(3)]
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        m = kpose.slerp(I3, end, fraction)
        for i in range(3):
            for j in range(3):
                assert geom.dot(m[i], m[j]) == pytest.approx(float(i == j))
    assert kpose.slerp(I3, I3, 0.5) == I3
    assert flat(kpose.slerp(I3, end, 1)) == pytest.approx(flat(end))


def test_interpolate_is_linear_in_translation():
    t, r = kpose.interpolate(([0.0, 0.0, 0.0], I3), ([0.0, 0.0, 60.0], RZ90), 0.5)
    assert t == [0.0, 0.0, 30.0]
    assert r[0][0] == pytest.approx(math.cos(math.pi / 4))


# ---- kit.model and kit.capture ------------------------------------------------------------


def test_model_update_uses_the_call_mark_and_maps_errors():
    nx = fake_nx()
    nx.tx.mark = "m1"
    nx.session.UpdateManager.DoUpdate = Mock(return_value=0)
    model.update(nx.tx)
    nx.session.UpdateManager.DoUpdate.assert_called_with("m1")
    model.update(nx.tx, "m2")
    nx.session.UpdateManager.DoUpdate.assert_called_with("m2")
    nx.session.UpdateManager.DoUpdate = Mock(return_value=2)
    with pytest.raises(NXError) as error:
        model.update(nx.tx)
    assert error.value.code == "NX_UPDATE_FAILED"


def _capture_rig(tmp_path, size=(320, 240)):
    nx = fake_nx()
    view = NS(RenderingStyle="wire", UpdateDisplay=Mock())
    nx.nx.View = NS(RenderingStyleType=NS(ShadedWithEdges="swe", Studio="st", Shaded="sh"))
    made = []

    class Builder(NS):
        def __init__(self):
            super().__init__(
                SourceType=NS(WorkView=1), UnitsEnumType=NS(Pixels=1),
                BackgroundOptions=NS(CustomColor=1, Original=2), destroyed=False,
            )  # fmt: skip
            made.append(self)

        def SetImageDimensionsInteger(self, dims):
            self.dims = dims

        def SetCustomBackgroundColor(self, color):
            self.color = color

        def Commit(self):
            w, h = size
            header = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + w.to_bytes(4, "big")
            (tmp_path / "f.png").write_bytes(header + h.to_bytes(4, "big"))

        def Destroy(self):
            self.destroyed = True

    nx.part.ModelingViews = NS(WorkView=view)
    nx.part.Views = NS(CreateStudioImageCaptureBuilder=Builder)
    return nx, view, made


def test_studio_png_renders_the_size_and_restores_the_style(tmp_path):
    nx, view, made = _capture_rig(tmp_path)
    assert capture.studio_png(nx.tx, tmp_path / "f.png", 320, 240) == (320, 240)
    assert made[0].dims == [240, 320] and made[0].destroyed and view.RenderingStyle == "wire"


def test_studio_png_removes_a_wrong_size_and_refuses_batch(tmp_path):
    nx, view, made = _capture_rig(tmp_path, size=(100, 100))
    with pytest.raises(NXError) as error:
        capture.studio_png(nx.tx, tmp_path / "f.png", 320, 240)
    assert error.value.code == "NX_CAPTURE_FAILED" and not (tmp_path / "f.png").exists()
    assert made[0].destroyed and view.RenderingStyle == "wire"
    nx.session.IsBatch = True
    with pytest.raises(NXError) as batch:
        capture.studio_png(nx.tx, tmp_path / "f.png", 320, 240)
    assert batch.value.code == "NX_VIEWPORT_UNAVAILABLE"
