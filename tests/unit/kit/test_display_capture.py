"""kit.display (snapshots, camera, sections) and kit.capture (viewport and Studio PNGs)."""

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import capture, display
from tests.kit import NXException
from tests.unit.display.fakes import WINDOW, display_nx, show_sheet


def test_snapshots_restore_newest_first_per_part():
    store = display.Snapshots()
    a1 = store.save("pa", [{"object": {"id": "b:1"}, "blanked": True}])
    b1 = store.save("pb", [])
    a2 = store.save("pa", [], camera={"scale": 2})
    assert store.latest("pa") == a2 and store.latest("pb") == b1
    with pytest.raises(NXError) as order:
        store.get(a1, "pa")
    assert order.value.code == "NX_DEPENDENCY_ORDER" and order.value.details["restore_first"] == a2
    with pytest.raises(NXError) as other:
        store.get(b1, "pa")
    assert other.value.code == "NX_WRONG_PART_CONTEXT"
    assert store.get(a2, "pa")["camera"] == {"scale": 2}
    store.drop(a2)
    assert store.get(a1, "pa")["records"][0]["blanked"] is True
    with pytest.raises(NXError) as stale:
        store.get("display_" + "0" * 32, "pa")
    assert stale.value.code == "NX_STALE_REFERENCE"


def test_snapshots_keep_a_bounded_history():
    store = display.Snapshots()
    first = store.save("p", [])
    for _ in range(display.Snapshots.LIMIT):
        store.save("p", [])
    assert first not in store.items and len(store.items) == display.Snapshots.LIMIT


def test_record_helpers():
    record = {"object": {"id": "fc:3"}, "blanked": False, "color_index": 5, "transparency": 0}
    assert display.record_id(record) == "fc:3" and display.record_id({"object": "b:1"}) == "b:1"
    assert display.state_of(record) == {"blanked": False, "color_index": 5, "transparency": 0}


def test_camera_style_names_and_drawing_views():
    nx = display_nx()
    assert display.camera(nx.tx, nx.work_view)["style"] == "shaded_with_edges"
    nx.work_view.RenderingStyle = 42
    assert display.camera(nx.tx, nx.work_view)["style"] == "nx_42"
    assert display.camera(nx.tx, nx.part.Views.WorkView)["style"] == "drawing"


def test_model_view_refuses_a_shown_sheet():
    nx = display_nx()
    show_sheet(nx)
    with pytest.raises(NXError) as error:
        display.model_view(nx.tx, nx.part)
    assert error.value.code == "NX_WRONG_APPLICATION"


def test_datums_include_coordinate_systems_once():
    nx = display_nx()
    plane = nx.datum_plane()
    nx.part.CoordinateSystems.append(plane)
    assert display.datums(nx.part) == [plane]


def test_viewport_png_returns_the_actual_window_size(tmp_path):
    nx = display_nx()
    target = tmp_path / "v.png"
    assert capture.viewport_png(nx.tx, target, 800, 500, style="wireframe") == WINDOW
    builder = nx.captures[0]
    assert builder.DeviceWidth == 800 and builder.styled == 9 and builder.destroyed
    assert nx.work_view.RenderingStyle == 0


def test_viewport_png_removes_a_partial_file_on_failure(tmp_path):
    nx = display_nx()
    nx.capture_error = NXException(3500001, "graphics failed")
    target = tmp_path / "v.png"
    with pytest.raises(NXError) as error:
        capture.viewport_png(nx.tx, target, 800, 500)
    assert error.value.code == "NX_CAPTURE_FAILED" and not target.exists()
    assert nx.captures[0].destroyed


def test_studio_png_keeps_the_style_when_none_and_uses_lighting(tmp_path):
    nx = display_nx()
    show_sheet(nx)  # no model view: only style=None works on a sheet
    size = capture.studio_png(
        nx.tx, tmp_path / "s.png", 320, 240, style=None, background="transparent", lighting=2
    )
    assert size == (320, 240) and nx.captures[0].BackgroundOption == "clear"
    assert nx.lights[0].committed == ["L2", "user"]
