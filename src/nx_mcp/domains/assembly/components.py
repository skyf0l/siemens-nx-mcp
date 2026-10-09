"""Components: list, add, edit and place occurrences (tool-surface 3.8).

NX 2406 findings encoded here:

- NX names a loaded part by its file basename: adding or substituting a file whose
  basename a different loaded part already uses would bind the other part, so it
  is refused as ``NX_NAME_CONFLICT`` before NX acts.
- ``MoveComponent`` pivots about the assembly origin (``kit.pose.move_args``).
- Editing a component (rename, suppress, reference set, replace) must not move it:
  the pose is read back and a change rolls the edit back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Mat3, Name, Offset, PageLimit, PathTo, Point3, Pose, Text
from nx_mcp.domains.assembly import common
from nx_mcp.domains.assembly.common import ComponentRef, ComponentRow, Translation
from nx_mcp.kit import args, assembly, builders, geom, model, parts, readback, result
from nx_mcp.kit import pose as kpose

PartFile = PathTo("read", ".prt")
BUILT_IN_SETS = ("Entire Part", "Empty")
RefSetName = Annotated[str, Text(132, r"^\S(.*\S)?$"), Doc("Reference set name, e.g. MODEL")]


# ---------------------------------------------------------------------------
# Shared checks


def _check_file(tx, path, field):
    """Refuse the work part itself and a basename another loaded part already uses."""
    work = parts.file_of(tx.part)
    if parts.same_file(work, path):
        fail("NX_INVALID_ARGUMENT", "A part cannot contain itself", field=field)
    stem = Path(path).stem.casefold()
    for loaded in tx.session.Parts:
        other = parts.file_of(loaded)
        if Path(other).stem.casefold() == stem and not parts.same_file(other, path):
            fail(
                "NX_NAME_CONFLICT",
                "A loaded part with the same file name comes from another folder",
                field=field,
                hint="Close that part or rename the file",
                details={"path": str(path), "loaded_part_path": other},
            )


# ---------------------------------------------------------------------------
# nx_list_components


class ComponentListRow(ComponentRow, total=False):
    depth: int
    suppressed: bool
    load_state: str
    part_path: str | None
    reference_set: str


class ComponentListData(TypedDict):
    items: list[ComponentListRow]
    page: dict[str, Any]


@tool(
    effect="read",
    label="List components",
    summary=lambda a, say: (
        "List components"
        + (f" named *{a['name_contains']}*" if a.get("name_contains") else "")
        + (" with poses" if a.get("include_poses") else "")
    ),
    keywords=("assembly tree", "occurrences", "assembly navigator", "parts in assembly", "BOM"),
    requires=("work_part",),
    cost="light",
    example={"include_poses": True},
)
def nx_list_components(
    tx: Tx,
    *,
    name_contains: Annotated[str, Text(64), Doc("Case-insensitive name filter")] | None = None,
    suppressed: Annotated[bool, Doc("Only suppressed (true) or unsuppressed (false)")]
    | None = None,
    include_poses: Annotated[bool, Doc("Add translation and rotation")] = False,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ComponentListData:
    """List the work part's component occurrences depth first, one page at a time. Rows carry
    path, load state, reference set, suppression and, on request, absolute poses."""
    found = []
    for c, names in parts.components(tx.part):
        if name_contains and name_contains.casefold() not in str(c.Name).casefold():
            continue
        if suppressed is not None and bool(c.IsSuppressed) != suppressed:
            continue
        found.append((c, names))
    selected, page = result.page(found, offset, limit)
    items = []
    for c, names in selected:
        row = common.component_row(tx, c, names, with_pose=include_poses)
        state = assembly.load_state(tx, c)
        reference_set = ""
        try:
            reference_set = str(c.ReferenceSet or "")
        except Exception:  # an unloaded occurrence
            reference_set = ""
        row.update(
            depth=len(names) - 1,
            suppressed=bool(c.IsSuppressed),
            load_state=state["load_state"],
            part_path=common.relative_path(tx, state["part_path"]) or None,
            reference_set=reference_set,
        )
        items.append(row)
    return {"items": items, "page": page}


# ---------------------------------------------------------------------------
# nx_create_component


class CreateComponentData(TypedDict):
    component: ComponentRow
    part_path: str


@tool(
    effect="model",
    label="Add component",
    summary=lambda a, say: f"Add component {Path(str(a.get('path', ''))).name}",
    keywords=("add component", "insert part", "place part", "add existing", "assemble"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"path": "parts/bracket.prt", "pose": {"translation": [50, 0, 0]}},
)
def nx_create_component(
    tx: Tx,
    *,
    path: PartFile,  # type: ignore[valid-type]
    name: Name | None = None,
    pose: Pose | None = None,
) -> CreateComponentData:
    """Add a saved part as a component of the work part at an absolute pose (identity when
    omitted). The work part becomes an assembly."""
    target = kpose.as_pose(pose) if pose is not None else ([0.0, 0.0, 0.0], kpose.identity())
    with tx.checks():
        _check_file(tx, path, "/path")
    part = tx.part
    with native(default="NX_BUILD_FAILED"):
        c, status = part.ComponentAssembly.AddComponent(
            str(path),
            "Entire Part",
            name or Path(path).stem,
            tx.nx.Point3d(*target[0]),
            builders.matrix(tx, target[1]),
            -1,
        )
    if status is not None and hasattr(status, "Dispose"):
        status.Dispose()
    readback.expect(c is not None, "component", "NX added no component")
    readback.expect(
        parts.same_file(parts.file_of(c.Prototype), path),
        "prototype",
        "The component uses another file",
        actual=parts.file_of(c.Prototype),
    )
    assembly.expect_pose(assembly.pose(c), target)
    return {
        "component": common.component_row(tx, c, [str(c.Name)]),
        "part_path": common.relative_path(tx, path),
    }


# ---------------------------------------------------------------------------
# nx_edit_component


class EditedComponent(ComponentRow, total=False):
    suppressed: bool
    reference_set: str
    part_path: str


class EditComponentData(TypedDict):
    components: list[EditedComponent]


def _edit_summary(a, say):
    keys = ("name", "replace_with", "suppressed", "reference_set")
    what = [k for k in keys if a.get(k) is not None]
    verbs = {"name": "rename", "replace_with": "replace", "reference_set": "reference set"}
    words = [verbs.get(k) or ("suppress" if a.get("suppressed") else "unsuppress") for k in what]
    return f"Edit {say('components')}: " + ", ".join(words)


def _reference_sets(component):
    prototype = component.Prototype
    if prototype is None:
        return None
    return [str(r.Name) for r in prototype.GetAllReferenceSets()] + list(BUILT_IN_SETS)


@tool(
    effect="model",
    label="Edit components",
    summary=_edit_summary,
    keywords=("rename component", "replace component", "suppress component", "use reference set"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    example={"components": ["o:4"], "suppressed": True},
)
def nx_edit_component(
    tx: Tx,
    *,
    components: Annotated[list[ComponentRef], Items(1, 100)],  # type: ignore[valid-type]
    name: Annotated[Name, Doc("New name; one component only")] | None = None,
    replace_with: Annotated[PartFile, Doc("Saved .prt that replaces the prototype")]  # type: ignore[valid-type]
    | None = None,
    suppressed: bool | None = None,
    reference_set: RefSetName | None = None,
) -> EditComponentData:
    """Rename, replace the part of, suppress or unsuppress, or set the reference set of direct
    components of the work part. Placement never changes. Remove one with nx_delete_objects."""
    if name is None and replace_with is None and suppressed is None and reference_set is None:
        fail("NX_INVALID_ARGUMENT", "Give name, replace_with, suppressed or reference_set")
    if name is not None and len(components) != 1:
        fail("NX_INVALID_ARGUMENT", "Rename one component at a time", field="/name")
    args.distinct("/components", components, "components")
    with tx.checks():
        values = [common.component(tx, r, f"/components/{i}") for i, r in enumerate(components)]
        if replace_with is not None:
            _check_file(tx, replace_with, "/replace_with")
        if reference_set is not None and replace_with is None:
            for i, c in enumerate(values):
                names = _reference_sets(c)
                if names is None or reference_set not in names:
                    fail(
                        "NX_NOT_FOUND",
                        f"{c.Name} has no reference set {reference_set!r}",
                        field="/reference_set",
                        details={"component": f"/components/{i}", "available": names},
                    )
    part = tx.part
    poses = [assembly.pose(c) for c in values]
    if replace_with is not None:
        with builders.build(tx, part.AssemblyManager.CreateReplaceComponentBuilder) as b:
            for c in values:
                b.ComponentsToReplace.Add(c)
            b.ReplacementPart = str(replace_with)
            b.ReplaceAllOccurrences = False
            b.MaintainRelationships = True
            b.commit()
            assembly.check(b.GetErrorList(), "NX_BUILD_FAILED", "NX did not replace the part")
        model.update(tx)
    if reference_set is not None:
        for i, c in enumerate(values):
            names = _reference_sets(c)
            if names is None or reference_set not in names:
                fail(
                    "NX_NOT_FOUND",
                    f"The new part has no reference set {reference_set!r}",
                    field="/reference_set",
                    details={"component": f"/components/{i}", "available": names},
                )
            with native(default="NX_BUILD_FAILED"):
                part.ComponentAssembly.ReplaceReferenceSet(c, reference_set)
    if name is not None:
        result.rename(tx, values[0], name)
    if suppressed is not None:
        method = "SuppressComponents" if suppressed else "UnsuppressComponents"
        with native(default="NX_BUILD_FAILED"):
            errors = getattr(part.ComponentAssembly, method)(values)
        assembly.check(errors, "NX_BUILD_FAILED", f"NX did not {method[:-10].lower()}")
    model.update(tx)
    names = assembly.paths(part)
    out = []
    for i, (c, before) in enumerate(zip(values, poses, strict=True)):
        assembly.expect_pose(assembly.pose(c), before, f"placement[{i}]")
        if suppressed is not None:
            readback.expect(bool(c.IsSuppressed) == suppressed, f"suppressed[{i}]")
        if reference_set is not None:
            readback.expect(
                str(c.ReferenceSet) == reference_set, f"reference_set[{i}]",
                actual=str(c.ReferenceSet),
            )  # fmt: skip
        if replace_with is not None:
            readback.expect(
                parts.same_file(parts.file_of(c.Prototype), replace_with),
                f"replace_with[{i}]",
                actual=parts.file_of(c.Prototype),
            )
        row = common.component_row(tx, c, names.get(int(c.Tag)))
        row.update(
            suppressed=bool(c.IsSuppressed),
            reference_set=str(c.ReferenceSet or ""),
            part_path=common.relative_path(tx, parts.file_of(c.Prototype)),
        )
        out.append(row)
    return {"components": out}


# ---------------------------------------------------------------------------
# nx_set_component_pose


class PoseValue(TypedDict):
    translation: Point3
    rotation: Mat3


class PoseData(TypedDict):
    component: ComponentRow
    previous: PoseValue


@tool(
    effect="model",
    label="Place component",
    summary={
        "absolute": "Place {component} at {translation}",
        "relative": "Move {component} by {translation}",
    },
    keywords=("move component", "position component", "reposition", "transform component"),
    requires=("work_part", "no_active_sketch", "assembly"),
    variants="mode",
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"component": "o:4", "mode": "absolute", "translation": [0, 40, 0]},
)
def nx_set_component_pose(
    tx: Tx,
    *,
    component: ComponentRef,  # type: ignore[valid-type]
    mode: Literal["absolute", "relative"],
    translation: Annotated[Translation, Doc("absolute: position; relative: offset (part units)")],
    rotation: Annotated[Mat3, Doc("absolute: orientation (default identity); relative: turn")]
    | None = None,
) -> PoseData:
    """Place a direct component at an absolute pose, or move it relative to its pose. A
    relative rotation turns it about its own origin, then the offset applies."""
    r = geom.rotation(rotation, "/rotation") if rotation is not None else None
    t = [float(v) for v in translation]
    with tx.checks():
        c = common.component(tx, component, "/component")
    current = assembly.pose(c)
    if mode == "absolute":
        target = (t, r if r is not None else kpose.identity())
    else:
        target = kpose.relative(current, t, r)
    assembly.move(tx, c, target)
    return {
        "component": common.component_row(tx, c),
        "previous": {"translation": current[0], "rotation": current[1]},
    }
