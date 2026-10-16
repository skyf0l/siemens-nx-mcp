"""Deformed components: the deformable definition of a part, the assembly deformations of
its components, their recorded min/max ranges and the bodies they made.

    deform.definition(part)                # FLEXIBLE_PART feature of a part, or None
    deform.ranges(tx, definition)          # {expression: [min, max]} recorded with it
    deform.deformation(tx, asm, c)         # FLEXED_PART of asm that deforms c, or None
    deform.flexed_bodies(flexed)           # assembly bodies the deformation copied
    deform.bodies_of(tx, c)                # those bodies for a deformed component ([] if none)
    deform.out_of_range(tx, asm)           # deformed values now outside their range
    deform.range_messages(tx, asm)         # the same rows as warning texts

NX 2406 findings encoded here (probed live):

- A deformation (``FLEXED_PART`` in the assembly) copies the part's member features
  into the assembly: the deformed geometry is assembly bodies that follow the
  component's pose, and the component itself shows no body any more.
- A deformed value may be a formula of assembly expressions, re-evaluated by every model
  update; NX enforces neither the min nor the max of the definition through the API, and
  2406 cannot read them back, so ``nx_create_deformable_definition`` records them in the
  ``NXMCP_DEFORM_RANGES`` string attribute of the ``FLEXIBLE_PART``. :func:`out_of_range`
  compares the current values with that record.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import native
from nx_mcp.kit import expr, parts, records

FLEXIBLE, FLEXED = "FLEXIBLE_PART", "FLEXED_PART"
RANGE_ATTRIBUTE = "NXMCP_DEFORM_RANGES"
MAX_ROWS = 20


def definition(part):
    """The ``FLEXIBLE_PART`` feature of ``part``, or None."""
    return next((f for f in getattr(part, "Features", None) or [] if f.FeatureType == FLEXIBLE),
                None)  # fmt: skip


def ranges(tx, feature) -> dict:
    """``{name: [min, max]}`` recorded by nx_create_deformable_definition ({} if none)."""
    try:
        return dict(records.get_json(tx, feature, RANGE_ATTRIBUTE, {}))
    except Exception:  # no attribute support or an unreadable record: no ranges
        return {}


def _flexed(assembly) -> list:
    return [f for f in getattr(assembly, "Features", None) or [] if f.FeatureType == FLEXED]


def _occurrence_tag(tx, flexed) -> int:
    with native(default="NX_ERROR"):
        data = tx.uf.Assem.AskDeformedDefinitionData(flexed.Tag)
    return int(data.PartOcc)


def deformation(tx, assembly, component):
    """The ``FLEXED_PART`` feature of ``assembly`` that deforms ``component``, or None."""
    for feature in _flexed(assembly):
        if _occurrence_tag(tx, feature) == int(component.Tag):
            return feature
    return None


def flexed_bodies(flexed) -> list:
    """Bodies of the assembly features a deformation copied."""
    out, seen = [], set()
    for parent in flexed.GetParents():
        for body in parent.GetBodies() if hasattr(parent, "GetBodies") else []:
            if int(body.Tag) not in seen:
                seen.add(int(body.Tag))
                out.append(body)
    return out


def bodies_of(tx, component) -> list:
    """The assembly bodies that show ``component`` deformed ([] when it is not deformed
    in its parent assembly, or NX cannot tell)."""
    owner = getattr(component, "OwningPart", None)
    if owner is None:
        return []
    with contextlib.suppress(Exception):
        flexed = deformation(tx, owner, component)
        if flexed is not None:
            return flexed_bodies(flexed)
    return []


def out_of_range(tx, assembly) -> list[dict]:
    """``[{component, expression, value, min, max}]`` of deformed values of ``assembly``'s
    components that lie outside the range recorded with their part's definition (a formula
    of assembly expressions can drive one out after it was set), at most 20."""
    flexed_features = _flexed(assembly)
    if not flexed_features:
        return []
    by_tag = {int(c.Tag): c for c, _names in parts.components(assembly)}
    rows: list[dict] = []
    for flexed in flexed_features:
        with contextlib.suppress(Exception):
            component = by_tag.get(_occurrence_tag(tx, flexed))
            own = definition(getattr(component, "Prototype", None)) if component else None
            limits = ranges(tx, own) if own is not None else {}
            if own is None or not limits:
                continue
            order = [str(e.Name) for e in own.GetExpressions()]
            mine = list(flexed.GetExpressions())
            for name, (low, high) in sorted(limits.items()):
                if name not in order or order.index(name) >= len(mine):
                    continue
                value = expr.value_or_none(tx, mine[order.index(name)])
                tol = 1e-9 * max(1.0, abs(low), abs(high))
                if value is not None and not low - tol <= value <= high + tol:
                    rows.append(
                        {
                            "component": tx.ref(component, "component"),
                            "expression": name,
                            "value": value,
                            "min": low,
                            "max": high,
                        }
                    )
        if len(rows) >= MAX_ROWS:
            break
    return rows[:MAX_ROWS]


def range_messages(tx, assembly) -> list[str]:
    """:func:`out_of_range` as warning texts (the caller warns with them)."""
    return [
        f"Deformed {row['component'].get('id')} ({row['component'].get('name')}): "
        f"{row['expression']} = {row['value']:g} lies outside its range "
        f"[{row['min']:g}, {row['max']:g}]; change the driving expressions or the "
        "deformation (nx_set_component_deformation)"
        for row in out_of_range(tx, assembly)
    ]
