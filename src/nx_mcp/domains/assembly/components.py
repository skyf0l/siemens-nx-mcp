"""Components: list, add, edit and place occurrences (tool-surface 3.8).

NX 2406 findings encoded here:

- NX names a loaded part by its file basename: adding or substituting a file whose
  basename a different loaded part already uses would bind the other part, so it
  is refused as ``NX_NAME_CONFLICT`` before NX acts.
- ``MoveComponent`` pivots about the assembly origin (``kit.pose.move_args``).
- Editing a component (rename, suppress, reference set, replace) must not move it:
  the pose is read back and a change rolls the edit back.
- New component in context: ``CreateNewComponentBuilder`` creates the part from a
  ``FileNew`` (not displayed) and places it at a CSYS (``ComponentOrigin``
  ``Newcsys``), so moved bodies keep their assembly position. NX writes the file on
  the part's first save.
- Make unique: ``Part.SetMakeUniqueName`` names the copy, then
  ``MakeUniquePartBuilder`` points the selected occurrence at it and renames it
  after the new part; other occurrences keep the original part.
- NX cannot change the reference set of a suppressed component ("Cannot change
  reference set on suppressed component", 720173): an edit unsuppresses first and
  suppresses last, and a reference set for a component that stays suppressed is
  refused before NX acts.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    BodyRef,
    Doc,
    Items,
    Mat3,
    Offset,
    PageLimit,
    PathTo,
    Point3,
    Text,
)
from nx_mcp.domains.assembly import common, structure
from nx_mcp.domains.assembly.common import (
    ComponentId,
    ComponentName,
    ComponentPose,
    ComponentRow,
    Rotation,
    Translation,
)
from nx_mcp.kit import args, builders, model, parts, readback, result, select
from nx_mcp.kit import pose as kpose
from nx_mcp.kit import units as kit_units

BUILT_IN_SETS = ("Entire Part", "Empty")
RefSetName = Annotated[str, Doc("e.g. MODEL or Entire Part (any case)"), Text(132, r"^\S(.*\S)?$")]
SET_HINT = (
    "Use a name from details.available. Sets come from the component's own part: create one "
    "there (nx_activate_part on it, nx_create_reference_set), then reactivate the assembly"
)


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


def _set_key(name) -> str:
    return "".join(ch for ch in str(name).casefold() if ch.isalnum())


def _reference_sets(component):
    """Names of the reference sets the component's part offers (None: not loaded)."""
    prototype = component.Prototype
    if prototype is None:
        return None
    names = [str(r.Name) for r in prototype.GetAllReferenceSets()] + list(BUILT_IN_SETS)
    return list(dict.fromkeys(names))


def _resolve_set(component, requested, where, owner=None):
    """The reference set of the component's part that ``requested`` names: exactly, else
    the one equal ignoring case, spaces, '_' and '-' ("model" -> MODEL)."""
    names = _reference_sets(component)
    if names is not None and requested in names:
        return requested
    loose = [n for n in names or () if _set_key(n) == _set_key(requested)]
    if len(loose) == 1:
        return loose[0]
    fail(
        "NX_NOT_FOUND",
        f"{owner or component.Name} has no reference set {requested!r}",
        field="/reference_set",
        hint=SET_HINT,
        details={"component": where, "available": names},
    )


def _use_set(tx, component, name):
    """Point ``component`` at its part's reference set ``name`` (a resolved name)."""
    if _set_of(component) != name:
        with native(default="NX_BUILD_FAILED"):
            tx.part.ComponentAssembly.ReplaceReferenceSet(component, name)


def _set_of(component) -> str:
    try:
        return str(component.ReferenceSet or "")
    except Exception:  # an unloaded occurrence
        return ""


def _order(component, names):
    """Sort key: depth first, siblings by name (then id), whatever order NX lists them in."""
    chain = []
    node = component
    while node is not None and getattr(node, "Parent", None) is not None:
        chain.append(node)
        node = node.Parent
    chain.reverse()
    if len(chain) != len(names):  # unexpected parentage: keep the names alone
        return [(str(n).casefold(), 0) for n in names]
    return [(str(n).casefold(), int(c.Tag)) for n, c in zip(names, chain, strict=True)]


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
    include_poses: bool = False,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ComponentListData:
    """List the work part's component occurrences depth first, a page at a time. Rows: id,
    path, load state, reference set, suppression and, with include_poses, absolute poses."""
    found = []
    for c, names in parts.components(tx.part):
        if name_contains and name_contains.casefold() not in str(c.Name).casefold():
            continue
        if suppressed is not None and bool(c.IsSuppressed) != suppressed:
            continue
        found.append((c, names))
    found.sort(key=lambda item: _order(*item))
    selected, page = result.page(found, offset, limit)
    items: list[ComponentListRow] = []
    for c, names in selected:
        row = cast(ComponentListRow, common.component_row(tx, c, names, with_pose=include_poses))
        state = structure.load_state(tx, c)
        reference_set = _set_of(c)
        row.update(
            {
                "depth": len(names) - 1,
                "suppressed": bool(c.IsSuppressed),
                "load_state": state["load_state"],
                "part_path": common.relative_path(tx, state["part_path"]) or None,
                "reference_set": reference_set,
            }
        )
        items.append(row)
    return {"items": items, "page": page}


# ---------------------------------------------------------------------------
# nx_create_component


class CreatedComponent(ComponentRow, total=False):
    reference_set: str


class CreateComponentData(TypedDict, total=False):
    component: CreatedComponent
    part_path: str
    part: result.Row
    units: str
    moved_bodies: int


BodyList = Annotated[
    list[BodyRef],
    Items(1, 100),
    Doc("new_in_context: work-part bodies moved into the new part (same position)"),
]


def _create_summary(a, say):
    if a.get("mode") == "new_in_context":
        return f"Create new component {Path(str(a.get('new_path', ''))).name}"
    return f"Add component {Path(str(a.get('path', ''))).name}"


@tool(
    effect="model",
    label="Add component",
    summary=_create_summary,
    keywords=(
        "add component",
        "insert part",
        "place part",
        "new component",
        "assemble",
        "assembly component",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="mode",
    required_by={"from_file": ("path",), "new_in_context": ("new_path",)},
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"path": "parts/bracket.prt", "name": "bracket_1", "pose": {"translation": [50, 0, 0]}},
)
def nx_create_component(
    tx: Tx,
    *,
    mode: Literal["from_file", "new_in_context"] = "from_file",
    path: Annotated[str, Doc("from_file: the saved .prt"), PathTo("read", ".prt")] | None = None,
    new_path: Annotated[str, Doc("new_in_context: the new part's file"), PathTo("new", ".prt")]
    | None = None,
    units: Annotated[Literal["mm", "in"], Doc("new_in_context: default the work part's")]
    | None = None,
    objects_to_move: BodyList | None = None,
    name: ComponentName | None = None,
    pose: Annotated[ComponentPose, Doc("Omitted: origin, unrotated")] | None = None,
    reference_set: RefSetName | None = None,
) -> CreateComponentData:
    """Add a saved part (from_file) or a new empty part (new_in_context) as a component of the
    work part. reference_set defaults to Entire Part (from_file) or the template's.
    new_in_context may move bodies into the new part; save it with nx_save_part. Use the
    returned component and part ids."""
    target = common.pose(pose)
    wrong = ("new_path", "units", "objects_to_move") if mode == "from_file" else ("path",)
    given = {"path": path, "new_path": new_path, "units": units, "objects_to_move": objects_to_move}
    for field in wrong:
        if given[field] is not None:
            fail("NX_INVALID_ARGUMENT", f"{mode} takes no {field}", field="/" + field)
    needed = "path" if mode == "from_file" else "new_path"
    if given[needed] is None:
        fail("NX_INVALID_ARGUMENT", f"{mode} needs {needed}", field="/" + needed)
    if mode == "new_in_context":
        assert new_path is not None  # checked above
        created, c = _new_in_context(tx, Path(new_path), units, objects_to_move or [], name, target)
        return _finish_create(tx, created, c, reference_set)
    assert path is not None  # checked above
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
    structure.expect_pose(structure.pose(c), target)
    data: CreateComponentData = {
        "component": cast(CreatedComponent, common.component_row(tx, c, [str(c.Name)])),
        "part_path": common.relative_path(tx, path),
        "part": tx.ref(c.Prototype, "part"),
    }
    return _finish_create(tx, data, c, reference_set)


def _finish_create(tx, data, component, reference_set) -> CreateComponentData:
    """Apply the requested reference set and report the one the component uses."""
    if reference_set is not None:
        chosen = _resolve_set(component, reference_set, "/reference_set")
        _use_set(tx, component, chosen)
        readback.expect(_set_of(component) == chosen, "reference_set", actual=_set_of(component))
    data["component"]["reference_set"] = _set_of(component)
    return data


def _new_in_context(tx, path, units, objects, name, target):
    """Create an empty part at ``path`` as a component at ``target`` (moving ``objects``)."""
    part = tx.part
    with tx.checks():
        parts.check_new_name(tx, path, field="/new_path")
        bodies = select.bodies(tx, objects, "/objects_to_move")
    units = units or kit_units.part_units(tx, part)
    before = {int(c.Tag) for c in structure.direct_children(part)}
    path.parent.mkdir(parents=True, exist_ok=True)
    nx = tx.nx
    with native(default="NX_BUILD_FAILED"):
        new_file = tx.session.Parts.FileNew()
    try:
        with builders.build(tx, part.AssemblyManager.CreateNewComponentBuilder) as b:
            new_file.TemplateFileName = parts.TEMPLATES[units]
            new_file.Units = nx.Part.Units.Millimeters if units == "mm" else nx.Part.Units.Inches
            new_file.NewFileName = str(path)
            new_file.MakeDisplayedPart = False
            new_file.DisplayPartOption = nx.DisplayPartOption.AllowAdditional
            b.NewComponentName = (name or path.stem).upper()
            b.ComponentOrigin = nx.Assemblies.CreateNewComponentBuilder.ComponentOriginType.Newcsys
            b.Csys = part.CoordinateSystems.CreateCoordinateSystem(
                nx.Point3d(*target[0]), builders.matrix(tx, target[1]), False
            )
            for body in bodies:
                b.ObjectForNewComponent.Add(body)
            b.OriginalObjectsDeleted = True
            b.NewFile = new_file
            b.commit()
    finally:
        with contextlib.suppress(Exception):
            new_file.Destroy()
    model.update(tx)
    added = [c for c in structure.direct_children(part) if int(c.Tag) not in before]
    readback.expect(len(added) == 1, "component", "NX added no component", actual=len(added))
    c = added[0]
    prototype = c.Prototype
    readback.expect(
        parts.same_file(parts.file_of(prototype), path),
        "prototype",
        "The component uses another file",
        actual=parts.file_of(prototype),
    )
    actual_units = kit_units.part_units(tx, prototype)
    readback.expect(actual_units == units, "units", actual=actual_units)
    structure.expect_pose(structure.pose(c), target)
    gone = [i for i, body in enumerate(bodies) if _alive(body, part)]
    readback.expect(not gone, "objects_to_move", "Bodies stayed in the work part", actual=gone)
    if bodies:
        count = len(list(prototype.Bodies))
        readback.expect(count >= len(bodies), "moved_bodies", actual=count)
    data: CreateComponentData = {
        "component": cast(CreatedComponent, common.component_row(tx, c, [str(c.Name)])),
        "part_path": common.relative_path(tx, path),
        "part": tx.ref(prototype, "part"),
        "units": units,
        "moved_bodies": len(bodies),
    }
    return data, c


def _alive(body, part) -> bool:
    """Whether ``body`` is still a body of ``part`` (moved bodies are deleted)."""
    tag = int(body.Tag)
    return any(int(b.Tag) == tag for b in part.Bodies)


# ---------------------------------------------------------------------------
# nx_edit_component


class EditedComponent(ComponentRow, total=False):
    suppressed: bool
    reference_set: str
    part_path: str


class EditComponentData(TypedDict):
    components: list[EditedComponent]


def _edit_summary(a, say):
    keys = ("name", "replace_with", "make_unique", "suppressed", "reference_set")
    what = [k for k in keys if a.get(k) is not None]
    verbs = {
        "name": "rename", "replace_with": "replace", "make_unique": "make unique",
        "reference_set": "reference set",
    }  # fmt: skip
    words = [verbs.get(k) or ("suppress" if a.get("suppressed") else "unsuppress") for k in what]
    return f"Edit {say('components')}: " + ", ".join(words)


def _suppress(tx, values, on):
    method = "SuppressComponents" if on else "UnsuppressComponents"
    with native(default="NX_BUILD_FAILED"):
        errors = getattr(tx.part.ComponentAssembly, method)(values)
    structure.check(errors, "NX_BUILD_FAILED", f"NX did not {'' if on else 'un'}suppress")
    model.update(tx)


@tool(
    effect="model",
    label="Edit components",
    summary=_edit_summary,
    keywords=(
        "rename component",
        "replace component",
        "suppress component",
        "use reference set",
        "make unique",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    example={"components": ["o:4"], "suppressed": False, "reference_set": "MODEL"},
    # NX 2406 replaces or makes unique only in the displayed assembly (a background one:
    # "unexpected NULL pointer" or the old part kept, live 2026-10-12).
    display_bound=("/replace_with", "/make_unique"),
)
def nx_edit_component(
    tx: Tx,
    *,
    components: Annotated[list[ComponentId], Items(1, 100)],
    name: ComponentName | None = None,
    replace_with: Annotated[
        str, Doc("Saved .prt that replaces the component's part"), PathTo("read", ".prt")
    ]
    | None = None,
    make_unique: Annotated[
        str, Doc("New .prt: a copy of the part for this component only"), PathTo("new", ".prt")
    ]
    | None = None,
    suppressed: Annotated[bool, Doc("false: unsuppress (done first); true: suppress (last)")]
    | None = None,
    reference_set: RefSetName | None = None,
) -> EditComponentData:
    """Edit direct components: rename (one), replace or make unique the part, (un)suppress, or
    set the reference set. Options combine in one call. reference_set must exist in the
    component's part. make_unique renames it after the copy. Placement never changes; delete
    with nx_delete_objects."""
    edits = (name, replace_with, make_unique, suppressed, reference_set)
    if all(v is None for v in edits):
        fail(
            "NX_INVALID_ARGUMENT",
            "Give name, replace_with, make_unique, suppressed or reference_set",
        )
    if name is not None and len(components) != 1:
        fail("NX_INVALID_ARGUMENT", "Rename one component at a time", field="/name")
    if make_unique is not None and len(components) != 1:
        fail("NX_INVALID_ARGUMENT", "Make one component unique at a time", field="/make_unique")
    if make_unique is not None and replace_with is not None:
        fail("NX_INVALID_ARGUMENT", "Replace or make unique, not both", field="/make_unique")
    args.distinct("/components", components, "components")
    with tx.checks():
        values = [common.component(tx, r, f"/components/{i}") for i, r in enumerate(components)]
        if replace_with is not None:
            _check_file(tx, replace_with, "/replace_with")
        if make_unique is not None:
            parts.check_new_name(tx, make_unique, field="/make_unique")
            if values[0].Prototype is None:
                fail(
                    "NX_INVALID_ARGUMENT",
                    "The component's part is not loaded",
                    field="/components/0",
                    hint="Open the assembly with load_components=true",
                )
        if reference_set is not None:
            for i, c in enumerate(values):
                if c.IsSuppressed and suppressed is not False:
                    fail(
                        "NX_INVALID_ARGUMENT",
                        f"{c.Name} is suppressed: NX cannot change its reference set",
                        field=f"/components/{i}",
                        hint="Add suppressed: false to unsuppress it in the same call",
                    )
                if replace_with is None:
                    _resolve_set(c, reference_set, f"/components/{i}")
    part = tx.part
    poses = [structure.pose(c) for c in values]
    if suppressed is False:
        _suppress(tx, values, False)
    if replace_with is not None:
        with builders.build(tx, part.AssemblyManager.CreateReplaceComponentBuilder) as b:
            for c in values:
                b.ComponentsToReplace.Add(c)
            b.ReplacementPart = str(replace_with)
            b.ReplaceAllOccurrences = False
            b.MaintainRelationships = True
            b.commit()
            structure.check(
                b.GetErrorList(),
                "NX_BUILD_FAILED",
                "NX did not replace the part",
                hint="Nothing changed; NX's reasons are in the message and details.errors",
            )
        model.update(tx)
    if make_unique is not None:
        _make_unique(tx, values[0], Path(make_unique))
    sets: list[str | None] = [None] * len(values)
    if reference_set is not None:
        owner = "The new part" if replace_with is not None else None
        for i, c in enumerate(values):
            sets[i] = _resolve_set(c, reference_set, f"/components/{i}", owner)
            _use_set(tx, c, sets[i])
    if name is not None:
        result.rename(tx, values[0], name)
    if suppressed:
        _suppress(tx, values, True)
    model.update(tx)
    names = structure.paths(part)
    out: list[EditedComponent] = []
    for i, (c, before) in enumerate(zip(values, poses, strict=True)):
        structure.expect_pose(structure.pose(c), before, f"placement[{i}]")
        if suppressed is not None:
            readback.expect(bool(c.IsSuppressed) == suppressed, f"suppressed[{i}]")
        if sets[i] is not None:
            readback.expect(_set_of(c) == sets[i], f"reference_set[{i}]", actual=_set_of(c))
        new_file = replace_with if replace_with is not None else make_unique
        if new_file is not None:
            readback.expect(
                parts.same_file(parts.file_of(c.Prototype), new_file),
                f"{'replace_with' if replace_with is not None else 'make_unique'}[{i}]",
                actual=parts.file_of(c.Prototype),
            )
        row = cast(EditedComponent, common.component_row(tx, c, names.get(int(c.Tag))))
        row.update(
            {
                "suppressed": bool(c.IsSuppressed),
                "reference_set": _set_of(c),
                "part_path": common.relative_path(tx, parts.file_of(c.Prototype)),
            }
        )
        out.append(row)
    return {"components": out}


def _make_unique(tx, component, path):
    """Point ``component`` at a new copy of its part saved later as ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with builders.build(tx, tx.part.AssemblyManager.CreateMakeUniquePartBuilder) as b:
        b.SelectedComponents.Add(component)
        component.Prototype.SetMakeUniqueName(str(path))
        b.commit()
    model.update(tx)


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
    example={
        "component": "o:4",
        "mode": "relative",
        "translation": [0, 40, 0],
        "rotation": {"axis": "z", "angle": 90},
    },  # fmt: skip
)
def nx_set_component_pose(
    tx: Tx,
    *,
    component: ComponentId,
    mode: Literal["absolute", "relative"],
    translation: Translation,
    rotation: Rotation | None = None,
) -> PoseData:
    """Place a direct component at an absolute pose, or move it relative to its pose. absolute
    without rotation means no rotation. relative turns it about its own origin by rotation,
    then shifts it by translation. Returns the previous pose."""
    r = common.rotation(rotation, "/rotation") if rotation is not None else None
    t = [float(v) for v in translation]
    with tx.checks():
        c = common.component(tx, component, "/component")
    current = structure.pose(c)
    if mode == "absolute":
        target = (t, r if r is not None else kpose.identity())
    else:
        target = kpose.relative(current, t, r)
    structure.move(tx, c, target)
    return {
        "component": common.component_row(tx, c),
        "previous": {"translation": current[0], "rotation": current[1]},
    }
