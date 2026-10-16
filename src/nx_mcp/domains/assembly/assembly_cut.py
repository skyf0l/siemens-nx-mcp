"""Assembly cut: subtract tool components from target components in the assembly context
(a deck cut-out for a hatch, a bulkhead pass-through), leaving the component parts as
they are.

NX findings encoded here:

- ``Features.CreateAssemblyCutBuilder(None)`` in the assembly (work part): target and
  tool occurrence bodies, ``HideToolBodyFlag``. The feature promotes the target bodies
  into the assembly and subtracts the tools there (``GetPromotionFeatures`` /
  ``GetBooleanBodyFeatures``); the component parts keep their solids.
- ``GetOperationFailures`` lists per-body failures (a tool that misses a target).
- The cut places the components as in the assembly's default arrangement, not the
  active one: a component positioned per arrangement is cut where the default puts it
  (live: a pin at x=50.8 in the default bored there while the active arrangement showed
  it at x=50). The cut is refused while the active arrangement is not the default and a
  listed component (or a parent) is positioned per arrangement.
- After a cut the components' occurrences show no solid any more (``FindOccurrence``
  finds none): the promoted bodies in the assembly carry the result, so a second cut
  of the same component is refused with the existing cut named.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items
from nx_mcp.domains.assembly import arrangements, common, structure
from nx_mcp.domains.assembly.common import ComponentId
from nx_mcp.kit import (
    bodies,
    builders,
    display,
    licenses,
    measure,
    model,
    parts,
    readback,
    result,
    select,
)

MAX_COMPONENTS = 50
CUT_TYPE = "ASSEMBLY_CUT"


class CutTarget(TypedDict):
    component: result.Row
    volume_before: float
    volume_after: float
    removed: float


class CutData(TypedDict):
    feature: result.Row
    targets: list[CutTarget]
    tools: list[result.Row]
    bodies: list[result.Row]


def _solids(tx, ref, field):
    """An unsuppressed component of the work assembly (any level) and its solid bodies."""
    c = select.resolve(tx, ref, "component", field=field)
    if not any(int(o.Tag) == int(c.Tag) for o, _names in parts.components(tx.part)):
        fail("NX_WRONG_OWNER", f"{ref} is not a component of the work part", field=field,
             hint=common.OWNER_HINT)  # fmt: skip
    if bodies.suppressed(c):
        fail("NX_INVALID_ARGUMENT", f"{ref} is suppressed; unsuppress it first", field=field)
    found = [b for b in bodies.occurrences(c) if getattr(b, "IsSolidBody", False)]
    if not found:
        _cut_already(tx, ref, c, field)
        fail("NX_WRONG_GEOMETRY", f"{ref} has no solid body (is its part loaded?)",
             field=field)  # fmt: skip
    return c, found


def _cuts(part) -> list:
    """The assembly cut features of ``part`` (``Features.AssemblyCut``: its feature type, or
    the promotion list only an assembly cut has)."""
    out = []
    for feature in getattr(part, "Features", None) or []:
        with contextlib.suppress(Exception):
            kind = str(feature.FeatureType).upper().replace(" ", "_")
            if kind.startswith(CUT_TYPE) or hasattr(feature, "GetPromotionFeatures"):
                out.append(feature)
    return out


def _cut_already(tx, ref, component, field) -> None:
    """Refuse a component whose solids an existing assembly cut promoted (its occurrence
    then shows none), naming the cut and its result bodies."""
    prototype = getattr(component, "Prototype", None)
    solids = [
        b for b in getattr(prototype, "Bodies", None) or [] if getattr(b, "IsSolidBody", False)
    ]
    cuts = _cuts(tx.part)
    if not solids or not cuts:
        return
    made: list = []
    for feature in cuts:
        with contextlib.suppress(Exception):
            made.extend(b for f in feature.GetBooleanBodyFeatures() for b in f.GetBodies())
    fail(
        "NX_WRONG_GEOMETRY",
        f"{ref} shows no solid: an assembly cut already promoted it into the assembly "
        f"({', '.join(str(tx.ref(f, 'feature').get('name')) for f in cuts[:3])})",
        field=field,
        hint="NX cuts a component once: delete the cut (nx_delete_objects on details.cuts) "
        "and cut again with every tool in one call; the cut result is in details.bodies",
        details={
            "cuts": [tx.ref(f, "feature") for f in cuts[:10]],
            "bodies": [tx.ref(b, "body") for b in made[:10]],
        },
    )


def _check_arrangement(tx, chosen) -> None:
    """Refuse when the active arrangement is not the default one and a listed component
    (or a parent of it) is positioned per arrangement: NX cuts at the default's poses."""
    assembly = tx.part.ComponentAssembly
    active = getattr(assembly, "ActiveArrangement", None)
    default = arrangements.default_arrangement(tx)
    if active is None or default is None or display.same(active, default):
        return
    root = structure.root(tx.part)
    for field, ref, c in chosen:
        node = c
        while node is not None and not display.same(node, root):
            if arrangements.varied(tx, node):
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"Assembly Cut places components as the default arrangement "
                    f"{default.Name} does, but {active.Name} is active and {ref} is "
                    "positioned per arrangement",
                    field=field,
                    hint=f"Make {active.Name} the default first (nx_edit_arrangement "
                    f"action set_default), or activate {default.Name}",
                    details={"active": str(active.Name), "default": str(default.Name)},
                )
            node = getattr(node, "Parent", None)


@tool(
    effect="model",
    label="Assembly cut",
    summary=lambda a, say: f"Cut {say('targets')} by {say('tools')}",
    keywords=(
        "assembly cut",
        "cut component",
        "cut-out in assembly",
        "subtract component",
        "deck opening",
        "context boolean",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    example={"targets": ["o:4"], "tools": ["o:7"]},
)
def nx_create_assembly_cut(
    tx: Tx,
    *,
    targets: Annotated[list[ComponentId], Items(1, MAX_COMPONENTS), Doc("Components cut")],
    tools: Annotated[list[ComponentId], Items(1, MAX_COMPONENTS), Doc("Components that cut")],
    hide_tools: Annotated[bool, Doc("Hide the tools' bodies promoted into the assembly")] = True,
) -> CutData:
    """Cut target components' solids by tool components' solids in the work assembly
    (Assembly Cut). The cut exists in the assembly only: component parts keep their
    solids. Components sit as in the default arrangement. A component is cut once: list
    every tool in one call. Returns each target's volume before and after."""
    with tx.checks():
        picked: dict[int, str] = {}
        chosen = []
        placed: list = []
        for name, refs in (("targets", targets), ("tools", tools)):
            for i, ref in enumerate(refs):
                field = f"/{name}/{i}"
                c, found = _solids(tx, ref, field)
                if int(c.Tag) in picked:
                    fail("NX_INVALID_ARGUMENT", f"{ref} is already listed in {picked[int(c.Tag)]}",
                         field=field)  # fmt: skip
                picked[int(c.Tag)] = name
                chosen.append((name, c, found))
                placed.append((field, ref, c))
        _check_arrangement(tx, placed)
    target_items = [(c, found) for name, c, found in chosen if name == "targets"]
    tool_items = [(c, found) for name, c, found in chosen if name == "tools"]
    before = [measure.volume(tx, found) for _c, found in target_items]
    part = tx.part
    with (
        licenses.attempt(tx, ("solid_modeling",)),
        builders.build(tx, part.Features.CreateAssemblyCutBuilder, None) as b,
    ):
        with native(default="NX_BUILD_FAILED"):
            b.TargetBody.Add([body for _c, found in target_items for body in found])
            b.ToolBody.Add([body for _c, found in tool_items for body in found])
            b.HideToolBodyFlag = bool(hide_tools)
        feature = b.commit()
        failures = b.GetOperationFailures() if b.has("GetOperationFailures") else None
    structure.check(failures, "NX_BUILD_FAILED", "NX could not cut every target")
    readback.expect(feature is not None, "feature", "NX created no assembly cut")
    model.update(tx)
    made = [body for f in feature.GetBooleanBodyFeatures() for body in f.GetBodies()]
    readback.expect(bool(made), "bodies", "The assembly cut made no body")
    out: list[CutTarget] = []
    for (c, found), vol in zip(target_items, before, strict=True):
        after = measure.volume(tx, found)  # the occurrence now shows the cut result
        out.append({"component": common.component_row(tx, c, with_pose=False),
                    "volume_before": vol, "volume_after": after, "removed": vol - after})  # fmt: skip
    total_before = sum(before)
    total_after = measure.volume(tx, made)
    readback.expect_close(sum(t["volume_after"] for t in out), total_after, "volume_after",
                          tol=1e-6 * max(1.0, total_before))  # fmt: skip
    readback.expect(total_after < total_before * (1 - 1e-9), "volume",
                    "The tools removed no material from the targets",
                    before=total_before, after=total_after)  # fmt: skip
    return {
        "feature": tx.ref(feature, kind="feature"),
        "targets": out,
        "tools": [common.component_row(tx, c, with_pose=False) for c, _f in tool_items],
        "bodies": [tx.ref(body, kind="body") for body in made],
    }
