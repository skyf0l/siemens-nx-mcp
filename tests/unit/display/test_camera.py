"""nx_get_camera / nx_set_camera on the shared fake kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import display
from tests.kit import run
from tests.unit.display.fakes import display_nx, show_sheet


def flat(m):
    return [v for row in m for v in row]


@pytest.fixture(autouse=True)
def fresh_snapshots():
    display.snapshots.clear()
    yield
    display.snapshots.clear()


def test_get_camera_reads_the_model_view():
    nx = display_nx()
    ok = run(nx, "nx_get_camera")
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    assert ok.frame == "display_part_absolute"
    data = ok.data
    assert data["view"] == "Trimetric" and data["style"] == "shaded_with_edges"
    assert flat(data["rotation"]) == pytest.approx(flat(display.ORIENTATION_ROTATIONS["trimetric"]))
    assert data["origin"] == [-1.0, -2.0, -3.0] and data["scale"] == 1.5
    assert data["interactive"] is True and "sheet" not in data


def test_get_camera_reports_the_shown_sheet():
    nx = display_nx()
    show_sheet(nx)
    data = run(nx, "nx_get_camera").data
    assert data["sheet"]["name"] == "SHT1" and data["style"] == "drawing"
    assert data["view"] == "SHT1@0" and data["origin"] == [-210, -148.5, 0]


def test_orientation_with_fit_is_read_back_and_restorable():
    nx = display_nx()
    view = nx.work_view
    ok = run(nx, "nx_set_camera", orientation="isometric", fit=True)
    assert ok.ok and ok.mutation_outcome == "committed"
    assert flat(ok.data["rotation"]) == pytest.approx(
        flat(display.ORIENTATION_ROTATIONS["isometric"])
    )
    assert view.fits == 1 and ok.data["scale"] == 2.5
    snapshot = display.snapshots.items[ok.restore_id]
    assert snapshot["camera"]["scale"] == 1.0  # the kept (fake services) camera
    assert snapshot["records"] == []


def test_orientation_keeps_the_scale_without_fit():
    nx = display_nx()
    ok = run(nx, "nx_set_camera", orientation="front")
    assert nx.work_view.fits == 0 and ok.data["scale"] == 1.5
    assert flat(ok.data["rotation"]) == pytest.approx(flat(display.ORIENTATION_ROTATIONS["front"]))


def test_explicit_camera_round_trips_and_fills_missing_values():
    nx = display_nx()
    rotation = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    ok = run(nx, "nx_set_camera", rotation=rotation, origin=[2, 3, 4], scale=2)
    assert ok.ok and ok.data["origin"] == [2, 3, 4] and ok.data["scale"] == 2
    assert flat(ok.data["rotation"]) == pytest.approx(flat(rotation))
    zoom = run(nx, "nx_set_camera", scale=4)
    assert zoom.data["origin"] == [2, 3, 4] and flat(zoom.data["rotation"]) == pytest.approx(
        flat(rotation)
    )


@pytest.mark.parametrize(
    "args,field",
    [
        ({}, None),
        ({"orientation": "top", "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}, "/rotation"),
        ({"fit": True, "scale": 2}, "/fit"),
        ({"rotation": [[1, 0, 0], [1, 0, 0], [0, 0, 1]]}, "/rotation"),
    ],
)
def test_bad_arguments_are_not_started(args, field):
    nx = display_nx()
    bad = run(nx, "nx_set_camera", **args)
    assert bad.error["category"] == "invalid_argument" and bad.mutation_outcome == "not_started"
    if field:
        assert bad.error["field"] == field


def test_a_camera_nx_does_not_take_rolls_back():
    nx = display_nx()
    nx.work_view.ignore_scale = True
    bad = run(nx, "nx_set_camera", scale=7)
    assert bad.error["category"] == "verification_failed"
    assert bad.error["details"]["check"] == "camera.scale"
    assert bad.mutation_outcome == "rolled_back" and nx.camera["scale"] == 1.0


def test_a_shown_sheet_has_no_model_camera():
    nx = display_nx()
    show_sheet(nx)
    bad = run(nx, "nx_set_camera", orientation="top")
    assert bad.error["code"] == "NX_WRONG_APPLICATION" and bad.mutation_outcome == "not_started"
    assert "nx_activate_drawing_sheet" in bad.error["hint"]


def test_work_and_display_part_must_match():
    nx = display_nx()
    nx.session.Parts.Display = NS(Tag=-1)
    bad = run(nx, "nx_set_camera", fit=True)
    assert bad.error["code"] == "NX_WRONG_PART_CONTEXT"


@pytest.mark.parametrize(
    ("orientation", "view_from", "up"),
    [
        ("top", [0, 0, 1], [0, 1, 0]),
        ("bottom", [0, 0, -1], [0, -1, 0]),
        ("front", [0, -1, 0], [0, 0, 1]),
        ("back", [0, 1, 0], [0, 0, 1]),
        ("right", [1, 0, 0], [0, 0, 1]),
        ("left", [-1, 0, 0], [0, 0, 1]),
    ],
)
def test_named_orientations_report_where_they_look_from(orientation, view_from, up):
    data = run(display_nx(), "nx_set_camera", orientation=orientation).data
    assert data["view_from"] == pytest.approx(view_from) and data["up"] == pytest.approx(up)
    assert display.ORIENTATION_FROM[orientation] == (pytest.approx(view_from), pytest.approx(up))
    # the axes are the rotation's columns: rotation[i][j] = world component i of axis j
    columns = [[row[j] for row in data["rotation"]] for j in range(3)]
    assert columns == [data["right"], data["up"], data["view_from"]]


def test_view_from_reproduces_nx_isometric_and_reaches_every_octant():
    iso = display.rotation_from([1, -1, 1])
    assert flat(iso) == pytest.approx(flat(display.ORIENTATION_ROTATIONS["isometric"]))
    nx = display_nx()
    ok = run(nx, "nx_set_camera", view_from=[-2, 2, 2], fit=True)
    assert ok.ok and ok.data["view_from"] == pytest.approx([-(3**-0.5), 3**-0.5, 3**-0.5])
    assert ok.data["right"][2] == pytest.approx(0) and ok.data["up"][2] > 0
    assert nx.work_view.fits == 1 and ok.restore_id
    down = run(display_nx(), "nx_set_camera", view_from=[0, 0, -5]).data
    assert down["view_from"] == pytest.approx([0, 0, -1]) and down["up"] == pytest.approx([0, 1, 0])
    tilted = run(display_nx(), "nx_set_camera", view_from=[1, 0, 0], up=[0, 1, 1]).data
    assert tilted["up"] == pytest.approx([0, 2**-0.5, 2**-0.5])


def test_explicit_rotation_is_echoed_unchanged_with_its_columns():
    r = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    data = run(display_nx(), "nx_set_camera", rotation=r).data
    assert flat(data["rotation"]) == pytest.approx(flat(r))
    assert data["right"] == pytest.approx([0, 1, 0]) and data["up"] == pytest.approx([-1, 0, 0])
    assert data["view_from"] == pytest.approx([0, 0, 1])


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"view_from": [1, 1, 1], "orientation": "top"}, "/view_from"),
        ({"view_from": [1, 1, 1], "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}, "/rotation"),
        ({"up": [0, 0, 1], "orientation": "top"}, "/up"),
        ({"view_from": [0, 0, 1], "up": [0, 0, 2]}, "/up"),
        ({"view_from": [0, 0, 0]}, "/view_from"),
    ],
)
def test_view_from_conflicts_are_invalid(args, field):
    bad = run(display_nx(), "nx_set_camera", **args)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started"
