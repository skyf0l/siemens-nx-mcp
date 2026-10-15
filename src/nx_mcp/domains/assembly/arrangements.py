"""Assembly arrangements: named states of one assembly in which components may sit at other
poses or be suppressed (a hatch open and shut, a rudder at +-35 deg).

NX findings encoded here:

- ``ComponentAssembly.Arrangements.Create(base, name)`` copies an existing arrangement;
  ``ActiveArrangement`` reads and switches the one NX shows; UF
  ``Assem.AskDefaultArrangement`` / ``SetDefaultArrangement`` the one a parent assembly
  uses; ``Arrangement.Delete(forget_positioning)``.
- A component has the same pose in every arrangement until its positioning is varied
  (``Component.SetPositioningVaried``); ``MoveComponent`` then moves it in the active
  arrangement only, so a pose for another arrangement is set by activating that one for
  the move and switching back.
- Suppression is per arrangement without switching (``SuppressComponents`` /
  ``UnsuppressComponents`` with arrangements, ``GetSuppressedState``).
"""

from __future__ import annotations

import contextlib
import typing
from typing import Annotated, Literal, TypedDict, cast

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Name, Text
from nx_mcp.domains.assembly import common, structure
from nx_mcp.domains.assembly.common import ComponentId, ComponentPose, ComponentRow
from nx_mcp.kit import builders, display, geom, model, readback, result
from nx_mcp.kit import pose as kpose

MAX_LISTED = 200
_NAME_TEXT = next(m for m in typing.get_args(Name)[1:] if isinstance(m, Text))
ArrangementName = Annotated[
    str, Doc("Arrangement name (any case); nx_list_arrangements"), _NAME_TEXT
]


class ArrangementRow(TypedDict):
    name: str
    active: bool
    default: bool


class ArrangementState(ArrangementRow, total=False):
    suppressed: list[result.Row]


class ArrangedComponent(ComponentRow, total=False):
    suppressed: bool
    varied: bool | None


class ArrangementList(TypedDict):
    items: list[ArrangementState]
    components: list[ArrangedComponent]
    truncated: bool


class ArrangementData(TypedDict):
    arrangement: ArrangementRow
    previous_name: str | None
    arrangements: list[ArrangementRow]
    components: list[ArrangedComponent]


class ComponentState(TypedDict):
    component: ComponentRow
    arrangement: ArrangementRow
    suppressed: bool
    varied: bool


# ---------------------------------------------------------------------------
# Shared helpers


def _assembly(tx):
    return tx.part.ComponentAssembly


def _all(assembly) -> list:
    """The arrangements by name (NX's own order changes as they are created and activated)."""
    return sorted(assembly.Arrangements, key=lambda a: str(a.Name).casefold())


def _key(name) -> str:
    return str(name).strip().casefold()


def _find(assembly, name, field):
    """The arrangement called ``name`` (any case), else NX_NOT_FOUND listing them."""
    found = [a for a in _all(assembly) if _key(a.Name) == _key(name)]
    if not found:
        fail("NX_NOT_FOUND", f"The assembly has no arrangement {name!r}", field=field,
             details={"available": [str(a.Name) for a in _all(assembly)]},
             hint="nx_list_arrangements lists them")  # fmt: skip
    return found[0]


def _clash(assembly, name, field, keep=None) -> None:
    for a in _all(assembly):
        if _key(a.Name) == _key(name) and not display.same(a, keep):
            fail("NX_ALREADY_EXISTS", f"An arrangement {a.Name} already exists", field=field,
                 hint="Choose another name, or use it: nx_edit_arrangement(arrangement="
                 f"'{a.Name}', action='activate')",
                 details={"arrangement": str(a.Name),
                          "arrangements": [str(o.Name) for o in _all(assembly)]})  # fmt: skip


def _default(tx):
    """The default arrangement of the work part (None when NX cannot tell)."""
    with contextlib.suppress(Exception):
        tag = int(tx.uf.Assem.AskDefaultArrangement(tx.part.Tag))
        return next((a for a in _all(_assembly(tx)) if int(a.Tag) == tag), None)
    return None


def default_arrangement(tx):
    """The default arrangement of the work assembly (None when NX cannot tell)."""
    return _default(tx)


def _row(tx, arrangement) -> ArrangementRow:
    assembly = _assembly(tx)
    return {
        "name": str(arrangement.Name),
        "active": display.same(assembly.ActiveArrangement, arrangement),
        "default": display.same(_default(tx), arrangement),
    }


def _arrangement_rows(tx) -> list[ArrangementRow]:
    return [_row(tx, a) for a in _all(_assembly(tx))]


def _children(tx) -> list:
    """Direct components by name (NX's child order is not stable)."""
    return sorted(structure.direct_children(tx.part), key=lambda c: str(c.Name).casefold())


def _components(tx) -> list[ArrangedComponent]:
    """Direct components (by name) with their poses and suppression in the active
    arrangement."""
    names = structure.paths(tx.part)
    out: list[ArrangedComponent] = []
    for c in _children(tx)[:MAX_LISTED]:
        row = cast(ArrangedComponent, common.component_row(tx, c, names.get(int(c.Tag))))
        row["suppressed"] = bool(c.IsSuppressed)
        out.append(row)
    return out


def _suppressed_in(tx, component, arrangement) -> bool:
    with native(default="NX_INCOMPLETE_RESULT"):
        state = _assembly(tx).GetSuppressedState(component, arrangement)
    if isinstance(state, tuple):  # (SuppressedState, controlled at this level)
        state = state[0]
    states = tx.nx.Assemblies.ComponentAssembly.SuppressedState
    return str(state) in (str(states.Suppressed), str(states.SuppressedByExp))


def _activate(tx, arrangement) -> None:
    assembly = _assembly(tx)
    with native(default="NX_BUILD_FAILED"):
        assembly.ActiveArrangement = arrangement
    model.update(tx)
    readback.expect(display.same(assembly.ActiveArrangement, arrangement), "active",
                    f"NX did not activate {arrangement.Name}")  # fmt: skip


@contextlib.contextmanager
def _active(tx, arrangement):
    """``arrangement`` active for the block, the previous one active again after it (also
    when the block fails: the kernel's undo mark then rolls the model back)."""
    before = _assembly(tx).ActiveArrangement
    if display.same(before, arrangement):
        yield
        return
    _activate(tx, arrangement)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _activate(tx, before)


def _varied(tx, component) -> bool | None:
    """Whether ``component`` is positioned individually per arrangement (None: unknown)."""
    with contextlib.suppress(Exception):
        builder = tx.part.PropertiesManager.CreateAssembliesParameterPropertiesBuilder([component])
        try:
            options = tx.nx.Assemblies.AssembliesParameterPropertiesBuilder.ArrangementOptions
            return str(builder.Arrangements) == str(options.IndividuallyPositioned)
        finally:
            builder.Destroy()
    return None


def varied(tx, component) -> bool | None:
    """Whether ``component`` is positioned per arrangement (None: NX cannot tell)."""
    return _varied(tx, component)


def _move_in(tx, component, arrangement, target) -> None:
    """Move ``component`` to the absolute ``target`` pose in ``arrangement`` (the active
    one) through the positioner's move network, as NX's Move Component does
    (``MoveComponent`` would move a varied component in every arrangement); read back."""
    positioner = _assembly(tx).Positioner
    shift, delta = kpose.move_args(structure.pose(component), target)
    with native(default="NX_BUILD_FAILED"):
        positioner.ClearNetwork()
        positioner.PrimaryArrangement = arrangement
        positioner.BeginMoveComponent()
        try:
            network = positioner.EstablishNetwork()
            network.MoveObjectsState = True
            network.NetworkArrangementsMode = (
                tx.nx.Positioning.ComponentNetwork.ArrangementsMode.Existing
            )
            network.SetMovingGroup([component])
            network.BeginDrag()
            # DragByTransform takes the matrix rows as R's rows (MoveComponent: columns).
            network.DragByTransform(
                tx.nx.Vector3d(*shift), builders.matrix(tx, geom.transpose(delta))
            )
            network.EndDrag()
            network.ResetDisplay()
            network.ApplyToModel()
        finally:
            positioner.ClearNetwork()
            positioner.EndMoveComponent()
    model.update(tx)
    structure.expect_pose(structure.pose(component), target)


# ---------------------------------------------------------------------------
# Tools

_REQUIRES = ("work_part", "no_active_sketch", "assembly")


@tool(
    effect="model",
    label="Create arrangement",
    summary="Arrangement {name}",
    keywords=(
        "arrangement",
        "assembly arrangement",
        "assembly state",
        "open closed position",
        "configuration",
        "alternate positions",
    ),  # fmt: skip
    requires=_REQUIRES,
    cost="light",
    example={"name": "HATCH_OPEN", "activate": True},
)
def nx_create_arrangement(
    tx: Tx,
    *,
    name: Name,
    copy_from: Annotated[str, Doc("Arrangement to copy; default the active one"), _NAME_TEXT]
    | None = None,
    activate: Annotated[bool, Doc("Make it the active (shown) arrangement")] = False,
) -> ArrangementData:
    """Create a named arrangement of the work assembly as a copy of an existing one (poses
    and suppression). Then set its states with nx_set_arrangement_component; switch with
    nx_edit_arrangement action activate."""
    with tx.checks():
        assembly = _assembly(tx)
        base = (
            _find(assembly, copy_from, "/copy_from")
            if copy_from is not None
            else assembly.ActiveArrangement
        )
        if base is None:
            fail("NX_INVALID_ARGUMENT", "The assembly has no arrangement to copy",
                 field="/copy_from")  # fmt: skip
        _clash(assembly, name, "/name")
    with native(default="NX_BUILD_FAILED"):
        made = assembly.Arrangements.Create(base, str(name))
    readback.expect(made is not None, "arrangement", "NX created no arrangement")
    readback.expect(_key(made.Name) == _key(name), "name", "NX named the arrangement otherwise",
                    actual=str(made.Name))  # fmt: skip
    if activate:
        _activate(tx, made)
    return {
        "arrangement": _row(tx, made),
        "previous_name": None,
        "arrangements": _arrangement_rows(tx),
        "components": _components(tx),
    }


@tool(
    effect="model",
    label="Edit arrangement",
    summary={
        "activate": "Activate arrangement {arrangement}",
        "rename": "Rename arrangement {arrangement} to {new_name}",
        "set_default": "Default arrangement {arrangement}",
        "delete": "Delete arrangement {arrangement}",
    },
    keywords=(
        "activate arrangement",
        "switch arrangement",
        "rename arrangement",
        "delete arrangement",
        "default arrangement",
    ),  # fmt: skip
    requires=_REQUIRES,
    variants="action",
    required_by={"rename": ("new_name",)},
    cost="light",
    invalidates="deleted",
    example={"arrangement": "HATCH_OPEN", "action": "activate"},
)
def nx_edit_arrangement(
    tx: Tx,
    *,
    arrangement: ArrangementName,
    action: Literal["activate", "rename", "set_default", "delete"],
    new_name: Annotated[str, Doc("rename: the new name"), _NAME_TEXT] | None = None,
) -> ArrangementData:
    """Activate, rename, set as default or delete a named arrangement of the work assembly.
    activate shows it (its poses and suppression); set_default: the one used where this
    assembly is a component; delete refuses the active, the default and the last one.
    Returns all arrangements and the component poses in the active one."""
    if action != "rename" and new_name is not None:
        fail("NX_INVALID_ARGUMENT", f"{action} takes no new_name", field="/new_name")
    with tx.checks():
        assembly = _assembly(tx)
        target = _find(assembly, arrangement, "/arrangement")
        if action == "rename":
            assert new_name is not None  # gate 1: required_by
            _clash(assembly, new_name, "/new_name", keep=target)
        if action == "delete":
            if len(_all(assembly)) < 2:
                fail("NX_INVALID_ARGUMENT", "An assembly keeps at least one arrangement",
                     field="/arrangement")  # fmt: skip
            for word, other in (("active", assembly.ActiveArrangement), ("default", _default(tx))):
                if display.same(other, target):
                    fail("NX_INVALID_ARGUMENT", f"{target.Name} is the {word} arrangement",
                         field="/arrangement",
                         hint=f"Make another arrangement {word} first")  # fmt: skip
    previous = str(target.Name)
    active_before = assembly.ActiveArrangement
    default_before = _default(tx)
    if action == "activate":
        _activate(tx, target)
        row = _row(tx, target)
        if active_before is not None:
            tx.note_restore({"action": "activate", "before": str(active_before.Name),
                             "after": previous})  # fmt: skip
    elif action == "rename":
        with native(default="NX_BUILD_FAILED"):
            target.SetName(str(new_name))
        readback.expect(_key(target.Name) == _key(new_name), "name",
                        "NX did not rename the arrangement", actual=str(target.Name))  # fmt: skip
        row = _row(tx, target)
        tx.note_restore({"action": "rename", "before": previous, "after": str(target.Name)})
    elif action == "set_default":
        with native(default="NX_BUILD_FAILED"):
            assembly.SetDefault(target)
        readback.expect(display.same(_default(tx), target), "default",
                        "NX did not make it the default")  # fmt: skip
        row = _row(tx, target)
        if default_before is not None:
            tx.note_restore({"action": "set_default", "before": str(default_before.Name),
                             "after": previous})  # fmt: skip
    else:
        with native(default="NX_BUILD_FAILED"):
            target.Delete(False)
        model.update(tx)
        readback.expect(all(_key(a.Name) != _key(previous) for a in _all(assembly)), "deleted",
                        "NX kept the arrangement")  # fmt: skip
        row = {"name": previous, "active": False, "default": False}
    return {
        "arrangement": row,
        "previous_name": previous if action == "rename" else None,
        "arrangements": _arrangement_rows(tx),
        "components": _components(tx),
    }


@tool(
    effect="model",
    label="Set component in arrangement",
    summary="Set {component} in arrangement {arrangement}",
    keywords=(
        "arrangement position",
        "arrangement suppression",
        "component state per arrangement",
        "vary position",
        "position in arrangement",
    ),  # fmt: skip
    requires=_REQUIRES,
    cost="light",
    invalidates="deleted",
    example={
        "arrangement": "HATCH_OPEN",
        "component": "o:4",
        "pose": {"translation": [0, 0, 120], "rotation": {"axis": "x", "angle": 60}},
    },
)
def nx_set_arrangement_component(
    tx: Tx,
    *,
    arrangement: ArrangementName,
    component: ComponentId,
    pose: Annotated[ComponentPose, Doc("Absolute pose in this arrangement only")] | None = None,
    suppressed: Annotated[bool, Doc("Suppress (true) or unsuppress in this arrangement")]
    | None = None,
) -> ComponentState:
    """Set a direct component's pose and/or suppression in one arrangement only. A pose makes
    the component positioned per arrangement (other arrangements keep their poses); it is
    absolute in the assembly frame, unrotated without rotation, as nx_set_component_pose."""
    if pose is None and suppressed is None:
        fail("NX_INVALID_ARGUMENT", "Give pose, suppressed or both", field="/pose")
    target_pose = common.pose(pose) if pose is not None else None
    with tx.checks():
        assembly = _assembly(tx)
        target = _find(assembly, arrangement, "/arrangement")
        c = common.component(tx, component, "/component")
        if target_pose is not None and suppressed is None and _suppressed_in(tx, c, target):
            fail("NX_INVALID_ARGUMENT", f"{component} is suppressed in {target.Name}",
                     field="/pose", hint="Pass suppressed=false with the pose")  # fmt: skip
    other = next((a for a in _all(assembly) if not display.same(a, target)), None)
    before = _component_state(tx, c, target, with_pose=target_pose is not None)
    if suppressed is False:
        _suppress(tx, c, target, False)
    if target_pose is not None:
        kept = None
        if other is not None:
            with _active(tx, other):
                kept = structure.pose(c)
        if not _varied(tx, c):
            with native(default="NX_BUILD_FAILED"):
                c.SetPositioningVaried([c], True)
            readback.expect(_varied(tx, c) is not False, "varied",
                            "NX kept the component's pose shared by all arrangements")  # fmt: skip
        with _active(tx, target):
            _move_in(tx, c, target, target_pose)
        if other is not None:
            with _active(tx, other):
                got = structure.pose(c)
            readback.expect(kpose.close(got, kept, tol=1e-6), "other_pose",
                            f"The pose of {component} changed in {other.Name}")  # fmt: skip
    if suppressed is True:
        _suppress(tx, c, target, True)
    model.update(tx)
    with _active(tx, target):
        row = common.component_row(tx, c)
    tx.modified(row)
    after = _component_state(tx, c, target, with_pose=target_pose is not None)
    tx.note_restore({"component": component, "arrangement": str(target.Name),
                     "before": before, "after": after})  # fmt: skip
    return {
        "component": row,
        "arrangement": _row(tx, target),
        "suppressed": _suppressed_in(tx, c, target),
        "varied": bool(_varied(tx, c)),
    }


def _component_state(tx, component, arrangement, *, with_pose: bool) -> dict:
    """Plain values of a component in one arrangement for the compensating undo:
    suppression, whether it is positioned per arrangement and (``with_pose``) its pose
    there."""
    state: dict = {"suppressed": _suppressed_in(tx, component, arrangement),
                   "varied": _varied(tx, component), "pose": None}  # fmt: skip
    if with_pose:
        with _active(tx, arrangement):
            t, r = structure.pose(component)
        state["pose"] = [t, r]
    return state


def _suppress(tx, component, arrangement, on: bool) -> None:
    assembly = _assembly(tx)
    call = assembly.SuppressComponents if on else assembly.UnsuppressComponents
    with native(default="NX_BUILD_FAILED"):
        errors = call([component], [arrangement])
    structure.check(errors, "NX_BUILD_FAILED", "NX did not change the suppression")
    readback.expect(_suppressed_in(tx, component, arrangement) == on, "suppressed",
                    "The suppression did not change")  # fmt: skip


@tool(
    target="assembly",
    effect="read",
    label="List arrangements",
    summary="Arrangements",
    keywords=("arrangements", "list arrangements", "active arrangement", "assembly states"),
    requires=("work_part", "assembly"),
    cost="light",
    example={},
)
def nx_list_arrangements(tx: Tx) -> ArrangementList:
    """List the work assembly's arrangements: active, default and the components suppressed
    in each. Also lists its direct components with their poses in the active one and varied
    (positioned per arrangement; activate another arrangement to read its poses)."""
    assembly = _assembly(tx)
    kids = _children(tx)
    items: list[ArrangementState] = []
    for a in _all(assembly):
        row = cast(ArrangementState, _row(tx, a))
        row["suppressed"] = [tx.ref(c, "component") for c in kids if _suppressed_in(tx, c, a)]
        items.append(row)
    components = _components(tx)  # the first MAX_LISTED of kids, in order
    for listed, c in zip(components, kids, strict=False):
        listed["varied"] = _varied(tx, c)
    return {"items": items, "components": components, "truncated": len(kids) > MAX_LISTED}


def _changed_since(what: str) -> None:
    fail("NX_DEPENDENCY_ORDER", f"{what} changed since the operation; undo the later "
         "operations first or set it back yourself")  # fmt: skip


@restorer("nx_edit_arrangement")
def _restore_arrangement(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set back what nx_edit_arrangement's activate, rename or set_default changed (a
    delete is not restorable): refused when the arrangement changed again since."""
    assembly = _assembly(tx)
    action, before, after = data["action"], data["before"], data["after"]
    if action == "rename":
        found = [a for a in _all(assembly) if _key(a.Name) == _key(after)]
        if not found:
            _changed_since(f"Arrangement {after}")
        if apply:
            _clash(assembly, before, "/new_name", keep=found[0])
            with native(default="NX_BUILD_FAILED"):
                found[0].SetName(before)
            readback.expect(_key(found[0].Name) == _key(before), "name",
                            "NX did not rename the arrangement back")  # fmt: skip
        return f"arrangement {after} renamed back to {before}"
    current = assembly.ActiveArrangement if action == "activate" else _default(tx)
    word = "active" if action == "activate" else "default"
    if current is None or _key(current.Name) != _key(after):
        _changed_since(f"The {word} arrangement")
    if apply:
        previous = _find(assembly, before, "/arrangement")
        if action == "activate":
            _activate(tx, previous)
        else:
            with native(default="NX_BUILD_FAILED"):
                assembly.SetDefault(previous)
            readback.expect(display.same(_default(tx), previous), "default",
                            "NX did not make it the default again")  # fmt: skip
    return f"{before} the {word} arrangement again"


@restorer("nx_set_arrangement_component")
def _restore_arrangement_component(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set a component's pose, suppression and per-arrangement positioning in one
    arrangement back to what they were before nx_set_arrangement_component: refused when
    they changed since."""
    ref, before, after = data["component"], data["before"], data["after"]
    target = _find(_assembly(tx), data["arrangement"], "/arrangement")
    c = common.component(tx, ref, "/component")
    if _suppressed_in(tx, c, target) != after["suppressed"]:
        _changed_since(f"The suppression of {ref} in {target.Name}")
    if after["pose"] is not None:
        with _active(tx, target):
            now = structure.pose(c)
        wanted = (after["pose"][0], after["pose"][1])
        if not kpose.close(now, wanted, tol=1e-6 * max(1.0, *map(abs, wanted[0]))):
            _changed_since(f"The pose of {ref} in {target.Name}")
    if apply:
        if after["suppressed"] and before["pose"] is not None:
            _suppress(tx, c, target, False)  # move it shown; suppressed again below
        if before["pose"] is not None:
            pose = (before["pose"][0], before["pose"][1])
            with _active(tx, target):
                _move_in(tx, c, target, pose)
            if before["varied"] is False and _varied(tx, c):
                with native(default="NX_BUILD_FAILED"):
                    c.SetPositioningVaried([c], False)
                readback.expect(_varied(tx, c) is not True, "varied",
                                "NX kept the component positioned per arrangement")  # fmt: skip
                with _active(tx, target):
                    structure.expect_pose(structure.pose(c), pose, "previous_pose")
        if _suppressed_in(tx, c, target) != before["suppressed"]:
            _suppress(tx, c, target, before["suppressed"])
        model.update(tx)
    state = "suppressed" if before["suppressed"] else "unsuppressed"
    where = f" at {before['pose'][0]}" if before["pose"] is not None else ""
    return f"{ref} in {target.Name} back to {state}{where}"
