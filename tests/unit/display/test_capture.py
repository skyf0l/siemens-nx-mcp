"""nx_capture_view: viewport and Studio PNGs, inline image metadata, sheets, cleanup."""

from pathlib import Path

import pytest

from nx_mcp.kit import files
from tests.kit import NXException, run
from tests.kit.kernel import services
from tests.kit.objects import Object, Sketch
from tests.unit.display.fakes import WINDOW, display_nx, show_sheet


def root(nx) -> Path:
    return services(nx).root


def test_viewport_capture_reports_the_window_size_and_an_inline_image():
    nx = display_nx()
    ok = run(nx, "nx_capture_view", width=800, height=500)
    assert ok.ok and ok.mutation_outcome == "not_applicable"  # a view-only action
    data = ok.data
    assert data["requested"] == [800, 500] and data["resolution"] == list(WINDOW)
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
    assert [w["code"] for w in ok.warnings] == ["NX_APPROXIMATED"]
    builder = nx.captures[0]
    assert builder.kind == "viewport" and builder.destroyed
    assert builder.background_color == [1.0, 1.0, 1.0] and builder.EnhanceEdges is True
    assert data["camera"]["view"] == "trimetric" and "restore_id" not in ok.envelope
    assert data["hidden_sketches"] == 0 and data["marks_part_modified"] is False


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
