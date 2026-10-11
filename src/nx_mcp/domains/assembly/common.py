"""Shapes and checks shared by the assembly tools (this domain only)."""

from __future__ import annotations

import contextlib
import typing
from typing import Annotated, Literal, TypedDict, cast

from nx_mcp.core.errors import fail, native
from nx_mcp.core.types import (
    Doc,
    Mat3,
    Name,
    Point3,
    Quantity,
    RefTo,
    Text,
    forbid_extra,
)
from nx_mcp.domains.assembly import structure
from nx_mcp.kit import display, files, geom, readback, result, select
from nx_mcp.kit import parts as kparts
from nx_mcp.kit import pose as kpose

# Undocumented items keep the listed schemas short; the field's own Doc names the units.
_Coordinate = Annotated[float | str, Quantity("length")]
_Row = tuple[float, float, float]
Translation = Annotated[
    tuple[_Coordinate, _Coordinate, _Coordinate], Doc("[x, y, z], part units or '10 mm'")
]
ComponentId = Annotated[str, RefTo("component", doc="Component id 'o:N' (nx_list_components)")]
ExplosionId = Annotated[str, RefTo("explosion", doc="Explosion id 'ex:N', not its name")]
_NAME_TEXT = next(m for m in typing.get_args(Name)[1:] if isinstance(m, Text))
ComponentName = Annotated[str, Doc("Component name (NX upper-cases it)"), _NAME_TEXT]
AXES = {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0]}


class AxisAngle(TypedDict):
    axis: Literal["x", "y", "z"]
    angle: Annotated[float | str, Quantity("angle"), Doc("Degrees or '1 rad'")]


Rotation = Annotated[
    tuple[_Row, _Row, _Row] | AxisAngle, Doc("Row-major 3x3 matrix, or {axis, angle}")
]


class _PoseRequired(TypedDict):
    translation: Translation


class ComponentPose(_PoseRequired, total=False):
    rotation: Rotation


forbid_extra(AxisAngle, ComponentPose)
# Total instances of one component pattern, including the seed.
MAX_INSTANCES = 100
OWNER_HINT = "Activate the subassembly that owns it with nx_activate_part"


class ComponentRow(result.Row, total=False):
    translation: Point3
    rotation: Mat3


def component_row(tx, component, names=None, *, with_pose=True) -> ComponentRow:
    """``{id, kind, name, part, path}`` (+ pose) of an occurrence."""
    row = cast(ComponentRow, tx.ref(component, "component"))  # gains the pose keys below
    if names is None:
        names = structure.paths(tx.part).get(int(component.Tag))
    if names:
        row["path"] = "/".join(names)
    if with_pose:
        row["translation"], row["rotation"] = structure.pose(component)
    return row


def rotation(value, field="/rotation") -> list:
    """Row-major rotation of a ``Rotation`` argument (a matrix, or an axis and an angle in
    degrees about it, right hand)."""
    if isinstance(value, dict):
        return kpose.axis_rotation(AXES[value["axis"]], float(value["angle"]))
    return geom.rotation(value, field)


def pose(value, field="/pose"):
    """``(t, R)`` floats of a ``ComponentPose`` argument (identity when omitted)."""
    if value is None:
        return [0.0, 0.0, 0.0], kpose.identity()
    rotation_value = value.get("rotation")
    r = rotation(rotation_value, field + "/rotation") if rotation_value is not None else None
    return [float(v) for v in value["translation"]], r if r is not None else kpose.identity()


def relative_path(tx, path) -> str:
    """A workspace-relative path for results ('' stays '')."""
    return files.relative(path, tx.workspace.root) if path else ""


def component(tx, ref, field, *, direct=True, unsuppressed=False):
    """A component of the work part's assembly; ``direct`` asks for a root child."""
    c = select.resolve(tx, ref, "component", field=field)
    part = tx.part
    if direct and not structure.is_direct_child(part, c):
        fail(
            "NX_WRONG_OWNER",
            f"{ref} is not a direct component of the work part",
            field=field,
            hint=OWNER_HINT,
        )
    if unsuppressed and c.IsSuppressed:
        fail("NX_INVALID_ARGUMENT", f"{ref} is suppressed; unsuppress it first", field=field)
    return c


def in_assembly(tx, ref, field):
    """An unsuppressed component (any level) of the displayed assembly, its part loaded."""
    c = select.resolve(tx, ref, "component", field=field)
    shown = tx.session.Parts.Display
    if not any(display.same(c, other) for other, _names in kparts.components(shown)):
        fail("NX_WRONG_OWNER", f"{ref} is not a component of the displayed assembly",
             field=field)  # fmt: skip
    if c.IsSuppressed:
        fail("NX_INVALID_ARGUMENT", f"{ref} is suppressed; unsuppress it first", field=field)
    if getattr(c, "Prototype", None) is None:
        fail("NX_NOT_LOADED", f"The part of {ref} is not loaded", field=field,
             hint="Load it with nx_open_part or nx_update_assembly")  # fmt: skip
    return c


def _set_work_component(tx, component) -> None:
    """``Parts.SetWorkComponent`` (None: the display part), keeping reference sets."""
    collection = tx.nx.PartCollection
    with native(default="NX_WRONG_PART_CONTEXT"):
        status = tx.session.Parts.SetWorkComponent(
            component, collection.RefsetOption.Current, collection.WorkComponentOption.Given
        )
    if status is not None and hasattr(status, "Dispose"):
        with contextlib.suppress(Exception):
            status.Dispose()


@contextlib.contextmanager
def working_in(tx, component):
    """Make ``component``'s part the work part (``component`` the work component) for the
    block, then restore the previous work part, also when the block fails (the kernel's
    undo mark then rolls the model back). Yields the component's part."""
    parts = tx.session.Parts
    before_part = parts.Work
    before_component = getattr(parts, "WorkComponent", None)
    _set_work_component(tx, component)
    readback.expect(
        display.same(getattr(parts, "WorkComponent", None), component), "work_component",
        "NX did not make the component the work component",
    )  # fmt: skip
    failed = True
    try:
        yield parts.Work
        failed = False
    finally:
        try:
            _set_work_component(tx, before_component)
            if not display.same(parts.Work, before_part):
                with native(default="NX_RESTORE_FAILED"):
                    parts.SetWork(before_part)
            readback.expect(display.same(parts.Work, before_part), "work_part",
                            "The work part was not restored")  # fmt: skip
        except Exception:
            if not failed:
                raise


def explosion_context(tx):
    """The work part, when it is also the display part (explosions are shown there)."""
    part = tx.part
    if not display.same(tx.session.Parts.Display, part):
        fail(
            "NX_WRONG_PART_CONTEXT",
            "Explosions need the assembly as both work and display part",
            hint="Activate it with nx_activate_part(work=true, display=true)",
        )
    return part


def explosion(tx, ref, field="/explosion"):
    obj = select.resolve(tx, ref, "explosion", field=field)
    if not display.same(getattr(obj, "OwningPart", None), tx.part):
        fail("NX_WRONG_OWNER", "The explosion belongs to another part", field=field)
    return obj
