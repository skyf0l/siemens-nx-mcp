"""Native inspection seam tests: reports, temporary solids and cleanup failures."""

import struct
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Component

pytestmark = pytest.mark.fake_nx


def test_occurrence_geometry_bounds_and_volume_are_explicit(rig):
    base = Body("base", [0, 0, 0, 2, 3, 4])
    base.volume = 24
    rig.part.Bodies.append(base)
    root = Component("root")
    a = Component("a", [Body()], root)
    nested = Component("nested", [Body()], a)
    rig.part.ComponentAssembly.RootComponent = root
    assert len(rig.e._geometry(scope="assembly")) == 3
    assert len(rig.e._geometry(rig.ref(a, "component"))) == 2
    a.IsSuppressed = True
    assert rig.e._occurrence_bodies(nested) == []
    assert rig.e._geometry(scope="assembly") == [base]
    rig.ref(base)
    for precision in ["exact", "conservative"]:
        result = rig.e._get_bounding_box(precision=precision)
        assert result["dimensions"] == [2, 3, 4] and result["units"] == "mm"
    assert rig.e._measure_volume()["volume_mm3"] == 24
    base.IsSolidBody = False
    with pytest.raises(NXToolError):
        rig.e._measure_volume()
    with pytest.raises(NXToolError):
        rig.e._get_bounding_box(precision="bad")
    rig.part.WCS.CoordinateSystem.Orientation.Element.Xx = 0
    with pytest.raises(NXToolError):
        rig.e._get_bounding_box(precision="exact")
    with pytest.raises(NXToolError):
        rig.e._geometry(scope="bad")
    rig.part.Bodies.clear()
    with pytest.raises(NXToolError):
        rig.e._geometry(scope="part")


def native_pair(rig, result=1, temporary=True):
    b = NS(FirstBody=NS(Value=None), SecondBody=NS(Value=None), Reset=Mock(), Destroy=Mock())
    solid = Body()
    solid.volume = 125
    sheet = Body()
    sheet.IsSolidBody = False

    def perform():
        if temporary:
            rig.part.Bodies.append(solid)
        return result

    b.PerformCheck = Mock(side_effect=perform)
    b.GetInterferenceResults = lambda: [solid, sheet]
    rig.part.AnalysisManager = NS(CreateSimpleInterferenceObject=lambda: b)
    return b


def image_builder(rig, payload=None, fail=False):
    b = NS(Destroy=Mock(), SetCustomBackgroundColor=Mock())

    def commit():
        if fail:
            raise RuntimeError("graphics failed")
        Path(b.FileName).write_bytes(
            payload
            if payload is not None
            else b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 320, 240)
        )

    b.Commit = commit
    rig.part.Views = NS(CreateImageExportBuilder=lambda: b)
    return b


@pytest.mark.parametrize(
    "background,style", [("white", "shaded"), ("original", "current"), ("transparent", "wireframe")]
)
def test_viewport_metadata_and_style_restoration(rig, background, style):
    b = image_builder(rig)
    result = rig.e._capture_view(
        width=320, height=240, background=background, style=style, fit=True
    )
    assert result["resolution"] == [320, 240] and not result["warnings"]
    assert result["capture_kind"] == "nx_model_viewport"
    assert rig.part.ModelingViews.WorkView.RenderingStyle == "shaded"
    assert b.Destroy.call_count == 1
    rig.part.ModelingViews.WorkView.RenderingStyle = 99
    assert rig.e._view_info()["rendering_style"] == "nx_style_99"


@pytest.mark.parametrize(
    "failure",
    [
        "native",
        "invalid_png",
        "wrong_size",
        "missing_display",
        "batch",
        "exists",
        "extension",
        "resolution",
        "style",
    ],
)
def test_capture_failure_does_not_leave_style_changed(rig, tmp_path, failure):
    b = image_builder(
        rig, payload=b"broken" if failure == "invalid_png" else None, fail=failure == "native"
    )
    p = tmp_path / "test.png"
    kwargs = {"path": str(p)}
    if failure == "missing_display":
        rig.session.Parts.Display = None
    if failure == "batch":
        rig.session.IsBatch = True
    if failure == "exists":
        p.write_bytes(b"preserved")
    if failure == "extension":
        kwargs["path"] = str(tmp_path / "test.jpg")
    if failure == "resolution":
        kwargs["width"] = True
    if failure == "style":
        kwargs["style"] = "bad"
    if failure == "wrong_size":
        result = rig.e._capture_view(**kwargs)
        assert result["warnings"]
        return
    with pytest.raises((NXToolError, RuntimeError)):
        rig.e._capture_view(**kwargs)
    assert rig.part.ModelingViews.WorkView.RenderingStyle == "shaded"
    if failure in ["native", "invalid_png"]:
        assert b.Destroy.call_count == 1
    if failure == "exists":
        assert p.read_bytes() == b"preserved"


def test_failed_builder_destroy_still_restores_view_style(rig):
    builder = image_builder(rig)
    builder.Destroy = Mock(side_effect=RuntimeError("destroy failed"))
    with pytest.raises(RuntimeError):
        rig.e._capture_view(style="wireframe")
    assert rig.part.ModelingViews.WorkView.RenderingStyle == "shaded"
