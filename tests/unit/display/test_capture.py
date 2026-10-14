"""nx_capture_view: viewport and Studio PNGs, inline image metadata, sheets, cleanup."""

from pathlib import Path

import pytest

from nx_mcp.kit import display, files
from tests.kit import NXException, run
from tests.kit.kernel import services
from tests.kit.objects import Object, Sketch
from tests.unit.display.fakes import WINDOW, display_nx, show_sheet


def root(nx) -> Path:
    return services(nx).root


def test_viewport_capture_reports_the_window_size_and_an_inline_image():
    nx = display_nx()
    ok = run(nx, "nx_capture_view")
    assert ok.ok and ok.mutation_outcome == "not_applicable"  # a view-only action
    data = ok.data
    # No width/height: the window's size, and no requested size to mistake for it (live:
    # a schema default 1024x768 next to a 1106x1794 image read as a failed request).
    assert "requested" not in data and data["resolution"] == list(WINDOW)
    assert not ok.warnings
    assert data["path"].startswith("captures/view-") and data["path"].endswith(".png")
    file = root(nx) / data["path"]
    assert files.png_size(file) == WINDOW and data["sha256"] == files.sha256(file)
    image = data["image"]
    assert image == {
        "path": data["path"],
        "sha256": data["sha256"],
        "inline": True,
        "mime_type": "image/png",
        "width": WINDOW[0],
        "height": WINDOW[1],
    }
    assert not ok.warnings and data["quality"] == "viewport"  # no size asked: the window's
    builder = nx.captures[0]
    assert builder.kind == "viewport" and builder.destroyed
    assert builder.background_color == [1.0, 1.0, 1.0] and builder.EnhanceEdges is True
    assert data["camera"]["view"] == "trimetric" and "restore_id" not in ok.envelope
    assert data["hidden_sketches"] == 0 and data["marks_part_modified"] is False


def test_a_given_size_is_honoured_by_a_studio_render_of_the_shaded_view():
    nx = display_nx()
    ok = run(nx, "nx_capture_view", width=800, height=500, path="shots/v.png")
    assert ok.ok and not ok.warnings, ok
    assert ok.data["resolution"] == [800, 500] and ok.data["quality"] == "studio"
    assert files.png_size(root(nx) / "shots" / "v.png") == (800, 500)
    assert [c.kind for c in nx.captures] == ["viewport", "studio"]
    assert sorted(p.name for p in (root(nx) / "shots").iterdir()) == ["v.png"]


def test_a_size_equal_to_the_default_still_counts_as_given():
    nx = display_nx()
    ok = run(nx, "nx_capture_view", width=1024)  # tx.given: the caller asked for 1024x768
    assert ok.ok and ok.data["resolution"] == [1024, 768] and ok.data["quality"] == "studio"


def test_a_wireframe_view_or_a_sheet_keeps_the_window_size_with_a_warning():
    nx = display_nx()
    wire = run(nx, "nx_capture_view", width=800, height=500, style="wireframe")
    assert wire.data["resolution"] == list(WINDOW) and wire.data["quality"] == "viewport"
    assert "a wireframe view is captured at the window size" in wire.warnings[0]["message"]
    show_sheet(nx)
    sheet = run(nx, "nx_capture_view", width=800, height=500)
    assert "a drawing sheet is captured" in sheet.warnings[0]["message"]
    assert [c.kind for c in nx.captures] == ["viewport", "viewport"]
    studio = run(nx, "nx_capture_view", quality="studio")
    assert studio.error["field"] == "/quality" and studio.mutation_outcome == "not_started"


def test_studio_capture_renders_the_exact_size_with_style_light_and_colour():
    nx = display_nx()
    path = str(root(nx) / "shots" / "a.png")
    ok = run(
        nx, "nx_capture_view", quality="studio", path=path, width=320, height=240,
        background="color", color=[0.2, 0.3, 0.4], style="studio", lighting=3,
    )  # fmt: skip
    assert ok.ok and ok.data["resolution"] == [320, 240] and not ok.warnings
    assert ok.data["path"] == "shots/a.png" and files.png_size(path) == (320, 240)
    builder = nx.captures[0]
    assert builder.dims == [240, 320] and builder.background_color == [0.2, 0.3, 0.4]
    assert builder.styled == 6 and nx.work_view.RenderingStyle == 0  # style put back
    lights = nx.lights[0]
    assert lights.committed == ["L3", "user"] and lights.destroyed


def test_fit_is_put_back_after_the_capture():
    nx = display_nx()
    ok = run(nx, "nx_capture_view", fit=True)
    assert ok.ok and nx.work_view.fits == 1
    assert ok.data["camera"]["scale"] == 2.5  # the captured, fitted camera
    assert nx.camera["scale"] == 1.0  # the kept camera was restored


def test_a_shown_sheet_is_captured_as_displayed():
    nx = display_nx()
    show_sheet(nx)
    ok = run(nx, "nx_capture_view", fit=True)
    assert ok.ok and ok.data["sheet"]["name"] == "SHT1" and "camera" not in ok.data
    assert nx.part.Views.WorkView.fitted
    bad = run(nx, "nx_capture_view", style="shaded")
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/style"
    assert bad.mutation_outcome == "not_started"


@pytest.mark.parametrize(
    "args,field",
    [
        ({"background": "color"}, "/color"),
        ({"background": "white", "color": [1, 0, 0]}, "/color"),
        ({"background": "black", "color": [1, 0, 0]}, "/color"),
        ({"color": [0, 300, 0]}, "/color"),
        ({"color": [-1, 0, 0]}, "/color"),
        ({"lighting": 2}, "/lighting"),
        ({"quality": "studio", "style": "wireframe"}, "/style"),
        ({"width": 64}, "/width"),
    ],
)
def test_bad_arguments_are_refused_before_nx(args, field):
    nx = display_nx()
    bad = run(nx, "nx_capture_view", **args)
    assert bad.mutation_outcome == "not_started" and bad.error["field"] == field
    assert not nx.captures


@pytest.mark.parametrize(
    ("args", "colour"),
    [
        ({"color": [0.2, 0.3, 0.4]}, [0.2, 0.3, 0.4]),  # a colour alone sets the background
        ({"background": "black"}, [0.0, 0.0, 0.0]),
        ({"color": [255, 0, 51]}, [1.0, 0.0, 0.2]),  # 0..255 components
        ({"background": "color", "color": [1, 1, 1]}, [1.0, 1.0, 1.0]),  # 0..1 when all <= 1
        ({}, [1.0, 1.0, 1.0]),  # white by default
    ],
)
def test_background_takes_natural_forms(args, colour):
    nx = display_nx()
    ok = run(nx, "nx_capture_view", **args)
    assert ok.ok, ok.error
    assert nx.captures[0].background_color == pytest.approx(colour)


def test_a_part_modified_before_the_capture_is_not_blamed_on_it():
    nx = display_nx()
    nx.part.IsModified = True
    ok = run(nx, "nx_capture_view")
    assert (
        ok.ok
        and ok.data["marks_part_modified"] is False
        and not [w for w in ok.warnings if w["code"] == "NX_UPDATE_WARNING"]
    )


def test_studio_wireframe_says_what_to_use_instead():
    bad = run(display_nx(), "nx_capture_view", quality="studio", style="wireframe")
    assert (
        bad.error["code"] == "NX_UNSUPPORTED_OPTION" and "quality=viewport" in bad.error["message"]
    )


def test_a_failed_capture_leaves_no_file_and_restores_the_view():
    nx = display_nx()
    nx.capture_error = NXException(3500001, "graphics failed")
    bad = run(nx, "nx_capture_view", fit=True, style="wireframe")
    assert bad.error["code"] == "NX_CAPTURE_FAILED" and bad.mutation_outcome == "rolled_back"
    assert nx.work_view.RenderingStyle == 0 and nx.camera["scale"] == 1.0
    assert not list(root(nx).rglob("*.png"))  # neither published nor left staged


def test_batch_nx_has_no_viewport():
    nx = display_nx()
    nx.session.IsBatch = True
    bad = run(nx, "nx_capture_view")
    assert bad.error["code"] == "NX_VIEWPORT_UNAVAILABLE"


def test_overwrite_replaces_an_existing_png_and_path_never_does():
    nx = display_nx()
    first = run(nx, "nx_capture_view", quality="studio", path="shots/o.png", width=320,
                height=240, overwrite=True)  # fmt: skip
    assert first.ok and first.data["overwritten"] is False  # nothing there yet: a new file
    again = run(nx, "nx_capture_view", path="shots/o.png")
    assert again.error["code"] == "NX_ALREADY_EXISTS" and again.error["field"] == "/path"
    assert again.mutation_outcome == "not_started"
    file = root(nx) / "shots" / "o.png"
    ok = run(nx, "nx_capture_view", quality="studio", path="shots/o.png", overwrite=True,
             width=640, height=480)  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "not_applicable" and ok.data["overwritten"] is True
    assert ok.data["path"] == "shots/o.png" and files.png_size(file) == (640, 480)
    assert ok.data["sha256"] == files.sha256(file)
    staging = root(nx) / ".nx-mcp" / "staging"
    assert not staging.exists() or not any(staging.rglob("*.png"))


@pytest.mark.parametrize(
    ("args", "code", "field"),
    [
        ({"overwrite": True}, "NX_INVALID_ARGUMENT", "/overwrite"),
        ({"path": "shots/text.png", "overwrite": True}, "NX_UNSUPPORTED_FILE_TYPE", "/path"),
        ({"path": "shots/text.png"}, "NX_ALREADY_EXISTS", "/path"),
        ({"path": "shots.txt", "overwrite": True}, "NX_UNSUPPORTED_FILE_TYPE", "/path"),
        ({"path": "dir.png", "overwrite": True}, "NX_WRONG_KIND", "/path"),
        ({"path": "../outside.png", "overwrite": True}, "NX_PATH_OUTSIDE_WORKSPACE", "/path"),
        ({"path": ".nx-mcp/x.png", "overwrite": True}, "NX_PATH_RESERVED", "/path"),
    ],
)
def test_overwrite_keeps_the_workspace_checks(args, code, field):
    nx = display_nx()
    text = root(nx) / "shots" / "text.png"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("not an image")
    (root(nx) / "dir.png").mkdir(exist_ok=True)
    bad = run(nx, "nx_capture_view", **args)
    assert bad.error["code"] == code and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and text.read_text() == "not an image"


def test_a_capture_that_marks_the_part_modified_says_so():
    nx = display_nx()
    plain = run(nx, "nx_capture_view", quality="studio", width=320, height=240)
    assert plain.data["marks_part_modified"] is False and not plain.warnings
    original = nx.part.Views.CreateLighting

    def lighting(arg):  # NX 2406 stores the lighting in the part: it is then modified
        lights = original(arg)
        commit = lights.Commit
        lights.Commit = lambda: (commit(), setattr(nx.part, "IsModified", True))
        return lights

    nx.part.Views.CreateLighting = lighting
    lit = run(nx, "nx_capture_view", quality="studio", width=320, height=240, lighting=2)
    assert lit.ok and lit.mutation_outcome == "not_applicable"
    assert lit.data["marks_part_modified"] is True
    assert [w["code"] for w in lit.warnings] == ["NX_UPDATE_WARNING"]
    assert "modified" in lit.warnings[0]["message"]


def test_a_capture_to_a_path_returns_no_inline_image_unless_asked():
    nx = display_nx()
    path = str(root(nx) / "shots" / "b.png")
    quiet = run(nx, "nx_capture_view", path=path)
    assert quiet.ok and quiet.data["image"]["inline"] is False and quiet.data["path"]
    loud = run(nx, "nx_capture_view", path=str(root(nx) / "shots" / "c.png"), inline=True)
    assert loud.data["image"]["inline"] is True
    bare = run(nx, "nx_capture_view", inline=False)  # no path: inline only when asked off
    assert bare.data["image"]["inline"] is False


def test_view_from_aims_the_capture_fits_and_puts_the_camera_back():
    nx = display_nx()
    ok = run(nx, "nx_capture_view", view_from=[-1, -1, 1])
    assert ok.ok and nx.work_view.fits == 1, ok
    cam = ok.data["camera"]
    third = 3**-0.5
    assert cam["view_from"] == pytest.approx([-third, -third, third])
    assert cam["up"][2] > 0 and cam["view_angle"] > 0
    assert nx.camera["scale"] == 1.0  # the engineer's camera came back
    still = run(nx, "nx_capture_view", view_from=[1, 0, 0], up=[0, 1, 0], fit=False)
    assert still.ok and nx.work_view.fits == 1  # fit=false keeps the scale
    assert still.data["camera"]["up"] == pytest.approx([0, 1, 0])


@pytest.mark.parametrize(
    ("args", "field"),
    [({"up": [0, 0, 1]}, "/up"), ({"view_from": [0, 0, 0]}, "/view_from")],
)
def test_bad_aim_is_refused_before_nx(args, field):
    nx = display_nx()
    bad = run(nx, "nx_capture_view", **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"
    assert nx.captures == []


def test_a_sheet_has_no_view_from():
    nx = display_nx()
    show_sheet(nx)
    bad = run(nx, "nx_capture_view", view_from=[1, 1, 1])
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/view_from"


def _clutter(nx):
    sketch, datum, hidden = Sketch("SKETCH_000"), Object("DATUM_PLANE"), Sketch("SKETCH_001")
    hidden.IsBlanked = True
    nx.part.Sketches.extend([sketch, hidden])
    nx.part.Datums.append(datum)
    nx.watch = [sketch, datum, hidden]
    return sketch, datum, hidden


def test_sketches_and_datums_are_hidden_for_the_capture_and_come_back():
    nx = display_nx()
    sketch, datum, hidden = _clutter(nx)
    ok = run(nx, "nx_capture_view")
    assert ok.ok and ok.data["hidden_sketches"] == 2 and ok.mutation_outcome == "not_applicable"
    assert nx.captures[0].blanked == [True, True, True]  # hidden while NX exported
    assert [sketch.IsBlanked, datum.IsBlanked, hidden.IsBlanked] == [False, False, True]
    assert ok.data["marks_part_modified"] is False and not [
        w for w in ok.warnings if w["code"] == "NX_UPDATE_WARNING"
    ]
    shown = run(nx, "nx_capture_view", hide_sketches=False)
    assert shown.data["hidden_sketches"] == 0 and nx.captures[1].blanked == [False, False, True]


def test_nothing_is_hidden_while_a_sketch_is_active():
    nx = display_nx()
    sketch, _datum, _hidden = _clutter(nx)
    nx.session.ActiveSketch = sketch
    ok = run(nx, "nx_capture_view")
    assert (
        ok.ok and ok.data["hidden_sketches"] == 0 and nx.captures[0].blanked == [False, False, True]
    )


def test_sheet_captures_a_sheet_of_a_part_showing_its_model_and_brings_the_model_back():
    nx = display_nx()
    sheet = nx.sheet("SHT1")
    model = run(nx, "nx_capture_view")
    assert model.ok and "camera" in model.data and "sheet" not in model.data
    note = next(w["message"] for w in model.warnings if w["code"] == "NX_NOTE")
    assert "drawing sheet(s) SHT1" in note and "pass sheet" in note
    ok = run(nx, "nx_capture_view", sheet="sht1")  # by name, any case
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    assert ok.data["sheet"]["name"] == "SHT1" and "camera" not in ok.data
    assert not [w for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert nx.part.DrawingSheets.CurrentDrawingSheet is None  # the model view is back
    assert not nx.drawing_work_view
    by_ref = run(nx, "nx_capture_view", sheet=nx.ref_of(sheet, "drawing_sheet"))
    assert by_ref.ok and by_ref.data["sheet"]["name"] == "SHT1"
    missing = run(nx, "nx_capture_view", sheet="SHT9")
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/sheet"
    assert missing.mutation_outcome == "not_started"
    styled = run(nx, "nx_capture_view", sheet="SHT1", style="shaded")
    assert styled.error["field"] == "/style"


def test_sheet_reopens_the_sheet_that_was_shown_before():
    nx = display_nx()
    first = show_sheet(nx, "SHT1")
    nx.sheet("SHT2")
    ok = run(nx, "nx_capture_view", sheet="SHT2")
    assert ok.ok and ok.data["sheet"]["name"] == "SHT2", ok
    assert nx.part.DrawingSheets.CurrentDrawingSheet is first


def test_a_named_work_part_that_is_not_displayed_is_shown_for_the_capture():
    """Live: beside the engineer's displayed part, the agent's part was
    NX's quiet work part; gate 4s switches no work part, so a capture with part=X wrote
    the engineer's view. X is now shown for the capture; the display and work part come
    back, and a fitted camera is put back in X before its tab is left."""
    from types import SimpleNamespace as NS

    from tests.kit.objects import Part

    nx = display_nx()
    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    parts = nx.session.Parts
    shown = parts.Display
    plate = Part(nx.session, root(nx) / "plate.prt")
    plate.ModelingViews, plate.Views = shown.ModelingViews, shown.Views
    parts.Display, parts.Work = shown, plate  # quietly the work part, not displayed
    seen = []
    commit = nx.part.Views.CreateImageExportBuilder

    def builder():
        b = commit()
        original = b.Commit
        b.Commit = lambda: (seen.append(parts.Display), original())[1]
        return b

    nx.part.Views.CreateImageExportBuilder = builder
    ok = run(nx, "nx_capture_view", part="plate", fit=True)
    assert ok.ok, ok
    assert seen == [plate] and ok.part["file"] == "plate.prt"
    assert parts.Display is shown and parts.Work is plate
    assert nx.work_view.fits == 1 and ok.data["camera"]["scale"] == 2.5
    assert nx.camera["scale"] == 1.0  # the kept camera was restored


def test_a_shown_topology_contour_is_reported_as_legend_only():
    """Live: outside the Topology Optimization task NX 2406 draws only a
    contour's legend (bands labelled 0, no body), and every export of the window shows
    just that; the capture says so instead of silently returning a blank model."""
    from types import SimpleNamespace as NS

    nx = display_nx()
    nx.part.PostManager = NS(GetContourLimits=lambda i: (0.0, 76.49, "MPa"))
    ok = run(nx, "nx_capture_view")
    assert ok.ok
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert len(notes) == 1 and "legend without the body" in notes[0] and "76.49" in notes[0]
    assert "nx_set_topology_contour with capture" in notes[0]

    def none(_index):
        raise NXException(1, "No contour is displayed.")

    nx.part.PostManager = NS(GetContourLimits=none)
    ok = run(nx, "nx_capture_view")
    assert ok.ok and not ok.warnings


def test_capture_lighting_leaves_hand_set_lights_alone():
    # Re-committing a UserDefined preset crashes NX's lighting builder (lights set by hand
    # with nx_set_light read UserDefined): the capture keeps the part's own lights.
    nx = display_nx()
    real = nx.part.Views.CreateLighting

    def user_defined(view):
        lights = real(view)
        lights.LightingCollectionType.UserDefined = "user"
        return lights

    nx.part.Views.CreateLighting = user_defined
    ok = run(nx, "nx_capture_view", quality="studio", style="studio", lighting=3)
    assert ok.ok
    lights = nx.lights[0]
    assert lights.committed == [] and lights.destroyed


def test_a_fitted_capture_frames_the_bodies_centred():
    nx = display_nx()
    nx.block(790, 400, 400, origin=(2200, -200, -200))
    nx.datum_plane()
    ok = run(nx, "nx_capture_view", fit=True)
    assert ok.ok, ok.error
    assert display.centre_offset(ok.data["camera"], [2595, 0, 0]) == pytest.approx(0, abs=1e-5)
    assert nx.camera["scale"] == 1.0  # the engineer's camera came back
