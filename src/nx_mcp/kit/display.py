"""Display state of the displayed part: camera, rendering style, appearance, datums,
dynamic sections, and the display snapshots behind ``restore_id``.

    part = display.shown_part(tx, work=True)        # the displayed part (= work part)
    view = display.model_view(tx, part)             # refuses while a drawing sheet is shown
    cam = display.camera(view)                      # {view, rotation, origin, absolute_origin,
                                                    #  scale, style}
    display.set_camera(tx, view, rotation, origin, scale)   # read back
    display.apply_appearance(tx, objects, color=186, transparency=40)
    rid = display.snapshots.save(part_key, records, camera)

NX 2406 findings encoded here:

- ``view.Origin`` is NX's native view origin, the value
  ``SetRotationTranslationScale`` takes back: the negated view-space coordinates of
  the point shown at the viewport centre. It round-trips exactly; callers pass the
  value ``camera()`` reported.
- ``rotation`` is row-major; its columns are the view axes (screen right, screen up,
  toward the viewer) in display-part coordinates (``geom.rows``/``builders.matrix``):
  ``rotation[i][j]`` is world component i of view axis j. The third column is the
  direction the camera looks *from* (``view_axes``). NX's native ``view.Matrix`` rows
  are those axes, so a matrix written as rows of view axes is the transpose.
- Canned orientations (``ORIENTATION_FROM``): top looks from +Z (screen up +Y),
  bottom from -Z (up -Y), front from -Y, back from +Y, right from +X, left from -X
  (up +Z), isometric from (1,-1,1)/sqrt3, trimetric from about (0.72,-0.59,0.38).
  NX's ``view.Name`` stays the layout view's name (``Trimetric``) whatever the
  camera, so ``nearest_view`` names the canned orientation closest to a rotation.
- Blanking a sketch or datum marks the part modified; undoing to a mark set before
  restores both the blank state and the modified flag (``capture_clutter``).
- ``DeleteSections`` leaves ``DisplaySectioningToggle`` on with no section left (the
  model looks cut by nothing): sectioning is switched off when no active section remains.
- Opening a ``DynamicSectionBuilder`` on an existing section to read it changes nothing.
- Display snapshots are restored newest first per part (a show restored before the
  hide it followed would hide the object again).

The snapshot store lives here, not in a domain: the host's services write it
(``save_display_snapshot``) and ``nx_restore_display`` reads it.
"""

from __future__ import annotations

import math
import uuid

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import builders, drawing, geom, readback

# Rendering styles by surface name (NXOpen.View.RenderingStyleType members).
STYLES = {
    "shaded": "Shaded",
    "shaded_with_edges": "ShadedWithEdges",
    "wireframe": "StaticWireframe",
    "studio": "Studio",
}
# Canned orientations (NXOpen.View.Canned members).
ORIENTATIONS = {
    "top": "Top",
    "front": "Front",
    "right": "Right",
    "back": "Back",
    "left": "Left",
    "bottom": "Bottom",
    "isometric": "Isometric",
    "trimetric": "Trimetric",
}
# Rotation each canned orientation gives (NX 2406 read-back; rows as ``camera()``).
_R2, _R3, _R6 = 0.5**0.5, 3**-0.5, 6**-0.5
ORIENTATION_ROTATIONS = {
    "top": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    "front": [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]],
    "right": [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    "back": [[-1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]],
    "left": [[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    "bottom": [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]],
    "isometric": [[_R2, -_R6, _R3], [_R2, _R6, -_R3], [0.0, 2 * _R6, _R3]],
    "trimetric": [
        [0.631219059, -0.291176882, 0.718873093],
        [0.775604603, 0.236971772, -0.585048613],
        [0.0, 0.926855114, 0.375419229],
    ],
}
# Direction each canned orientation looks from and its screen up (world axes).
ORIENTATION_FROM = {
    name: ([r[2] for r in rot], [r[1] for r in rot]) for name, rot in ORIENTATION_ROTATIONS.items()
}
# Named colours -> UF_DISP colour names (closest colour in the part's colour table).
COLOR_NAMES = {
    "red": "RED_NAME",
    "green": "GREEN_NAME",
    "blue": "BLUE_NAME",
    "yellow": "YELLOW_NAME",
    "cyan": "CYAN_NAME",
    "magenta": "MAGENTA_NAME",
    "orange": "ORANGE_NAME",
    "white": "WHITE_NAME",
    "black": "BLACK_NAME",
    "gray": "MEDIUM_GRAY_NAME",
}
MAX_RECORDS = 10000  # objects and faces one display change may touch


# ---- parts and views ------------------------------------------------------------------------


def same(a, b) -> bool:
    return a is not None and b is not None and int(a.Tag) == int(b.Tag)


def shown_part(tx, *, work: bool = False):
    """The displayed part; with ``work`` it must also be the work part."""
    part = tx.session.Parts.Display
    if part is None:
        fail("NX_NO_WORK_PART", "No part is displayed", hint="Open a part with nx_open_part")
    if work and not same(part, tx.session.Parts.Work):
        fail(
            "NX_WRONG_PART_CONTEXT",
            "The displayed part is not the work part",
            hint="Make it both work and display part with nx_activate_part",
        )
    return part


def sheet_shown(part) -> bool:
    """True when NX shows a drawing sheet of ``part`` instead of the model view."""
    return drawing.current(part) is not None or drawing.work_view_is_drawing(part)


def model_view(tx, part):
    """The model work view of ``part``; refused while a drawing sheet is shown."""
    if sheet_shown(part):
        fail(
            "NX_WRONG_APPLICATION",
            "A drawing sheet is shown, so there is no model view",
            hint="Return to the model with nx_activate_drawing_sheet without a sheet",
        )
    return part.ModelingViews.WorkView


def style_name(tx, value) -> str:
    """Surface name of a native rendering style (``nx_<value>`` when unnamed here)."""
    enum = tx.nx.View.RenderingStyleType
    for name, member in STYLES.items():
        native_member = getattr(enum, member, None)
        if native_member is not None and (
            native_member == value or str(native_member) == str(value)
        ):
            return name
    return "nx_" + str(value)


def set_style(tx, view, style: str) -> None:
    """Set the rendering style by surface name."""
    with native(default="NX_ERROR"):
        view.RenderingStyle = getattr(tx.nx.View.RenderingStyleType, STYLES[style])


def camera(tx, view) -> dict:
    """``{view, rotation, origin, absolute_origin, scale, style}`` of a view.

    A drawing sheet's view has no rendering style (``style`` is ``drawing``)."""
    try:
        style = style_name(tx, view.RenderingStyle)
    except Exception:  # NX: "Object is not a valid view or not valid for this operation"
        style = "drawing"
    try:
        absolute = [float(v) for v in geom.xyz(view.AbsoluteOrigin)]
    except Exception:
        absolute = [float(v) for v in geom.xyz(view.Origin)]
    return {
        "view": str(view.Name),
        "rotation": [[float(v) for v in row] for row in geom.rows(view.Matrix)],
        "origin": [float(v) for v in geom.xyz(view.Origin)],
        "absolute_origin": absolute,
        "scale": float(view.Scale),
        "style": style,
    }


def view_axes(rotation) -> dict:
    """``{right, up, view_from}`` world unit vectors: the columns of a camera rotation."""
    return {
        name: [float(rotation[i][j]) for i in range(3)]
        for j, name in enumerate(("right", "up", "view_from"))
    }


def rotation_from(view_from, up=None, field="/view_from") -> list[list[float]]:
    """The camera rotation looking from ``view_from`` (world direction from the model to
    the camera) with ``up`` as near screen up as possible (default world +Z, or +Y when
    looking along Z). ``up`` parallel to ``view_from`` is ``invalid_argument``."""
    z = geom.unit(view_from, field=field)
    if up is None:
        up = [0.0, 1.0, 0.0] if abs(z[2]) > 1 - 1e-9 else [0.0, 0.0, 1.0]
    u = geom.unit(up, field="/up")
    x = geom.cross(u, z)
    if geom.norm(x) < 1e-9:
        fail("NX_INVALID_ARGUMENT", "up must not be parallel to view_from", field="/up")
    x = geom.unit(x)
    y = geom.cross(z, x)
    return [[x[i], y[i], z[i]] for i in range(3)]


def nearest_view(rotation) -> tuple[str, float]:
    """``(name, degrees)``: the canned orientation whose look-from direction is closest to
    the camera's and the angle between the two directions (0 when the camera looks from
    there, whatever its roll). Ties (e.g. (-1,-1,1) is 54.7 degrees from front, left and
    top) go to the smallest whole rotation, then the order of ``ORIENTATION_ROTATIONS``."""
    keys = []
    look = [rotation[i][2] for i in range(3)]
    for order, (name, rot) in enumerate(ORIENTATION_ROTATIONS.items()):
        cosine = sum(look[i] * rot[i][2] for i in range(3))
        direction = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
        trace = sum(rotation[i][j] * rot[i][j] for i in range(3) for j in range(3))
        whole = math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))
        keys.append((round(direction, 6), round(whole, 6), order, name))
    direction, _whole, _order, name = min(keys)
    return name, round(direction, 3)


def expect_camera(got: dict, rotation=None, origin=None, scale=None, check="camera") -> None:
    """Read-back of a camera against the requested values (those given)."""
    if rotation is not None:
        flat = [v for row in got["rotation"] for v in row]
        readback.expect_close(flat, [v for row in rotation for v in row], check + ".rotation", 1e-6)
    if origin is not None:
        readback.expect_close(got["origin"], origin, check + ".origin")
    if scale is not None:
        readback.expect_close(got["scale"], scale, check + ".scale", abs(scale) * 1e-6)


def set_camera(tx, view, rotation, origin, scale) -> dict:
    """Set an explicit camera, update the display and read it back."""
    with native(default="NX_ERROR"):
        view.SetRotationTranslationScale(
            builders.matrix(tx, rotation), tx.nx.Point3d(*(float(v) for v in origin)), float(scale)
        )
        view.UpdateDisplay()
    got = camera(tx, view)
    expect_camera(got, rotation, origin, scale)
    return got


def orient(tx, view, orientation: str, *, fit: bool) -> None:
    """A canned orientation; the scale is kept unless ``fit``."""
    adjust = tx.nx.View.ScaleAdjustment
    with native(default="NX_ERROR"):
        view.Orient(
            getattr(tx.nx.View.Canned, ORIENTATIONS[orientation]),
            adjust.Fit if fit else adjust.Current,
        )
        view.UpdateDisplay()


# ---- appearance -----------------------------------------------------------------------------


def color_index(tx, name: str) -> int:
    """The part colour-table index closest to a named colour."""
    disp = tx.uf.Disp
    with native(default="NX_ERROR"):
        return int(disp.AskClosestColorInDisplayedPart(getattr(disp.ColorName, COLOR_NAMES[name])))


def apply_appearance(tx, objects, color=None, transparency=None) -> None:
    """Colour index and/or translucency (0 opaque .. 100) on displayable objects.

    Only the objects themselves change (``ApplyToOwningParts`` off); a body passes
    the change on to all its faces.
    """
    modification = tx.session.DisplayManager.NewDisplayModification()
    try:
        with native(default="NX_ERROR"):
            modification.ApplyToOwningParts = False
            modification.ApplyToAllFaces = True
            if color is not None:
                modification.NewColor = int(color)
            if transparency is not None:
                modification.NewTranslucency = int(transparency)
            modification.Apply(list(objects))
    finally:
        modification.Dispose()


def translucency(tx, obj):
    """Translucency 0..100 of a face or body (None where NX does not report it)."""
    try:
        return int(tx.uf.Obj.AskTranslucency(obj.Tag))
    except Exception:
        return None


# ---- objects shown ---------------------------------------------------------------------------


def datums(part) -> list:
    """Datums and coordinate systems owned by ``part`` (unique by tag)."""
    values = list(getattr(part, "Datums", []) or []) + list(
        getattr(part, "CoordinateSystems", []) or []
    )
    return list({int(x.Tag): x for x in values}.values())


def capture_clutter(tx, part) -> list:
    """Shown finished sketches and datums of ``part`` (none while a sketch is active:
    hiding is then skipped, the edited sketch stays as it is)."""
    if getattr(tx.session, "ActiveSketch", None) is not None:
        return []
    values = list(getattr(part, "Sketches", []) or []) + datums(part)
    return [v for v in values if not bool(getattr(v, "IsBlanked", True))]


# ---- dynamic sections -----------------------------------------------------------------------


def sections(part) -> list:
    return list(getattr(part, "DynamicSections", []) or [])


def section_state(tx, part, view, section) -> dict:
    """``{origin, normal, cap, clip, active, name}`` of one section (read through a builder
    that is never committed)."""
    with builders.build(tx, part.DynamicSections.CreateSectionBuilder, section, view) as b:
        state = {
            "origin": [float(v) for v in geom.xyz(b.GetOrigin())],
            "normal": [float(v) for v in geom.xyz(b.GetNormal())],
            "cap": bool(b.ShowCap),
            "clip": bool(b.ShowClip),
        }
    state["active"] = same(view.ActiveDynamicSection, section)
    state["name"] = str(getattr(section, "Name", "") or "")
    return state


def write_section(tx, part, view, *, section=None, origin=None, normal=None, cap=None, name=None):
    """Create (``section=None``) or edit a one-plane section and commit it; returns it.

    Absolute coordinates; the material on the normal's positive side is cut away."""
    types = tx.nx.Display.DynamicSectionTypes
    factory = part.DynamicSections.CreateSectionBuilder
    args = (section, view) if section is not None else (view,)
    with builders.build(tx, factory, *args) as b:
        if section is None:
            b.Type = types.Type.OnePlane
            b.CsysType = types.CoordinateSystem.Absolute
            b.ClipType = types.Clip.Section
            b.ShowClip = True
            b.ShowViewer = False
            b.ShowGrid = False
        if name is not None:
            b.SetName(str(name))
        if normal is not None:
            b.SetNormal(tx.nx.Vector3d(*(float(v) for v in normal)))
        if origin is not None:
            b.SetOrigin(tx.nx.Point3d(*(float(v) for v in origin)))
        if cap is not None:
            b.ShowCap = bool(cap)
        made = b.commit()
    return made if made is not None else section


def set_sectioning(view, section, enabled: bool) -> None:
    """Make ``section`` the active one and switch view sectioning on, or switch it off."""
    if enabled:
        view.ActiveDynamicSection = section
        view.DisplaySectioningToggle = True
    elif same(view.ActiveDynamicSection, section):
        view.DisplaySectioningToggle = False


def delete_sections(tx, part, view, targets) -> None:
    """Delete sections; sectioning goes off when no (active) section remains (NX 2406)."""
    with native(default="NX_ERROR"):
        part.DynamicSections.DeleteSections(False, list(targets))
        if not sections(part) or not view.ActiveDynamicSection:
            view.DisplaySectioningToggle = False


# ---- display snapshots (restore_id) ----------------------------------------------------------


class Snapshots:
    """Display snapshots by ``restore_id``, newest last; restored newest first per part.

    A snapshot holds ``records`` (display records whose ``object`` names a reference,
    blank state, ``color_index``, ``transparency``) and an optional camera. Snapshots
    live for the NX host's lifetime (a reload or new session forgets them).
    """

    LIMIT = 200

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def save(self, part_key: str, records, camera=None) -> str:
        token = "display_" + uuid.uuid4().hex
        self.items[token] = {"part": part_key, "records": list(records), "camera": camera}
        while len(self.items) > self.LIMIT:
            self.items.pop(next(iter(self.items)))
        return token

    def get(self, restore_id: str, part_key: str) -> dict:
        """The snapshot to restore now, or a stable error (stale, other part, order)."""
        snapshot = self.items.get(restore_id)
        if snapshot is None:
            fail(
                "NX_STALE_REFERENCE",
                "Unknown or already restored restore_id",
                field="/restore_id",
                hint="restore_id values live until restored or until NX restarts",
            )
        if snapshot["part"] != part_key:
            fail(
                "NX_WRONG_PART_CONTEXT",
                "The snapshot belongs to another part",
                field="/restore_id",
                hint="Activate the part it was taken in with nx_activate_part",
            )
        newest = self.latest(part_key)
        if newest != restore_id:
            fail(
                "NX_DEPENDENCY_ORDER",
                "Restore display changes in reverse order: a newer change comes first",
                field="/restore_id",
                details={"restore_first": newest},
            )
        return snapshot

    def latest(self, part_key: str) -> str | None:
        return next((k for k in reversed(self.items) if self.items[k]["part"] == part_key), None)

    def drop(self, restore_id: str) -> None:
        self.items.pop(restore_id, None)

    def clear(self) -> None:
        self.items.clear()


snapshots = Snapshots()


def record_id(record: dict) -> str | None:
    """The reference a display record names (``record["object"]["id"]``)."""
    obj = record.get("object")
    return obj.get("id") if isinstance(obj, dict) else obj if isinstance(obj, str) else None


def state_of(record: dict) -> dict:
    """The comparable display state of a record: blanked, colour, transparency."""
    return {k: record[k] for k in ("blanked", "color_index", "transparency") if k in record}
