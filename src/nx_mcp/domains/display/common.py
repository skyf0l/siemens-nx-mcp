"""Display-domain helpers: the objects a reference shows, record rows, camera rows."""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import Items, RefTo
from nx_mcp.kit import bodies, display, select
from nx_mcp.kit.result import Row

# Kinds a display tool accepts; features show their bodies, sketches their curves.
TARGET_KINDS = ("body", "face", "edge", "curve", "point", "sketch", "component", "feature", "datum")
Targets = Annotated[list[Annotated[str, RefTo(*TARGET_KINDS)]], Items(1, 1000)]


class Camera(TypedDict, total=False):
    view: str
    view_angle: float
    rotation: list[list[float]]
    view_from: list[float]
    up: list[float]
    right: list[float]
    origin: list[float]
    absolute_origin: list[float]
    scale: float
    style: str


class DisplayRow(TypedDict, total=False):
    object: Row
    blanked: bool
    color: int
    transparency: int | None


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


def targets(tx, refs, *, field="/objects", expand=False) -> list:
    """Displayable objects of ``refs`` (unique by tag, in order).

    Features expand to their bodies and sketches to their curves; with ``expand``
    components expand to the occurrence bodies they show.
    """
    found: dict = {}
    for i, ref in enumerate(refs):
        obj = select.resolve(tx, ref, *TARGET_KINDS, field=f"{field}/{i}")
        for value in _shown(tx, obj, kind_of(tx, obj), expand):
            found.setdefault(int(value.Tag), value)
    if not found:
        fail("NX_WRONG_GEOMETRY", "The selection shows nothing (no bodies or curves)", field=field)
    if len(found) > display.MAX_RECORDS:
        fail("NX_TOO_MANY_OBJECTS", f"More than {display.MAX_RECORDS} objects", field=field)
    return list(found.values())


def display_rows(tx, pairs) -> list[DisplayRow]:
    """``[{object, blanked, color?, transparency?}]`` for ``(obj, record)`` pairs."""
    out: list[DisplayRow] = []
    for obj, record in pairs:
        row: DisplayRow = {
            "object": tx.ref(obj, kind_of(tx, obj)),
            "blanked": bool(record["blanked"]),
        }
        if "color_index" in record:
            row["color"] = int(record["color_index"])
            transparency = record.get("transparency")
            row["transparency"] = None if transparency is None else int(transparency)
        out.append(row)
    return out


def camera_row(cam: dict, *, named: bool = True) -> Camera:
    """The surface fields of a camera (``kit.display.camera``); ``view_from``, ``up`` and
    ``right`` are the rotation's columns (world unit vectors). ``view`` is the canned
    orientation nearest in look-from direction and ``view_angle`` the degrees between the
    two directions (``named``), else NX's view name (a drawing sheet's view)."""
    axes = display.view_axes(cam["rotation"])
    row: Camera = {"view": cam["view"]}
    if named:
        row["view"], row["view_angle"] = display.nearest_view(cam["rotation"])
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
