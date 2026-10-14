"""Model overview and health (tool-surface 3.9).

``nx_check_model`` reads native feature errors and warnings, suppression, unloaded or
suppressed components, unsolved assembly constraints (the solver status NX keeps: a
constraint whose edge a prototype edit consumed reports CannotSolve, an error; NotSolved
or OutOfDate is a warning), UF body consistency, self-overlapping solids (Examine
Geometry's body face intersections, ``kit.bodies.clashing_faces``: an error naming the
faces; a tube wider than its helix pitch passes UF consistency) and sealed internal voids
(a solid body with more than one shell, ``voids``: an info issue with each void's facet
volume and box, since a hollow part is often intended); it does not run a model update.

Examine Geometry runs on NX's main thread and one body's check cannot be interrupted; its
time grows faster than the face count (about an hour on a 17,627-face solid, NX 2406).
By default it examines solids of at most ``kit.bodies.CLASH_FACES`` faces, and per call
at most ``CLASH_BODIES`` solids, ``CLASH_TOTAL_FACES`` faces and ``CLASH_SECONDS``
seconds; every solid left out gets an info issue. The void check reads every edge's faces
(slow on very large solids): it skips solids over ``CLASH_FACES`` faces with an info issue
too. ``clash=True`` examines every solid for both until the call's cost budget is spent.
Shared prototypes are checked once. ``healthy`` speaks for what was examined only:
``complete`` is false when a check left solids out, and ``skipped`` counts them per check
and reason (``face_limit``: one solid over the face limit; ``call_limit``: the per-call
solids, faces or seconds; ``budget``: the call's cost budget), so a partial check is
never read as a clean bill.
``nx_get_model_summary`` pages the feature tree, expressions, sketches and components,
and gives counts, bounds and health for the overview: feature diagnostics, constraints
and body consistency, without the self-overlap and void checks.
"""

from __future__ import annotations

import contextlib
import time
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit
from nx_mcp.domains.inspection import voids
from nx_mcp.kit import bodies, expr, measure, parts, positioning, result
from nx_mcp.kit import features as kfeatures

# Self-overlap (Examine Geometry) per call by default; clash=True lifts these limits.
CLASH_BODIES = 50  # solids examined
CLASH_TOTAL_FACES = 10_000  # faces of the examined solids, together
CLASH_SECONDS = 60.0  # no further solid is examined after this
CLASH_COST = "minutes to an hour per large solid, NX frozen meanwhile"
LISTED_FACES = 10  # clashing faces named per body
CLASH_ADVICE = (
    "the body overlaps itself (parts of a sweep path closer than its section, e.g. a tube "
    "wider than its helix pitch): use a smaller section or move the path apart"
)
CLASH_DOC = (
    f"Also check solids over {bodies.CLASH_FACES:,} faces for self-overlap (up to 1 h each, NX "
    "frozen) and voids"
)
VOID_NOTE = "fine when the part is meant to be hollow; no vent hole is needed"
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


class Skipped(TypedDict):
    check: str
    reason: str
    bodies: int
    faces: int


class HealthData(TypedDict):
    healthy: bool
    complete: bool
    skipped: list[Skipped]
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


def _skipped(tx, owner, where, body, faces) -> Issue:
    return {
        "severity": "info",
        "kind": "self_intersection",
        "part": where,
        "object": tx.ref(body, kind="body", part=owner),
        "message": f"self-intersection check skipped: {faces:,} faces (over "
        f"{bodies.CLASH_FACES:,}); pass clash: true to force ({CLASH_COST})",
    }


def _skip(skipped, check, reason, faces) -> None:
    """Count one solid a check left out in ``skipped`` (``{(check, reason): Skipped}``)."""
    if skipped is None:
        return
    row = skipped.setdefault(
        (check, reason), {"check": check, "reason": reason, "bodies": 0, "faces": 0}
    )
    row["bodies"] += 1
    row["faces"] += int(faces)


def _voids(tx, owner, where, body, *, force: bool, skipped=None) -> Issue | None:
    """The sealed-void info issue of ``body`` (more than one shell), or None; a body over
    ``kit.bodies.CLASH_FACES`` faces gets a skipped-check info issue unless ``force``."""
    if not force:
        faces = bodies.face_count(body)
        if faces > bodies.CLASH_FACES:
            _skip(skipped, "internal_void", "face_limit", faces)
            return {
                "severity": "info",
                "kind": "internal_void",
                "part": where,
                "object": tx.ref(body, kind="body", part=owner),
                "message": f"void check skipped: {faces:,} faces (over {bodies.CLASH_FACES:,}); "
                "pass clash: true to force (slow on large solids)",
            }
    with native():
        groups = voids.shells(body)
    if len(groups) < 2:
        return None
    return {
        "severity": "info",
        "kind": "internal_void",
        "part": where,
        "object": tx.ref(body, kind="body", part=owner),
        "message": f"{len(groups) - 1} sealed internal void(s): {VOID_NOTE}",
        "shells": len(groups),
        "voids": voids.voids(tx, body, groups),
    }


def _budget_spent(tx) -> bool:
    """``tx.checkpoint()``; True instead of raising when the call's cost budget is spent
    (a cancel request still raises)."""
    try:
        tx.checkpoint()
    except NXError as error:
        if (error.details or {}).get("reason") != "budget":
            raise
        return True
    return False


def clashes(tx, solids, *, force: bool = False, skipped=None) -> list[Issue]:
    """Self-overlap issues of ``solids`` (``(owner, where, body)`` rows): errors, plus
    info issues for the solids left out (over the face limit or the call's budgets),
    counted in ``skipped`` (``_skip``) when given.
    ``force`` lifts the face limit and the budgets of this module; the call's cost budget
    still stops it (between bodies: one body's check cannot be interrupted)."""
    issues: list[Issue] = []
    started, examined, total, left = time.monotonic(), 0, 0, 0
    reason = "call_limit"
    for owner, where, body in solids:
        faces = bodies.face_count(body)
        if not force and faces > bodies.CLASH_FACES:
            issues.append(_skipped(tx, owner, where, body, faces))
            _skip(skipped, "self_intersection", "face_limit", faces)
            continue
        spent = not force and (
            examined >= CLASH_BODIES
            or total + faces > CLASH_TOTAL_FACES
            or time.monotonic() - started > CLASH_SECONDS
        )
        if not left and not spent and _budget_spent(tx):
            reason = "budget"
            spent = True
        if left or spent:
            left += 1
            _skip(skipped, "self_intersection", reason, faces)
            continue
        examined, total = examined + 1, total + faces
        clash = _clash(tx, owner, where, body)
        if clash is not None:
            issues.append(clash)
    if left:
        if force:
            why = "the call's time budget was spent"
        else:
            why = (
                f"the check stops at {CLASH_BODIES} solids, {CLASH_TOTAL_FACES:,} faces or "
                f"{CLASH_SECONDS:g} s per call; pass clash: true to examine them all ({CLASH_COST})"
            )
        issues.append(
            {
                "severity": "info",
                "kind": "self_intersection",
                "message": f"{left} solid bodies were not examined for self-overlap: {why}",
            }
        )
    return issues


def diagnose(
    tx,
    scope="part",
    *,
    clash: Literal["auto", "all", "off"] = "auto",
    find_voids: bool = True,
    skipped: dict | None = None,
) -> tuple[list[Issue], Counts]:
    """``(issues, counts)`` of the work part or the whole loaded assembly (solids a check
    left out are counted in ``skipped``, ``_skip``, when given). ``clash``:
    self-overlap on solids within the face limit and budgets (auto), on every solid until
    the cost budget is spent (all), or not at all (off); ``find_voids``: sealed voids, on
    solids within the face limit unless ``clash`` is all."""
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
    examine: list[tuple[Any, str, Any]] = []
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
            if solids % 20 == 0:
                tx.checkpoint()
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
            if not getattr(body, "IsSolidBody", False):
                continue
            examine.append((owner, where, body))
            void = (
                _voids(tx, owner, where, body, force=clash == "all", skipped=skipped)
                if find_voids
                else None
            )
            if void is not None:
                issues.append(void)
    if clash != "off":
        issues.extend(clashes(tx, examine, force=clash == "all", skipped=skipped))
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
    cost="heavy",
    example={"scope": "part"},
)
def nx_check_model(
    tx: Tx,
    *,
    scope: Annotated[Literal["part", "assembly"], Doc("assembly adds loaded components")] = "part",
    clash: Annotated[bool, Doc(CLASH_DOC)] = False,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> HealthData:
    """Check feature errors/warnings, suppression, unloaded components, unsolved constraints
    and bodies. Bodies: consistency, self-overlap, sealed voids (info: fine if intended).
    healthy: no errors/warnings in what was examined; complete false: some solids were
    not examined (skipped), not a clean bill. No update."""
    left: dict = {}
    issues, counts = diagnose(tx, scope, clash="all" if clash else "auto", skipped=left)
    selected, page = result.page(issues, offset, limit)
    return {
        "healthy": counts["errors"] == 0 and counts["warnings"] == 0,
        "complete": not left,
        "skipped": list(left.values()),
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
    issues, counts = diagnose(tx, "part", clash="off", find_voids=False)
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
    cost="heavy",
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
