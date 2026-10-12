"""NX weight management of an assembly (Advanced Assemblies): asserted masses of bought-in
components and parts, and component groups for weight reports (``nx_measure_assembly_mass``
reads both).

NX 2406 findings (live, ``kit.massprops`` has the API details):

- UF ``AssertCompProps`` takes the centre of gravity in the owning assembly's frame and
  ``AskProps`` of the component returns it in the component's part frame, so only
  direct components of the work part are asserted as occurrences; ``level part``
  (UF ``AssertPartProps``, part frame) serves nested ones and every use of a part.
- Assertions, their deletion and component groups need ``adv_assemblies``: the license
  is checked (``CheckPresence``, a query) before any change. The kernel's undo mark
  rolls assertions and new groups back.
- Group names are unique without regard to case and may hold spaces; the four system
  groups (AllComponents, ...) are never listed by default nor redefined.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Name, Point3, Range
from nx_mcp.domains.assembly import common
from nx_mcp.kit import geom, readback, result, select
from nx_mcp.kit import massprops as mp
from nx_mcp.kit import parts as kparts
from nx_mcp.kit import units as kunits

MAX_MEMBERS = 200


class Previous(TypedDict):
    mass: float | None
    center_of_gravity: list[float] | None


class ComponentMassData(TypedDict):
    component: result.Row
    level: str
    part: str
    mass: float | None
    center_of_gravity: list[float] | None
    cleared: bool
    previous: Previous | None


def _assembly_center(tx, found, pose):
    """An asserted centre (part frame, mm) in assembly coordinates, part units."""
    if found is None or found["center"] is None:
        return None
    scale = 1.0 / kunits.MM_PER[kunits.part_units(tx)]
    return mp.to_assembly(pose, geom.scale(found["center"], scale))


def _summary(a, say) -> str:
    if a.get("clear"):
        return f"Clear asserted mass of {a.get('component')}"
    return f"Assert {a.get('mass')} kg on {a.get('component')}"


@tool(
    effect="model",
    label="Set component mass (weight management)",
    summary=_summary,
    keywords=(
        "assert mass",
        "assumed weight",
        "component weight",
        "weight management",
        "bought-in part mass",
        "asserted centre of gravity",
    ),  # fmt: skip
    requires=("work_part", "assembly", "no_active_sketch"),
    cost="light",
    refresh_annotations=False,
    example={"component": "o:12", "mass": 350, "center": [300, 0, 450]},
)
def nx_set_component_mass(
    tx: Tx,
    *,
    component: common.ComponentId,
    mass: Annotated[float, Range(gt=0, le=1e7), Doc("kg")] | None = None,
    center: Annotated[Point3, Doc("CoG, part units")] | None = None,
    frame: Literal["assembly", "component"] = "assembly",
    level: Literal["occurrence", "part"] = "occurrence",
    clear: bool = False,
) -> ComponentMassData:
    """Assert a component's mass and CoG (NX weight management), e.g. a bought-in engine;
    level part: all its uses; clear deletes it."""
    if clear and (mass is not None or center is not None):
        fail("NX_INVALID_ARGUMENT", "clear takes no mass or center", field="/clear")
    if not clear and mass is None:
        fail("NX_INVALID_ARGUMENT", "Give the mass to assert (or clear=true)", field="/mass")
    if frame == "component" and center is None:
        fail("NX_INVALID_ARGUMENT", "frame applies to center; give center", field="/frame")
    with tx.checks():
        if level == "occurrence":
            c = common.component(tx, component, "/component", direct=True)
        else:  # any level of the work part's assembly (it need not be displayed)
            c = select.resolve(tx, component, "component", field="/component")
            if not any(int(o.Tag) == int(c.Tag) for o, _names in kparts.components(tx.part)):
                fail("NX_WRONG_OWNER", f"{component} is not a component of the work part",
                     field="/component")  # fmt: skip
        prototype = getattr(c, "Prototype", None)
        if level == "part" and prototype is None:
            fail("NX_NOT_LOADED", f"The part of {component} is not loaded", field="/component")
        target = c if level == "occurrence" else prototype
        if level == "part":
            # Not tx.touch_part: a partially loaded prototype snapshots without its
            # features, so the kernel listed them as created (live NX 2406).
            tx.require_owned(prototype, field="/component")
        pose = mp.pose_of(c)
        before = mp.asserted(tx, target)
        previous: Previous | None = None
        if before is not None:
            previous = {"mass": before["mass"],
                        "center_of_gravity": _assembly_center(tx, before, pose)}  # fmt: skip
        mp.require_license(tx, what="NX weight management")
    row = tx.ref(c, "component")
    part_name = kparts.file_of(prototype).replace("\\", "/").rsplit("/", 1)[-1]
    if clear:
        if before is None:
            tx.warn("NX_NO_EFFECT", "No asserted mass to clear")
        else:
            mp.clear_weight(tx, target, level)
            readback.expect(mp.asserted(tx, target) is None, "assertion",
                            "NX kept the asserted mass")  # fmt: skip
            tx.modified(row)
        return {"component": row, "level": level, "part": part_name, "mass": None,
                "center_of_gravity": None, "cleared": before is not None,
                "previous": previous}  # fmt: skip
    assert mass is not None  # checked above
    wanted = None
    if center is not None:
        wanted = [float(v) for v in center]
        if frame == "component":
            wanted = mp.to_assembly(pose, wanted)
    mm = kunits.MM_PER[kunits.part_units(tx)]
    given = None
    if wanted is not None:
        given = wanted if level == "occurrence" else mp.to_local(pose, wanted)
        given = geom.scale(given, mm)
    mp.assert_weight(tx, target, level, mass, given)
    got = mp.asserted(tx, target)
    readback.expect(
        got is not None and got["mass"] is not None, "mass", "NX did not assert the mass"
    )
    assert got is not None and got["mass"] is not None
    readback.expect_close(got["mass"], float(mass), "mass", tol=1e-9 * max(1.0, float(mass)))
    center_back = _assembly_center(tx, got, pose)
    if wanted is not None:
        readback.expect(center_back is not None, "center", "NX did not assert the centre")
        readback.expect_close(center_back, wanted, "center")
    tx.modified(row)
    return {
        "component": row,
        "level": level,
        "part": part_name,
        "mass": float(got["mass"]),
        "center_of_gravity": center_back if wanted is not None else None,
        "cleared": False,
        "previous": previous,
    }


# ---------------------------------------------------------------------------
# Component groups


class GroupRow(TypedDict):
    name: str
    system: bool
    count: int
    members: list[result.Row]
    truncated: bool


class GroupData(TypedDict):
    group: GroupRow
    replaced: bool


class GroupList(TypedDict):
    items: list[GroupRow]


def _group_row(tx, group, members, with_members=True) -> GroupRow:
    shown = members[:MAX_MEMBERS] if with_members else []
    return {
        "name": str(group.Name),
        "system": mp.is_system_group(group.Name),
        "count": len(members),
        "members": [common.component_row(tx, c, with_pose=False) for c in shown],
        "truncated": with_members and len(members) > MAX_MEMBERS,
    }


def _descendants(component) -> list:
    out, stack = [], list(component.GetChildren())
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.GetChildren())
    return out


@tool(
    effect="model",
    label="Create component group",
    summary="Component group {name}",
    keywords=(
        "component group",
        "weight group",
        "system group",
        "group components",
        "advanced assemblies",
    ),  # fmt: skip
    requires=("work_part", "assembly", "no_active_sketch"),
    cost="light",
    refresh_annotations=False,
    example={"name": "PROPULSION", "components": ["o:12", "o:13"]},
)
def nx_create_component_group(
    tx: Tx,
    *,
    name: Name,
    components: Annotated[list[common.ComponentId], Items(1, 500)],
    children: bool = False,
    replace: Annotated[bool, Doc("Redefine a group of this name")] = False,
) -> GroupData:
    """Create an NX component group of these components (and children), e.g. a weight group
    for nx_measure_assembly_mass group_by component_group."""
    if mp.is_system_group(name):
        fail("NX_INVALID_ARGUMENT", f"{name} is an NX system group", field="/name")
    with tx.checks():
        part = tx.part
        members = {int(c.Tag) for c, _names in kparts.components(part)}
        chosen: list = []
        for i, ref in enumerate(components):
            c = select.resolve(tx, ref, "component", field=f"/components/{i}")
            if int(c.Tag) not in members:
                fail("NX_WRONG_OWNER", f"{ref} is not a component of the work part",
                     field=f"/components/{i}")  # fmt: skip
            if any(int(o.Tag) == int(c.Tag) for o in chosen):
                fail("NX_INVALID_ARGUMENT", f"{ref} is listed twice", field=f"/components/{i}")
            chosen.append(c)
        existing = mp.find_group(part, name)
        if existing is not None and not replace:
            fail("NX_ALREADY_EXISTS", f"A component group {existing.Name} already exists",
                 field="/name", hint="Set replace=true to redefine it")  # fmt: skip
        mp.require_license(tx, what="Component groups")
    with native(default="NX_BUILD_FAILED"):
        if existing is None:
            group = part.ComponentGroups.CreateComponentGroup(name)
        else:
            group = existing
            for c in mp.group_members(group):
                group.RemoveComponent(c)
        for c in chosen:
            group.AddComponent(c, bool(children))
    wanted = {int(c.Tag) for c in chosen}
    if children:
        wanted |= {int(d.Tag) for c in chosen for d in _descendants(c)}
    got = mp.group_members(group)
    readback.expect({int(c.Tag) for c in got} == wanted, "members",
                    "Component group members differ", count=len(got), wanted=len(wanted))  # fmt: skip
    return {"group": _group_row(tx, group, got), "replaced": existing is not None}


@tool(
    target="assembly",
    effect="read",
    label="List component groups",
    summary="Component groups",
    keywords=("component groups", "weight groups", "list groups", "group members"),
    requires=("work_part", "assembly"),
    cost="light",
    example={},
)
def nx_list_component_groups(
    tx: Tx,
    *,
    include_system: Annotated[bool, Doc("Also NX's AllComponents, CurrentComponents, ...")] = False,
    with_members: bool = True,
) -> GroupList:
    """List the NX component groups of the work part's assembly with their member components
    (at most 200 each)."""
    found = mp.groups(tx.part, include_system=include_system)
    return {"items": [_group_row(tx, g, members, with_members) for g, members in found]}
