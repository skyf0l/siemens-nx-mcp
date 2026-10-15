"""Display-domain helpers: the objects a reference shows, record rows, camera rows."""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import Items, RefTo, Text
from nx_mcp.kit import bodies, display, select
from nx_mcp.kit.args import choice as choice  # natural option spellings
from nx_mcp.kit.result import Row

# Kinds a display tool accepts; features show their bodies, sketches their curves.
TARGET_KINDS = ("body", "face", "edge", "curve", "point", "sketch", "component", "feature", "datum")
Targets = Annotated[list[Annotated[str, RefTo(*TARGET_KINDS)]], Items(1, 1000)]

# A free spelling next to a Literal (``Literal[...] | Loose``): the schema lists the
# canonical values and ``choice`` maps 'Hide', ' RED ' or 'grey' onto them.
Loose = Annotated[str, Text(32)]


# A camera within this many degrees of a canned look-from direction is named after it;
# farther off, ``view`` is ``custom`` and ``nearest_view`` names the closest one.
NAMED_WITHIN = 10.0


class Camera(TypedDict, total=False):
    view: str
    nearest_view: str
    view_angle: float
    rotation: list[list[float]]
    view_from: list[float]
    up: list[float]
    right: list[float]
    origin: list[float]
    absolute_origin: list[float]
    scale: float
    style: str


class FaceRow(TypedDict):
    object: Row
    color: int
    transparency: int | None


class DisplayRow(TypedDict, total=False):
    object: Row
    blanked: bool
    color: int
    color_name: str
    color_label: str
    color_hex: str
    transparency: int | None
    faces_differ: int
    faces: list[FaceRow]


def kind_of(tx, obj) -> str:
    """The reference kind of a displayable object (curves are not in the host's table)."""
    try:
        return tx.kind_of(obj)
    except NXError:
        if hasattr(obj, "FindOccurrence"):
            return "component"
        if hasattr(obj, "IsSolidBody"):
            return "body"
        return "curve"


def _shown(tx, obj, kind, expand):
    if kind == "feature" or (kind == "component" and expand):
        return bodies.of(tx, obj)
    if kind == "sketch":
        return list(obj.GetAllGeometry())
    return [obj]


def targets(tx, refs, *, field="/objects", expand=False, kinds=TARGET_KINDS) -> list:
    """Displayable objects of ``refs`` (unique by tag, in order).

    Features expand to their bodies and sketches to their curves; with ``expand``
    components expand to the occurrence bodies they show. ``kinds``: the reference kinds
    the tool takes (a wrong kind's error lists these).
    """
    found: dict = {}
    for i, ref in enumerate(refs):
        obj = select.resolve(tx, ref, *kinds, field=f"{field}/{i}")
        for value in _shown(tx, obj, kind_of(tx, obj), expand):
            found.setdefault(int(value.Tag), value)
    if not found:
        fail("NX_WRONG_GEOMETRY", "The selection shows nothing (no bodies or curves)", field=field)
    if len(found) > display.MAX_RECORDS:
        fail("NX_TOO_MANY_OBJECTS", f"More than {display.MAX_RECORDS} objects", field=field)
    return list(found.values())


def faces_of(obj) -> list:
    """The faces of a body (``[]`` for anything else)."""
    return list(obj.GetFaces()) if hasattr(obj, "IsSolidBody") else []


def color_names(tx) -> dict[int, str]:
    """``{index: name}`` of the named colours in the displayed part's colour table (the
    index ``nx_set_appearance`` uses for each name)."""
    out: dict[int, str] = {}
    for name in display.COLOR_NAMES:
        try:
            out.setdefault(display.color_index(tx, name), name)
        except Exception:  # noqa: BLE001 - a colour NX cannot name is just not named
            continue
    return out


def display_rows(tx, pairs) -> list[DisplayRow]:
    """``[{object, blanked, color?, color_name?, color_label?, color_hex?, transparency?,
    faces_differ?, faces?}]`` for ``(obj, record)`` pairs: one row per object, a body's
    faces only as ``faces_differ`` (how many faces have another colour or transparency than
    the body) and the first ``display.FACE_ROWS`` of them in ``faces``. ``color_name`` is
    given where the index is a named colour, ``color_label`` and ``color_hex`` from the
    part's colour table; a body's transparency is None when its faces disagree."""
    records = {int(obj.Tag): record for obj, record in pairs}
    coloured = any("color_index" in r for r in records.values())
    names = color_names(tx) if coloured else {}
    table = _palette(tx) if coloured else {}
    out: list[DisplayRow] = []
    for obj, record in pairs:
        row: DisplayRow = {
            "object": tx.ref(obj, kind_of(tx, obj)),
            "blanked": bool(record["blanked"]),
        }
        if "color_index" in record:
            row["color"] = int(record["color_index"])
            if row["color"] in names:
                row["color_name"] = names[row["color"]]
            if row["color"] in table:
                label, rgb = table[row["color"]]
                row["color_label"], row["color_hex"] = label, display.hex_of(rgb)
            transparency = record.get("transparency")
            if transparency is None and "faces" not in record:
                transparency = _faces_transparency(obj, records)
            odd = record.get("faces") or []
            if any(f.get("transparency") != transparency for f in odd):
                transparency = None  # the faces disagree
            row["transparency"] = None if transparency is None else int(transparency)
            if odd:
                row["faces_differ"] = len(odd)
                row["faces"] = _face_rows(tx, odd[: display.FACE_ROWS])
        out.append(row)
    return out


def _palette(tx) -> dict:
    """The displayed part's colour table (``{}`` when NX does not report it)."""
    try:
        part = tx.session.Parts.Display
        return display.palette(part) if part is not None else {}
    except Exception:  # noqa: BLE001 - rows then carry the index alone
        return {}


def _face_rows(tx, odd) -> list[FaceRow]:
    """Rows of the faces a body record lists (faces the model lost are left out)."""
    rows: list[FaceRow] = []
    for item in odd:
        try:
            face = select.resolve(tx, display.record_id(item), "face")
        except NXError:
            continue
        shade = item.get("transparency")
        rows.append({"object": tx.ref(face, "face"), "color": int(item["color_index"]),
                     "transparency": None if shade is None else int(shade)})  # fmt: skip
    return rows


def _faces_transparency(obj, records):
    """The one transparency all faces of a body share, else None."""
    seen = {records[int(f.Tag)].get("transparency") for f in faces_of(obj) if int(f.Tag) in records}
    return seen.pop() if len(seen) == 1 else None


def camera_row(cam: dict, *, named: bool = True) -> Camera:
    """The surface fields of a camera (``kit.display.camera``); ``view_from``, ``up`` and
    ``right`` are the rotation's columns (world unit vectors). With ``named``, ``view`` is
    the canned orientation nearest in look-from direction when it is within
    ``NAMED_WITHIN`` degrees, else ``custom`` with ``nearest_view``; ``view_angle`` is the
    degrees between the two directions. Without, ``view`` is NX's view name (a sheet's)."""
    axes = display.view_axes(cam["rotation"])
    row: Camera = {"view": cam["view"]}
    if named:
        nearest, angle = display.nearest_view(cam["rotation"])
        if angle <= NAMED_WITHIN:
            row["view"] = nearest
        else:
            row["view"], row["nearest_view"] = "custom", nearest
        row["view_angle"] = angle
    row.update(
        {
            "rotation": cam["rotation"],
            "view_from": axes["view_from"],
            "up": axes["up"],
            "right": axes["right"],
            "origin": cam["origin"],
            "absolute_origin": cam["absolute_origin"],
            "scale": cam["scale"],
            "style": cam["style"],
        }
    )
    return row
