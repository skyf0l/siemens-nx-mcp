"""Model overview and health (tool-surface 3.9).

``nx_check_model`` reads native feature errors and warnings, suppression, unloaded or
suppressed components, UF body consistency and sealed internal voids (a solid body
with more than one shell, ``voids``: a warning with each void's facet volume and box);
it does not run a model update.
Shared prototypes are checked once. ``nx_get_model_summary`` pages the feature tree,
expressions, sketches and components, and gives counts, bounds and health for the
overview.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit
from nx_mcp.domains.inspection import voids
from nx_mcp.kit import bodies, expr, measure, parts, result


class Issue(TypedDict, total=False):
    severity: str
    kind: str
    part: str
    object: result.Row
    path: list[str]
    message: str
    faults: list[dict[str, Any]]
    shells: int
    voids: list[dict[str, Any]]


class Counts(TypedDict):
    errors: int
    warnings: int
    parts_checked: int
    features_checked: int
    bodies_checked: int


class HealthData(TypedDict):
    healthy: bool
    errors: int
    warnings: int
    parts_checked: int
    features_checked: int
    bodies_checked: int
    issues: list[Issue]
    page: dict[str, Any]


def _fault(tx, owner, code, tag):
    fault: dict[str, Any] = {"code": int(code)}
    with contextlib.suppress(Exception):
        fault["message"] = str(tx.nx.NXException(int(code)).GetMessage())
    with contextlib.suppress(Exception):
        obj = tx.nx.TaggedObjectManager.GetTaggedObject(tag)
        kind = tx.kind_of(obj)
        if kind in ("face", "edge", "body"):
            fault["object"] = tx.ref(obj, kind=kind, part=owner)
    return fault


def diagnose(tx, scope="part") -> tuple[list[Issue], Counts]:
    """``(issues, counts)`` of the work part or the whole loaded assembly."""
    part = tx.part
    checked = {int(part.Tag): part}
    issues: list[Issue] = []
    if scope == "assembly":
        for component, path in parts.components(part):
            tx.checkpoint()
            prototype = getattr(component, "Prototype", None)
            if bodies.suppressed(component):
                if getattr(component, "IsSuppressed", False):
                    issues.append(
                        {"severity": "info", "kind": "suppressed_component", "path": path}
                    )
            elif prototype is None or not hasattr(prototype, "Features"):
                issues.append({"severity": "warning", "kind": "unloaded_component", "path": path})
            else:
                checked.setdefault(int(prototype.Tag), prototype)
    features = solids = 0
    for owner in checked.values():
        where = tx.services.part_path(owner)
        for feature in owner.Features:
            features += 1
            if features % 200 == 0:
                tx.checkpoint()
            with native():
                diagnostics = (
                    ("error", feature.GetFeatureErrorMessages()),
                    ("warning", feature.GetFeatureWarningMessages()),
                )
            for severity, messages in diagnostics:
                for message in messages or []:
                    issues.append(
                        {
                            "severity": severity,
                            "kind": "feature_diagnostic",
                            "part": where,
                            "object": tx.ref(feature, kind="feature", part=owner),
                            "message": str(message),
                        }
                    )
            if feature.Suppressed:
                issues.append(
                    {
                        "severity": "info",
                        "kind": "suppressed_feature",
                        "part": where,
                        "object": tx.ref(feature, kind="feature", part=owner),
                    }
                )
        for body in owner.Bodies:
            solids += 1
            with native():
                count, codes, tags = tx.uf.Modeling.AskBodyConsistency(body.Tag)
            if int(count):
                issues.append(
                    {
                        "severity": "error",
                        "kind": "body_consistency",
                        "part": where,
                        "object": tx.ref(body, kind="body", part=owner),
                        "faults": [
                            _fault(tx, owner, c, t) for c, t in zip(codes, tags, strict=False)
                        ],
                    }
                )
            if getattr(body, "IsSolidBody", False):
                with native():
                    groups = voids.shells(body)
                if len(groups) > 1:
                    issues.append(
                        {
                            "severity": "warning",
                            "kind": "internal_void",
                            "part": where,
                            "object": tx.ref(body, kind="body", part=owner),
                            "message": f"{len(groups) - 1} sealed internal void(s)",
                            "shells": len(groups),
                            "voids": voids.voids(tx, body, groups),
                        }
                    )
    counts: Counts = {
        "errors": sum(i["severity"] == "error" for i in issues),
        "warnings": sum(i["severity"] == "warning" for i in issues),
        "parts_checked": len(checked),
        "features_checked": features,
        "bodies_checked": solids,
    }
    return issues, counts


@tool(
    effect="read",
    label="Check model health",
    summary="Check model ({scope})",
    keywords=("model health", "feature errors", "diagnostics", "body check", "examine geometry"),
    requires=("work_part",),
    cost="light",
    example={"scope": "part"},
)
def nx_check_model(
    tx: Tx,
    *,
    scope: Annotated[Literal["part", "assembly"], Doc("assembly adds loaded components")] = "part",
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> HealthData:
    """Check feature errors and warnings, suppression, unloaded components, body
    consistency and sealed voids (warning). healthy: no errors or warnings; no update."""
    issues, counts = diagnose(tx, scope)
    selected, page = result.page(issues, offset, limit)
    return {
        "healthy": counts["errors"] == 0 and counts["warnings"] == 0,
        **counts,
        "issues": selected,
        "page": page,
    }


class SummaryData(TypedDict, total=False):
    section: str
    part: result.Row
    modified: bool
    counts: dict[str, int]
    bounds: dict[str, list[float]] | None
    health: dict[str, Any]
    items: list[dict[str, Any]]
    page: dict[str, Any]


def overview(tx):
    part = tx.part
    found = list(part.Bodies)
    bounds = None
    if found:
        low, high = measure.bounds(tx, found)
        bounds = {"min": low, "max": high, "size": [b - a for a, b in zip(low, high, strict=True)]}
    issues, counts = diagnose(tx, "part")
    return {
        "part": tx.ref(part, kind="part"),
        "modified": bool(part.IsModified),
        "counts": {
            "bodies": len(found),
            "features": len(list(part.Features)),
            "sketches": len(list(part.Sketches)),
            "expressions": len(list(part.Expressions)),
            "components": len(parts.components(part)),
        },
        "bounds": bounds,
        "health": {
            "healthy": counts["errors"] == 0 and counts["warnings"] == 0,
            "errors": counts["errors"],
            "warnings": counts["warnings"],
        },
    }


def _section_rows(tx, section, selected):
    if section == "components":
        return [
            {
                **tx.ref(c, kind="component"),
                "path": path,
                "depth": len(path) - 1,
                "suppressed": bool(getattr(c, "IsSuppressed", False)),
            }
            for c, path in selected
        ]
    if section == "features":
        return [
            {
                **tx.ref(f, kind="feature"),
                "type": str(f.FeatureType).lower(),
                "suppressed": bool(f.Suppressed),
                "parents": [tx.ref(p, kind="feature")["id"] for p in f.GetParents()],
            }
            for f in selected
        ]
    if section == "expressions":
        return [
            {
                **tx.ref(e, kind="expression"),
                "formula": str(e.RightHandSide),
                "value": expr.value(tx, e),
                "units": expr.units(e),
            }
            for e in selected
        ]
    return [{**tx.ref(s, kind="sketch"), "curves": len(list(s.GetAllGeometry()))} for s in selected]


@tool(
    effect="read",
    label="Get model summary",
    summary="Model summary: {section}",
    keywords=("model overview", "feature tree", "part summary", "counts", "part navigator"),
    requires=("work_part",),
    cost="light",
    example={"section": "overview"},
)
def nx_get_model_summary(
    tx: Tx,
    *,
    section: Literal["overview", "components", "features", "expressions", "sketches"] = "overview",
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> SummaryData:
    """Summarise the work part: counts, bounds and health, or one page of its features,
    expressions, sketches or components."""
    if section == "overview":
        return {"section": section, **overview(tx)}
    part = tx.part
    pool = {
        "components": lambda: parts.components(part),
        "features": lambda: list(part.Features),
        "expressions": lambda: sorted(part.Expressions, key=lambda e: str(e.Name)),
        "sketches": lambda: list(part.Sketches),
    }[section]()
    selected, page = result.page(pool, offset, limit)
    return {"section": section, "items": _section_rows(tx, section, selected), "page": page}
