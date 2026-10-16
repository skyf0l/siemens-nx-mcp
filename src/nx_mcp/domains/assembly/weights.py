"""NX weight management of an assembly (Advanced Assemblies): asserted masses of bought-in
components and parts, and component groups for weight reports (``nx_measure_assembly_mass``
reads both).

NX 2406 findings (live, ``kit.massprops`` has the API details):

- UF ``AssertCompProps`` takes the centre of gravity in the owning assembly's frame and
  ``AskProps`` of the component returns it in the component's part frame, so only
  direct components of the work part are asserted as occurrences; ``level part``
  (UF ``AssertPartProps``, part frame) serves nested ones and every use of a part.
- Assertions, their deletion and component groups need ``adv_assemblies``: NX's license
  refusal is reported as ``NX_LICENSE_UNAVAILABLE`` naming it (``kit.licenses.attempt``).
  The kernel's undo mark rolls assertions and new groups back.
- Group names are unique without regard to case and may hold spaces; the four system
  groups (AllComponents, ...) are never listed by default nor redefined.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Name, Point3, Range
from nx_mcp.domains.assembly import common
from nx_mcp.kit import geom, model, readback, result, select
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
    level part: all its uses; center omitted keeps an asserted CoG; clear deletes it."""
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
    row = tx.ref(c, "component")
    part_name = kparts.file_of(prototype).replace("\\", "/").rsplit("/", 1)[-1]
    if clear:
        if before is None:
            held = None
            if level == "occurrence" and prototype is not None:
                held = mp.asserted(tx, prototype)
            if held is not None:
                mass_text = f" ({held['mass']:g} kg)" if held["mass"] is not None else ""
                tx.warn("NX_NO_EFFECT", f"{c.Name} has no occurrence assertion to clear; its "
                        f"part {part_name} carries a part-level one{mass_text}: pass "
                        "level='part' to clear it (for every use of the part)")  # fmt: skip
            else:
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
    elif previous is not None and previous["center_of_gravity"] is not None:
        wanted = list(previous["center_of_gravity"])  # a new mass keeps the asserted CoG
        tx.warn("NX_NOTE", f"The asserted centre of gravity {wanted} was kept; pass center "
                "to move it")  # fmt: skip
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
    count: int | None  # None: a system group NX did not evaluate for this assembly
    members: list[result.Row]
    truncated: bool


class GroupData(TypedDict):
    group: GroupRow
    replaced: bool


class GroupList(TypedDict):
    items: list[GroupRow]


class GroupEditData(TypedDict, total=False):
    group: GroupRow
    previous_name: str
    deleted: str
    remaining: list[str]


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
                 field="/name", details={"existing": str(existing.Name)},
                 hint="Set replace=true to redefine it")  # fmt: skip
    replaced_members = (
        [tx.ref(c, "component")["id"] for c in mp.group_members(existing)]
        if existing is not None
        else None
    )
    with mp.licensed(tx), native(default="NX_BUILD_FAILED"):
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
    tx.note_restore({
        "group": str(group.Name),
        "before": None if existing is None else replaced_members,
        "after": [tx.ref(c, "component")["id"] for c in got],
    })  # fmt: skip
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
    (at most 200 each). A system group NX left unevaluated has count null."""
    part = tx.part
    found = mp.groups(part, include_system=include_system)
    tree: list | None = None
    rows, unknown = [], []
    for group, members in found:
        if mp.is_system_group(group.Name) and not members:
            # NX fills the system groups for the displayed part: in another assembly they
            # come back empty. AllComponents is the whole tree; the others stay unknown.
            if tree is None:
                tree = [c for c, _names in kparts.components(part)]
            if tree and str(group.Name).casefold() == "allcomponents":
                members = tree
            elif tree:
                row = _group_row(tx, group, members, with_members)
                row["count"] = None
                rows.append(row)
                unknown.append(str(group.Name))
                continue
        rows.append(_group_row(tx, group, members, with_members))
    if unknown:
        tx.warn("NX_NOTE", f"NX reported no members for {', '.join(unknown)} in this assembly "
                "(it fills system groups for the displayed part): count is null; display the "
                "assembly (nx_activate_part) to read them")  # fmt: skip
    return {"items": rows}


def _edit_group_summary(a, say) -> str:
    if a.get("delete"):
        return f"Delete component group {a.get('name')}"
    return f"Rename component group {a.get('name')} to {a.get('new_name')}"


@tool(
    effect="model",
    label="Edit component group",
    summary=_edit_group_summary,
    keywords=(
        "rename component group",
        "delete component group",
        "remove weight group",
        "drop group",
    ),  # fmt: skip
    requires=("work_part", "assembly", "no_active_sketch"),
    cost="light",
    refresh_annotations=False,
    example={"name": "PROPULSION", "new_name": "DRIVE"},
)
def nx_edit_component_group(
    tx: Tx,
    *,
    name: Annotated[Name, Doc("The user group (any case)")],
    new_name: Annotated[Name, Doc("Rename it")] | None = None,
    delete: Annotated[bool, Doc("Delete the group (its components stay)")] = False,
) -> GroupEditData:
    """Rename or delete a user component group of the work part's assembly; the components
    stay. Redefine members with nx_create_component_group(replace=true)."""
    if (new_name is not None) == delete:
        fail("NX_INVALID_ARGUMENT", "Give new_name or delete=true (one of them)",
             field="/new_name" if delete else "/delete")  # fmt: skip
    if mp.is_system_group(name):
        fail("NX_INVALID_ARGUMENT", f"{name} is an NX system group", field="/name")
    if new_name is not None and mp.is_system_group(new_name):
        fail("NX_INVALID_ARGUMENT", f"{new_name} is an NX system group name", field="/new_name")
    with tx.checks():
        part = tx.part
        group = mp.find_group(part, name)
        if group is None:
            available = [str(g.Name) for g, _m in mp.groups(part)][:50]
            fail("NX_NOT_FOUND", f"The assembly has no component group {name!r}", field="/name",
                 details={"available": available},
                 hint="nx_list_component_groups lists them")  # fmt: skip
        if new_name is not None:
            clash = mp.find_group(part, new_name)
            if clash is not None and clash is not group:
                fail("NX_ALREADY_EXISTS", f"A component group {clash.Name} already exists",
                     field="/new_name", details={"existing": str(clash.Name)})  # fmt: skip
    old = str(group.Name)
    if delete:
        with mp.licensed(tx), native(default="NX_BUILD_FAILED"):
            tx.session.UpdateManager.AddToDeleteList([group])
        model.update(tx)
        readback.expect(mp.find_group(part, old) is None, "deleted",
                        "NX kept the component group")  # fmt: skip
        return {"deleted": old, "remaining": [str(g.Name) for g, _m in mp.groups(part)]}
    assert new_name is not None
    with mp.licensed(tx), native(default="NX_BUILD_FAILED"):
        got = result.rename(tx, group, new_name)
    readback.expect(got.casefold() == new_name.casefold(), "name", "NX did not rename the group",
                    actual=got)  # fmt: skip
    tx.note_restore({"before": old, "after": got})
    return {"group": _group_row(tx, group, mp.group_members(group)), "previous_name": old}


def _group_now(tx, name):
    group = mp.find_group(tx.part, name)
    if group is None:
        fail("NX_DEPENDENCY_ORDER", f"Component group {name} was renamed or deleted since the "
             "operation; undo the later operations first")  # fmt: skip
    return group


def _member_tags(group) -> set[int]:
    return {int(c.Tag) for c in mp.group_members(group)}


@restorer("nx_create_component_group")
def _restore_group(tx: Tx, data: dict, *, apply: bool) -> str:
    """Delete a group nx_create_component_group made, or give a replaced group its previous
    members back: refused when its members changed since."""
    name = data["group"]
    group = _group_now(tx, name)
    after = {int(select.resolve(tx, r, "component", field="/components").Tag)
             for r in data["after"]}  # fmt: skip
    if _member_tags(group) != after:
        fail("NX_DEPENDENCY_ORDER", f"The members of component group {name} changed since "
             "the operation; undo the later operations first")  # fmt: skip
    before = data["before"]
    if before is None:
        if apply:
            with native(default="NX_BUILD_FAILED"):
                tx.session.UpdateManager.AddToDeleteList([group])
            model.update(tx)
            readback.expect(mp.find_group(tx.part, name) is None, "deleted",
                            "NX kept the component group")  # fmt: skip
        return f"component group {name} deleted"
    if apply:
        wanted = [select.resolve(tx, r, "component", field="/components") for r in before]
        with native(default="NX_BUILD_FAILED"):
            for c in mp.group_members(group):
                group.RemoveComponent(c)
            for c in wanted:
                group.AddComponent(c, False)
        readback.expect(_member_tags(group) == {int(c.Tag) for c in wanted}, "members",
                        "Component group members differ")  # fmt: skip
    return f"component group {name} back to its {len(before)} previous members"


@restorer("nx_edit_component_group")
def _restore_group_name(tx: Tx, data: dict, *, apply: bool) -> str:
    """Rename a group nx_edit_component_group renamed back (a delete is not restorable)."""
    group = _group_now(tx, data["after"])
    if apply:
        clash = mp.find_group(tx.part, data["before"])
        if clash is not None and clash is not group:
            fail("NX_ALREADY_EXISTS", f"Another component group is named {clash.Name} now",
                 details={"existing": str(clash.Name)})  # fmt: skip
        with native(default="NX_BUILD_FAILED"):
            got = result.rename(tx, group, data["before"])
        readback.expect(got.casefold() == str(data["before"]).casefold(), "name",
                        "NX did not rename the group back", actual=got)  # fmt: skip
    return f"component group {data['after']} renamed back to {data['before']}"
