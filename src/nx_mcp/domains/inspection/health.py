"""Model overview and health (tool-surface 3.9).

``nx_check_model`` reads native feature errors and warnings, suppression, unloaded or
suppressed components, unsolved assembly constraints (the solver status NX keeps: a
constraint whose edge a prototype edit consumed reports CannotSolve, an error; NotSolved
or OutOfDate is a warning), UF body consistency, self-overlapping solids (Examine
Geometry's body face intersections, ``kit.bodies.clashing_faces``: an error naming the
faces; a tube wider than its helix pitch passes UF consistency) and sealed internal voids
(a solid body with more than one shell, ``voids``: a warning with each void's facet
volume and box); it does not run a model update. Examine Geometry runs on the first
``CLASH_BODIES`` solids of a call (an info issue counts the rest).
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
from nx_mcp.kit import bodies, expr, measure, parts, positioning, result
from nx_mcp.kit import features as kfeatures

CLASH_BODIES = 50  # solids examined for self-overlap per call (Examine Geometry's cost)
LISTED_FACES = 10  # clashing faces named per body
CLASH_ADVICE = (
    "the body overlaps itself (parts of a sweep path closer than its section, e.g. a tube "
    "wider than its helix pitch): use a smaller section or move the path apart"
)
CONSTRAINT_ADVICE = (
    "its geometry may be gone after a part edit; re-create it on current geometry, or edit "
    "or suppress it with nx_edit_assembly_constraint"
)


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
    status: str
    components: list[str]
    faces: list[result.Row]


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


def _clash(tx, owner, where, body) -> Issue | None:
    """The self-overlap error of ``body`` (Examine Geometry), or None."""
    found = bodies.clashing_faces(tx, body, owner)  # NX lists each clashing pair: repeats
    faces = list({int(f.Tag): f for f in found}.values())
    if not faces:
        return None
    named = []
    for face in faces[:LISTED_FACES]:
        with contextlib.suppress(Exception):
            named.append(tx.ref(face, kind="face", part=owner))
    return {
        "severity": "error",
        "kind": "self_intersection",
        "part": where,
        "object": tx.ref(body, kind="body", part=owner),
        "message": f"{len(faces)} faces cut other faces of the body: {CLASH_ADVICE}",
        "faces": named,
    }


def diagnose(tx, scope="part", *, clashes: bool = True) -> tuple[list[Issue], Counts]:
    """``(issues, counts)`` of the work part or the whole loaded assembly; ``clashes``
    examines the first ``CLASH_BODIES`` solids for self-overlap."""
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
    features = solids = examined = skipped = 0
    for owner in checked.values():
        where = tx.services.part_path(owner)
        for row in positioning.unsolved(tx, owner):
            issues.append(
                {
                    "severity": row["severity"],
                    "kind": "assembly_constraint",
                    "part": where,
                    "object": row["constraint"],
                    "status": row["status"],
                    "components": row["components"],
                    "message": f"{row['type'] or 'assembly'} constraint is {row['status']}: "
                    + CONSTRAINT_ADVICE,
                }
            )
        # Feature.Suppressed is also True for features rolled back after the current
        # feature; only the user's suppressions are issues.
        held = {t for t, (_f, user, _a) in kfeatures.states(tx, owner).items() if user}
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
            if int(feature.Tag) in held:
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
            if clashes and getattr(body, "IsSolidBody", False):
                if examined < CLASH_BODIES:
                    examined += 1
                    tx.checkpoint()
                    clash = _clash(tx, owner, where, body)
                    if clash is not None:
                        issues.append(clash)
                else:
                    skipped += 1
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
    if skipped:
        issues.append(
            {
                "severity": "info",
                "kind": "self_intersection",
                "message": f"{skipped} solid bodies past the first {CLASH_BODIES} were not "
                "examined for self-overlap",
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
    """Check feature errors/warnings, suppression, unloaded components, unsolved constraints
    and bodies. Bodies: consistency, self-overlap, sealed voids. healthy: no errors or
    warnings; no update."""
    issues, counts = diagnose(tx, scope)
    selected, page = result.page(issues, offset, limit)
    return {
        "healthy": counts["errors"] == 0 and counts["warnings"] == 0,
        **counts,
        "issues": selected,
        "page": page,
    }


visible_features = kfeatures.timeline  # the Part Navigator's features, timeline order


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
            "features": len(visible_features(part)),
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
        state = {}
        for f in selected:  # by owning part: user suppression vs rolled back (inactive)
            part = getattr(f, "OwningPart", None) or tx.part
            if int(part.Tag) not in state:
                state[int(part.Tag)] = kfeatures.states(tx, part)
        rows = []
        for f in selected:
            part = getattr(f, "OwningPart", None) or tx.part
            _f, user, active = state[int(part.Tag)].get(
                int(f.Tag), (f, bool(getattr(f, "Suppressed", False)), True)
            )
            rows.append(
                {
                    **tx.ref(f, kind="feature"),
                    "type": str(f.FeatureType).lower(),
                    "suppressed": user,
                    "active": active,
                    "parents": [tx.ref(p, kind="feature")["id"] for p in f.GetParents()],
                }
            )
        return rows
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
        "features": lambda: visible_features(part),
        "expressions": lambda: sorted(part.Expressions, key=lambda e: str(e.Name)),
        "sketches": lambda: list(part.Sketches),
    }[section]()
    selected, page = result.page(pool, offset, limit)
    return {"section": section, "items": _section_rows(tx, section, selected), "page": page}
