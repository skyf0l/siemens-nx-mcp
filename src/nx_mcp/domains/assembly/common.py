"""Shapes and checks shared by the assembly tools (this domain only)."""

from __future__ import annotations

from typing import Annotated, cast

from nx_mcp.core.errors import fail
from nx_mcp.core.types import Doc, Mat3, Point3
from nx_mcp.domains.assembly import structure
from nx_mcp.kit import display, files, result, select

Translation = Annotated[tuple[float, float, float], Doc("[x, y, z] in part units")]
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
