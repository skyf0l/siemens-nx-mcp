"""nx_get_camera / nx_set_camera on the shared fake kernel."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import display
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Part
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
    assert data["view"] == "trimetric" and data["view_angle"] == 0.0
    assert data["style"] == "shaded_with_edges"
    assert flat(data["rotation"]) == pytest.approx(flat(display.ORIENTATION_ROTATIONS["trimetric"]))
    assert data["origin"] == [-1.0, -2.0, -3.0] and data["scale"] == 1.5
    assert data["interactive"] is True and "sheet" not in data


def test_get_camera_reads_the_display_or_shows_the_named_background_part():
    """Two of the agent's parts loaded: the camera read needs no part (never
    NX_AMBIGUOUS_PART) and then switches nothing. A named part in the background is shown
    for the read (live: the engineer's camera came back for part X) and the shown tab
    comes back."""
    nx = display_nx()
    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)  # tab switches
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    parts = nx.session.Parts
    shown = parts.Display
    plate = Part(nx.session, Path(services(nx).root) / "plate.prt")
    parts.Work = parts.Display = shown
    nx.session.strict_work = True
    ok = run(nx, "nx_get_camera")
    assert ok.ok and ok.data["part"]["name"] == shown.Name, ok
    assert nx.session.calls == [] and parts.Display is shown
    seen = []
    plate.ModelingViews = NS(WorkView=shown.ModelingViews.WorkView)
    original = display.camera
    try:
        display.camera = lambda tx, view: (
            seen.append(tx.session.Parts.Display) or original(tx, view)
        )
        ok = run(nx, "nx_get_camera", part="plate")
    finally:
        display.camera = original
    assert ok.ok, ok
    assert ok.data["part"]["name"] == plate.Name and seen == [plate]
    assert parts.Display is shown and parts.Work is shown


def test_get_camera_reports_the_shown_sheet():
    nx = display_nx()
    show_sheet(nx)
    data = run(nx, "nx_get_camera").data
    assert data["sheet"]["name"] == "SHT1" and data["style"] == "drawing"
    assert data["view"] == "SHT1@0" and data["origin"] == [-210, -148.5, 0]
    assert "view_angle" not in data  # a sheet keeps NX's view name


def test_a_custom_camera_is_custom_with_the_nearest_view_and_the_angle_off_it():
    nx = display_nx()
    ok = run(nx, "nx_set_camera", view_from=[-1, -1, 1])
    assert ok.ok and ok.data["view_from"] == pytest.approx([-(3**-0.5), -(3**-0.5), 3**-0.5])
    # (-1,-1,1), the isometric octant facing a part's min-X front, is 54.7 degrees from
    # front, left and top alike: the smallest whole rotation (front, +Z up) is nearest,
    # but that far off the view is not called front.
    assert ok.data["view"] == "custom" and ok.data["nearest_view"] == "front"
    assert ok.data["view_angle"] == pytest.approx(54.736)
    assert run(nx, "nx_get_camera").data["view"] == "custom"
    iso = run(nx, "nx_set_camera", orientation="isometric").data
    assert iso["view"] == "isometric" and iso["view_angle"] == 0.0 and "nearest_view" not in iso
    near = run(nx, "nx_set_camera", view_from=[0.05, -1, 0]).data
    assert near["view"] == "front" and near["view_angle"] == pytest.approx(2.862, abs=1e-3)
    assert "nearest_view" not in near


def test_iso_is_isometric():
    nx = display_nx()
    ok = run(nx, "nx_set_camera", orientation="iso", fit=True)
    assert ok.ok and ok.data["view"] == "isometric" and nx.work_view.fits == 1
    assert flat(ok.data["rotation"]) == pytest.approx(
        flat(display.ORIENTATION_ROTATIONS["isometric"])
    )


def test_zoom_multiplies_the_current_scale_about_the_view_centre():
    nx = display_nx()
    run(nx, "nx_set_camera", rotation=[[0, -1, 0], [1, 0, 0], [0, 0, 1]], origin=[2, 3, 4],
        scale=2)  # fmt: skip
    ok = run(nx, "nx_set_camera", zoom=1.5)
    assert ok.ok and ok.data["scale"] == pytest.approx(3) and ok.data["origin"] == [2, 3, 4]
    assert flat(ok.data["rotation"]) == pytest.approx(flat([[0, -1, 0], [1, 0, 0], [0, 0, 1]]))
    assert ok.restore_id
    out = run(nx, "nx_set_camera", zoom=0.5).data
    assert out["scale"] == pytest.approx(1.5)


def test_zoom_after_fit_scales_the_fitted_view():
    nx = display_nx()
    ok = run(nx, "nx_set_camera", orientation="front", fit=True, zoom=0.8)
    assert ok.ok and nx.work_view.fits == 1
    assert ok.data["scale"] == pytest.approx(2.5 * 0.8)  # the fake fits to 2.5
    plain = run(display_nx(), "nx_set_camera", fit=True, zoom=2).data
    assert plain["scale"] == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("args", "field", "words"),
    [
        ({}, None, "zoom"),
        ({"orientation": "top", "view_from": [1, 1, 1]}, "/view_from", "orientation and view_from"),
        ({"scale": 2, "zoom": 2}, "/zoom", "not both"),
        ({"fit": True, "scale": 2}, "/fit", "zoom"),
        ({"up": [0, 0, 1], "orientation": "top"}, "/up", "view_from"),
    ],
)
def test_errors_name_the_fields_and_what_to_send(args, field, words):
    bad = run(display_nx(), "nx_set_camera", **args)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.mutation_outcome == "not_started"
    assert bad.error.get("field") == field and words in bad.error["message"]
    if not args:
        assert "isometric" in bad.error["hint"]


def test_nearest_view_names_every_canned_rotation_whatever_the_roll():
    for name, rotation in display.ORIENTATION_ROTATIONS.items():
        assert display.nearest_view(rotation) == (name, 0.0)
    rolled = display.rotation_from([1, 0, 0], [0, 1, 0])  # right, with +Y up
    assert display.nearest_view(rolled) == ("right", 0.0)
    assert display.nearest_view(display.rotation_from([0, 0, -1])) == ("bottom", 0.0)


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


def test_a_named_work_part_that_is_not_displayed_is_shown_for_the_read():
    """Live: the agent's part was NX's (quiet) work part beside the
    engineer's displayed part; gate 4s does not switch a target that is already the work
    part, so nx_get_camera part=X read the engineer's view and named their part. The read
    now shows X, then the engineer's display and the quiet work part come back."""
    nx = display_nx()
    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    parts = nx.session.Parts
    shown = parts.Display
    plate = Part(nx.session, Path(services(nx).root) / "plate.prt")
    plate.ModelingViews = NS(WorkView=shown.ModelingViews.WorkView)
    parts.Display, parts.Work = shown, plate  # the work part, quietly, not displayed
    seen = []
    original = display.camera
    try:
        display.camera = lambda tx, view: (
            seen.append(tx.session.Parts.Display) or original(tx, view)
        )
        ok = run(nx, "nx_get_camera", part="plate")
    finally:
        display.camera = original
    assert ok.ok, ok
    assert ok.data["part"]["name"] == plate.Name and seen == [plate]
    assert parts.Display is shown and parts.Work is plate
    ok = run(nx, "nx_get_camera")  # unnamed: what NX displays, nothing switched
    assert ok.data["part"]["name"] == shown.Name and parts.Work is plate


def test_set_camera_shows_a_named_work_part_that_is_not_displayed():
    """The same gate 4s gap: part=X, X the quiet work part beside a displayed part, was
    refused ("The displayed part is not the work part"); X is now shown, its view set,
    and the display comes back."""
    nx = display_nx()
    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    parts = nx.session.Parts
    shown = parts.Display
    plate = Part(nx.session, Path(services(nx).root) / "plate.prt")
    plate.ModelingViews = shown.ModelingViews
    parts.Display, parts.Work = shown, plate
    ok = run(nx, "nx_set_camera", part="plate", orientation="top")
    assert ok.ok, ok
    assert ok.data["part"]["name"] == plate.Name and ok.data["view"] == "top"
    assert parts.Display is shown and parts.Work is plate


def _screen_offset(cam, centre):
    return display.centre_offset(cam, centre)


def test_fit_frames_the_shown_bodies_centred_not_the_origin():
    """A model far from the absolute origin: fit frames the bodies, their box centre at
    the viewport centre, and leaves out datums and hidden bodies."""
    nx = display_nx()
    body = nx.block(790, 400, 400, origin=(2200, -200, -200))
    hidden = nx.block(1, 1, 1)
    hidden.IsBlanked = True
    nx.datum_plane()
    ok = run(nx, "nx_set_camera", fit=True)
    assert ok.ok, ok.error
    assert nx.work_view.fit_to == [body] and nx.work_view.fits == 1
    assert ok.data["fit"]["bodies"] == 1
    assert ok.data["fit"]["centre"] == pytest.approx([2595, 0, 0])
    assert ok.data["fit"]["offset"] == pytest.approx(0, abs=1e-5)
    assert _screen_offset(ok.data, [2595, 0, 0]) == pytest.approx(0, abs=1e-5)
    assert ok.data["absolute_origin"] == pytest.approx([2595, 0, 0], abs=1e-3)


def test_a_fit_that_frames_the_origin_too_is_panned_to_the_bodies_centre():
    """NX's whole fit (FitToObjects refused) also frames the datum CSYS at the origin: the
    view is panned so the bodies' box centre is at the viewport centre."""
    nx = display_nx()
    nx.block(790, 400, 400, origin=(2200, -200, -200))
    nx.work_view.fit_to_error = RuntimeError("not fittable")
    ok = run(nx, "nx_set_camera", orientation="isometric", fit=True)
    assert ok.ok, ok.error
    assert ok.data["view"] == "isometric" and ok.data["scale"] == 2.5
    assert ok.data["fit"]["offset"] == pytest.approx(0, abs=1e-6)
    assert _screen_offset(ok.data, [2595, 0, 0]) == pytest.approx(0, abs=1e-6)


def test_fit_without_bodies_uses_nx_fit():
    nx = display_nx()
    nx.datum_plane()
    ok = run(nx, "nx_set_camera", fit=True)
    assert ok.ok and ok.data["fit"] == {"bodies": 0} and nx.work_view.fit_to is None


def test_a_fit_nx_leaves_off_centre_rolls_back():
    nx = display_nx()
    nx.block(10, 10, 10, origin=(500, 0, 0))
    view = nx.work_view
    view.fit_to_error = RuntimeError("not fittable")
    view.SetRotationTranslationScale = lambda m, p, s: None  # NX ignores the pan
    bad = run(nx, "nx_set_camera", fit=True)
    assert bad.error["category"] == "verification_failed"
    assert bad.mutation_outcome == "rolled_back"


def test_origin_for_puts_a_point_at_the_viewport_centre():
    """view.Origin is the negated view-space position of the centre: origin_for inverts
    it for any rotation, and the depth along the view direction does not count."""
    rotation = display.rotation_from([-1, -1, 1])
    centre = [2595.0, 10.0, -4.0]
    origin = display.origin_for(rotation, centre)
    absolute = [-sum(rotation[i][j] * origin[j] for j in range(3)) for i in range(3)]
    assert absolute == pytest.approx(centre)
    deeper = [
        c + 50 * v for c, v in zip(centre, display.view_axes(rotation)["view_from"], strict=True)
    ]
    cam = {"rotation": rotation, "absolute_origin": deeper}
    assert display.centre_offset(cam, centre) == pytest.approx(0, abs=1e-9)
    cam["absolute_origin"] = [centre[0], centre[1], centre[2] + 3]
    assert display.centre_offset(cam, centre) > 0


def test_a_colour_table_in_0_255_is_read_as_0_1():
    entries = {1: (255, 0, 0), 2: (0, 0, 255)}
    colors = NS(Find=lambda i: NS(Label=f"C{i}", GetRgb=lambda: NS(
        R=entries[i][0], G=entries[i][1], B=entries[i][2])) if i in entries else None)  # fmt: skip
    table = display.palette(NS(Colors=colors))
    assert table == {1: ("C1", [1.0, 0.0, 0.0]), 2: ("C2", [0.0, 0.0, 1.0])}
    assert display.hex_of(table[2][1]) == "#0000FF"


def test_a_new_direction_turns_about_the_view_centre():
    """NX keeps the view-space origin when only the rotation changes, which moved the model
    35 mm off centre live: the camera turns about the point at the viewport centre."""
    nx = display_nx()
    nx.block(100, 50, 20)
    top = run(nx, "nx_set_camera", orientation="top", fit=True)
    centre = top.data["absolute_origin"]
    turned = run(nx, "nx_set_camera", view_from=[-1, -1, 1])
    assert turned.ok, turned.error
    assert turned.data["absolute_origin"] == pytest.approx(centre)
    assert turned.data["scale"] == top.data["scale"]
    front = run(nx, "nx_set_camera", orientation="front")
    assert front.data["absolute_origin"] == pytest.approx(centre)
    assert flat(front.data["rotation"]) == pytest.approx(
        flat(display.ORIENTATION_ROTATIONS["front"])
    )


def test_center_puts_a_point_at_the_view_centre_and_style_stays():
    nx = display_nx()
    ok = run(nx, "nx_set_camera", center=[10, 20, 30], style="wireframe")
    assert ok.ok, ok.error
    assert ok.data["absolute_origin"] == pytest.approx([10, 20, 30])
    assert ok.data["style"] == "wireframe" and nx.work_view.RenderingStyle == 9
    turned = run(nx, "nx_set_camera", orientation="right", center=[1, 2, 3])
    assert turned.data["absolute_origin"] == pytest.approx([1, 2, 3])
    assert turned.data["view"] == "right"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"center": [0, 0, 0], "origin": [0, 0, 0]}, "/center"),
        ({"center": [0, 0, 0], "fit": True}, "/center"),
    ],
)
def test_center_rules(args, field):
    nx = display_nx()
    nx.block(1, 1, 1)
    bad = run(nx, "nx_set_camera", **args)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started"
