"""Feature tree and expressions (tool-surface 3.3).

NX 2406 lessons kept here:

- ``Feature.Suppressed`` is True both for user-suppressed features and for
  features after the current feature; UF ``AskSuppressFeature`` tells them apart
  (mask 1 = suppressed by the user, 16 = inactive).
- ``Feature.GetChildren()`` is one-sided (a block extrude lists no children
  although a united boss lists it as parent), so children come from every
  feature's ``GetParents()``.
- Suppressing an inactive feature sets its flag but NX still builds it once the
  model rolls forward: suppression changes need active features.
- NX unsuppresses only the listed features; dependents stay suppressed.
- A model update leaves an active sketch unsolved after a driving expression
  changed; ``kit.sketch.solve`` (``LocalUpdate``) solves it and checks the
  driven dimensions.
- Bad formulas are native errors: ``NX_INVALID_FORMULA`` keeps ``nx_code``.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FeatureRef, Items, Pose, Text, forbid_extra
from nx_mcp.kit import args, expr, readback, result
from nx_mcp.kit import sketch as ksketch

ExpressionName = Annotated[
    str, Text(100, r"^[A-Za-z_][A-Za-z0-9_]{0,99}$"), Doc("Expression name (case-sensitive)")
]
Formula = Annotated[str, Text(4096), Doc("NX expression formula, e.g. 'p3*2' or '25'")]
MAX_FEATURES = 100


# ---------------------------------------------------------------------------
# Timeline state


def _internal(feature) -> bool:
    return bool(getattr(feature, "IsInternal", False))


def timeline(part) -> list:
    """User-visible features in timeline order (a sketch's internal datum system omitted).

    Timestamps are not unique, so the stable sort keeps NX's order for ties.
    """
    features = list(part.Features)
    if features and all(hasattr(f, "Timestamp") for f in features):
        features.sort(key=lambda f: int(f.Timestamp))
    return [f for f in features if not _internal(f)]


def states(tx, part) -> dict:
    """``{tag: (feature, suppressed, active)}`` in timeline order."""
    order = timeline(part)
    current = getattr(part, "CurrentFeature", None)
    tags = [int(f.Tag) for f in order]
    cut = tags.index(int(current.Tag)) if current is not None and int(current.Tag) in tags else None
    ask = getattr(getattr(tx.uf, "Modeling", None), "AskSuppressFeature", None)
    out = {}
    for i, f in enumerate(order):
        active = cut is None or i <= cut
        if ask is not None:
            code = int(ask(f.Tag))
            suppressed = bool(code & 1)
            active = active and not code & 16
        else:
            suppressed = bool(getattr(f, "Suppressed", False)) and active
        out[int(f.Tag)] = (f, suppressed, active)
    return out


def child_map(features) -> dict:
    """``{parent tag: {child tags}}`` from every feature's parents (and children)."""
    children: dict = {}
    for f in features:
        tag = int(f.Tag)
        for p in f.GetParents():
            children.setdefault(int(p.Tag), set()).add(tag)
        for c in f.GetChildren():
            children.setdefault(tag, set()).add(int(c.Tag))
    return children


def descendants(children, roots) -> list:
    seen, stack, out = set(roots), list(roots), []
    while stack:
        for tag in sorted(children.get(stack.pop(), ())):
            if tag not in seen:
                seen.add(tag)
                stack.append(tag)
                out.append(tag)
    return out


def reorder_conflicts(order, parents, children, moved, anchor, position) -> list:
    """``[(tag, "parent"|"child")]`` that forbid moving ``moved`` before/after ``anchor``."""
    i, j = order.index(moved), order.index(anchor)
    if i < j:
        passed = order[i + 1 : j + (1 if position == "after" else 0)]
        return [(k, "child") for k in passed if k in children.get(moved, set())]
    passed = order[j + (1 if position == "after" else 0) : i]
    return [(k, "parent") for k in passed if k in parents.get(moved, set())]


def expected_order(order, moved, anchor, position) -> list:
    rest = [k for k in order if k != moved]
    at = rest.index(anchor) + (1 if position == "after" else 0)
    return rest[:at] + [moved] + rest[at:]


def _feature(tx, ref, field="/feature"):
    """A feature of the work part (an internal sketch datum system is refused)."""
    f = tx.resolve(ref, "feature", work_part=True)
    if _internal(f):
        fail(
            "NX_INVALID_ARGUMENT",
            "Internal features (a sketch's datum system) follow their owner; use the owner",
            field=field,
        )
    return f


def update(tx):
    """Run the NX model update under the call's mark; errors are ``NX_UPDATE_FAILED``."""
    with native(default="NX_UPDATE_FAILED"):
        count = tx.session.UpdateManager.DoUpdate(tx.mark)
    if count:
        fail("NX_UPDATE_FAILED", f"The model update reported {count} error(s)")


def feature_messages(f, kind="Error") -> list:
    query = getattr(f, f"GetFeature{kind}Messages", None)
    try:
        return [str(m) for m in query()] if query is not None else []
    except Exception:
        return []


def solve_driven_sketch(tx, expressions):
    """Solve the active sketch when an edited expression drives one of its dimensions."""
    sk = tx.session.ActiveSketch
    if not sk or not expressions:
        return
    tags = ksketch._expression_closure(expressions)
    nx = tx.nx
    driven = any(
        getattr(c, "AssociatedExpression", None) is not None
        and int(c.AssociatedExpression.Tag) in tags
        for c in sk.GetAllConstraintsOfType(
            nx.Sketch.ConstraintClass.Dimension, nx.Sketch.ConstraintType.NoCon
        )
    )
    if driven:
        ksketch.solve(tx, sk, expressions)


# ---------------------------------------------------------------------------
# nx_get_feature


class Parameter(TypedDict):
    expression: result.Row
    description: str
    formula: str
    value: float | None
    units: str
    editable: bool


class FeatureInfo(TypedDict):
    feature: result.Row
    type: str
    timestamp: int | None
    suppressed: bool
    active: bool
    parents: list[result.Row]
    children: list[result.Row]
    parameters: list[Parameter]
    errors: list[str]


def _parameter(tx, e) -> Parameter:
    try:
        value = expr.value(tx, e)
    except Exception:
        value = None
    return {
        "expression": tx.ref(e, kind="expression"),
        "description": str(getattr(e, "Description", "") or ""),
        "formula": str(e.RightHandSide),
        "value": value,
        "units": expr.units(e),
        "editable": expr.editable(e),
    }


def info(tx, f) -> FeatureInfo:
    part = tx.part
    state = states(tx, part).get(int(f.Tag), (f, bool(getattr(f, "Suppressed", False)), True))
    children = child_map(list(part.Features)).get(int(f.Tag), set())
    by_tag = {int(x.Tag): x for x in part.Features}
    stamp = getattr(f, "Timestamp", None)
    return {
        "feature": tx.ref(f, kind="feature"),
        "type": str(getattr(f, "FeatureType", "")),
        "timestamp": None if stamp is None else int(stamp),
        "suppressed": state[1],
        "active": state[2],
        "parents": [tx.ref(p, kind="feature") for p in f.GetParents()],
        "children": [
            tx.ref(by_tag[t], kind="feature")
            for t in sorted(children)
            if t in by_tag and not _internal(by_tag[t])
        ],
        "parameters": [_parameter(tx, e) for e in f.GetExpressions()],
        "errors": feature_messages(f),
    }


@tool(
    effect="read",
    label="Get feature",
    summary="Feature {feature}",
    keywords=(
        "feature info",
        "feature parameters",
        "parents children",
        "timestamp",
        "edit parameters",
    ),
    requires=("work_part",),
    cost="instant",
    example={"feature": "f:12"},
)
def nx_get_feature(tx: Tx, *, feature: FeatureRef) -> FeatureInfo:
    """Read a feature: type, timeline position, state, parents and children. Parameters list
    each expression with its formula, value, units and editability."""
    return info(tx, _feature(tx, feature))


# ---------------------------------------------------------------------------
# nx_edit_feature


class ParameterValue(TypedDict):
    name: Annotated[str, Text(100), Doc("Expression name or alias: distance, radius, count...")]
    value: Annotated[
        float | Annotated[str, Text(4096)], Doc("Number in the expression's units, or a formula")
    ]


class TransformDefinition(TypedDict):
    transform: Pose


forbid_extra(ParameterValue, TransformDefinition)


def _parameter_targets(tx, f, parameters):
    owned = list(f.GetExpressions())
    seen, out = set(), []
    for i, item in enumerate(parameters):
        field = f"/parameters/{i}"
        name = item["name"]
        exp = next((e for e in owned if e.Name == name), None)
        if exp is None:
            try:
                exp = expr.alias(f, name)
            except Exception as error:
                error.field = field + "/name"
                raise
        if int(exp.Tag) in seen:
            fail("NX_INVALID_ARGUMENT", f"{name} names an expression twice", field=field)
        seen.add(int(exp.Tag))
        if not expr.editable(exp):
            fail("NX_READ_ONLY", f"Expression {exp.Name} is locked or interpart", field=field)
        out.append((exp, item["value"], field))
    return out


@tool(
    effect="model",
    label="Edit feature",
    summary=lambda a, say: (
        f"Edit {say('feature')}"
        + (f" ({len(a['parameters'])} parameters)" if a.get("parameters") else " definition")
    ),
    keywords=(
        "edit parameters",
        "change dimension",
        "modify feature",
        "bind expression",
        "edit with rollback",
    ),
    requires=("work_part",),
    cost="heavy",
    previewable=True,
    example={"feature": "f:12", "parameters": [{"name": "distance", "value": 25}]},
)
def nx_edit_feature(
    tx: Tx,
    *,
    feature: FeatureRef,
    parameters: Annotated[list[ParameterValue], Items(1, 25)] | None = None,
    definition: TransformDefinition | None = None,
) -> FeatureInfo:
    """Set feature parameters to numbers or formulas, by expression name or alias (distance,
    radius, count). definition replaces a transform."""
    args.exactly_one(parameters=parameters, definition=definition)
    f = _feature(tx, feature)
    if definition is not None:
        kind = str(getattr(f, "FeatureType", "")).upper().replace(" ", "_")
        handler = hooks("definition").get(kind)
        if handler is None:
            fail(
                "NX_UNSUPPORTED_OPTION",
                f"{kind} has no editable definition here; edit its parameters instead",
                field="/definition",
            )
        handler(tx, f, definition)
        update(tx)
    else:
        targets = _parameter_targets(tx, f, parameters)
        for exp, value, field in targets:
            try:
                expr.bind(tx, exp, value)
            except Exception as error:
                if getattr(error, "field", "") is None:
                    error.field = field + "/value"  # type: ignore[attr-defined]  # any exception without a field
                raise
        update(tx)
        for exp, value, field in targets:
            if isinstance(value, (int, float)):
                readback.expect_close(expr.value(tx, exp), float(value), field.strip("/"))
        solve_driven_sketch(tx, [exp for exp, _, _ in targets])
    errors = feature_messages(f)
    if errors:
        fail("NX_UPDATE_FAILED", "The feature failed to update: " + "; ".join(errors))
    return info(tx, f)


# ---------------------------------------------------------------------------
# Suppression, current feature, order


class Row(result.Row, total=False):
    index: int
    suppressed: bool
    requested: bool


class SuppressionData(TypedDict):
    changed: list[Row]
    unchanged: list[Row]
    still_suppressed: list[Row]


def _row(
    tx,
    f,
    *,
    index: int | None = None,
    suppressed: bool | None = None,
    requested: bool | None = None,
) -> Row:
    row: Row = {**tx.ref(f, kind="feature")}
    if index is not None:
        row["index"] = index
    if suppressed is not None:
        row["suppressed"] = suppressed
    if requested is not None:
        row["requested"] = requested
    return row


@tool(
    effect="model",
    label="Set feature suppression",
    summary=lambda a, say: ("Suppress " if a["suppressed"] else "Unsuppress ") + say("features"),
    keywords=("suppress", "unsuppress", "disable feature", "enable feature", "deactivate"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"features": ["f:12"], "suppressed": True},
)
def nx_set_feature_suppression(
    tx: Tx,
    *,
    features: Annotated[list[FeatureRef], Items(1, MAX_FEATURES)],
    suppressed: bool,
) -> SuppressionData:
    """Suppress or unsuppress features. Dependents follow a suppress; unsuppress restores only
    the listed ones (see still_suppressed). Reacquire faces and edges after."""
    args.distinct("/features", features, "features")
    part = tx.part
    targets = [_feature(tx, ref, f"/features/{i}") for i, ref in enumerate(features)]
    if len({int(f.Tag) for f in targets}) != len(targets):
        fail("NX_INVALID_ARGUMENT", "features name the same feature twice", field="/features")
    before = states(tx, part)
    inactive = [f for f in targets if not before.get(int(f.Tag), (f, False, True))[2]]
    if inactive:
        fail(
            "NX_INVALID_ARGUMENT",
            "Features after the current feature are inactive; NX would not honour their "
            "suppression when the model rolls forward",
            field="/features",
            hint="Make the last feature current with nx_set_current_feature, then retry",
            details={"inactive": [_row(tx, f) for f in inactive]},
        )
    pending = [f for f in targets if before[int(f.Tag)][1] != suppressed]
    with native(default="NX_BUILD_FAILED"):
        if pending and suppressed:
            part.Features.SuppressFeatures(pending)
        elif pending:
            failed = list(part.Features.UnsuppressFeatures(pending) or [])
            readback.expect(
                not failed, "unsuppressed", "NX could not unsuppress some features",
                features=[f.JournalIdentifier for f in failed],
            )  # fmt: skip
    if pending:
        update(tx)
    after = states(tx, part)
    wrong = [
        f for f in targets if after.get(int(f.Tag), (f, not suppressed, True))[1] != suppressed
    ]
    readback.expect(
        not wrong, "suppressed", "NX left features in the wrong suppression state",
        features=[f.JournalIdentifier for f in wrong],
    )  # fmt: skip
    asked = {int(f.Tag) for f in targets}
    changed = [
        _row(tx, f, suppressed=s, requested=tag in asked)
        for tag, (f, s, _) in after.items()
        if tag in before and before[tag][1] != s
    ]
    still: list[Row] = []
    if not suppressed:
        children = child_map(list(part.Features))
        for tag in descendants(children, sorted(asked)):
            if tag in after and after[tag][1]:
                still.append(_row(tx, after[tag][0], suppressed=True))
    return {
        "changed": changed,
        "unchanged": [
            _row(tx, f, suppressed=suppressed)
            for f in targets
            if int(f.Tag) not in {int(p.Tag) for p in pending}
        ],
        "still_suppressed": still,
    }


class CurrentData(TypedDict):
    current: Row
    inactive: list[Row]


@tool(
    effect="model",
    label="Set current feature",
    summary=lambda a, say: f"Make {say('feature')} current" if a.get("feature") else "Roll to end",
    keywords=("make current", "rollback timeline", "roll back", "insert point", "timeline"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"feature": "f:12"},
)
def nx_set_current_feature(
    tx: Tx,
    *,
    feature: FeatureRef | None = None,
) -> CurrentData:
    """Roll the timeline to a feature, or to the end when feature is omitted. Later features
    become inactive and new features are inserted after the current one."""
    part = tx.part
    order = timeline(part)
    if not order:
        fail("NX_NOT_FOUND", "The work part has no features")
    chosen = _feature(tx, feature) if feature is not None else order[-1]
    with native(default="NX_BUILD_FAILED"):
        chosen.MakeCurrentFeature()
    update(tx)
    after = states(tx, part)
    current = getattr(part, "CurrentFeature", None)
    if current is None:
        active = [f for f, _, a in after.values() if a]
        current = active[-1] if active else None
    readback.expect(
        current is not None and int(current.Tag) == int(chosen.Tag),
        "current",
        "NX made a different feature current",
        current=getattr(current, "JournalIdentifier", None),
    )
    keys = list(after)
    return {
        "current": _row(
            tx, chosen, index=keys.index(int(chosen.Tag)), suppressed=after[int(chosen.Tag)][1]
        ),
        "inactive": [
            _row(tx, f, index=keys.index(t), suppressed=s)
            for t, (f, s, a) in after.items()
            if not a
        ],
    }


class OrderData(TypedDict):
    feature: Row
    anchor: Row
    moved: bool


@tool(
    effect="model",
    label="Reorder feature",
    summary="Move {feature} {position} {anchor}",
    keywords=("reorder", "move feature", "timeline order", "feature order", "drag in history"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"feature": "f:12", "anchor": "f:9", "position": "before"},
)
def nx_set_feature_order(
    tx: Tx,
    *,
    feature: FeatureRef,
    anchor: FeatureRef,
    position: Literal["before", "after"],
) -> OrderData:
    """Move a feature directly before or after another. A feature cannot move before a parent
    or after a child (NX_DEPENDENCY_ORDER lists them); NX renumbers feature names."""
    if feature == anchor:
        fail("NX_INVALID_ARGUMENT", "feature and anchor must differ", field="/anchor")
    part = tx.part
    moved, target = _feature(tx, feature), _feature(tx, anchor, "/anchor")
    key, at = int(moved.Tag), int(target.Tag)
    if key == at:
        fail("NX_INVALID_ARGUMENT", "feature and anchor must differ", field="/anchor")
    before = states(tx, part)
    order = list(before)
    parents = {key: {int(p.Tag) for p in moved.GetParents()}}
    children = {key: child_map(list(part.Features)).get(key, set())}
    conflicts = reorder_conflicts(order, parents, children, key, at, position)
    if conflicts:
        fail(
            "NX_DEPENDENCY_ORDER",
            f"Moving {moved.Name} {position} {target.Name} would put it before a parent or "
            "after a child",
            field="/anchor",
            details={
                "conflicts": [{**_row(tx, before[k][0]), "relation": r} for k, r in conflicts]
            },
        )
    wanted = expected_order(order, key, at, position)
    if wanted != order:
        kinds = tx.nx.Features.FeatureCollection.ReorderType
        # NX 2406 refuses some moves natively (670529 "being referenced").
        with native(default="NX_DEPENDENCY_ORDER"):
            part.Features.ReorderFeature(
                [moved], target, kinds.Before if position == "before" else kinds.After
            )
        update(tx)
        after = list(states(tx, part))
        readback.expect(after == wanted, "order", "NX timeline order differs from the request")
    return {
        "feature": _row(tx, moved, index=wanted.index(key)),
        "anchor": _row(tx, target, index=wanted.index(at)),
        "moved": wanted != order,
    }


# ---------------------------------------------------------------------------
# Expressions and update


class ExpressionData(TypedDict):
    expression: result.Row
    formula: str
    value: float | None
    units: str
    created: bool


@tool(
    effect="model",
    label="Set expression",
    summary="Set {name} = {formula}",
    keywords=("expression", "parameter", "variable", "formula", "design table", "p0"),
    requires=("work_part",),
    cost="heavy",
    previewable=True,
    example={"name": "wall", "formula": "2.5", "units": "mm"},
)
def nx_set_expression(
    tx: Tx,
    *,
    name: ExpressionName,
    formula: Formula,
    units: Literal["mm", "in", "deg", "rad", "unitless"] | None = None,
) -> ExpressionData:
    """Create a named Number expression or change its formula. units apply to a new expression
    only; the model updates and an active sketch it drives is solved."""
    existing = expr.find(tx, name)
    try:
        exp = _set(tx, existing, name, formula, units)
    except NXError as error:
        if error.code == "NX_INVALID_FORMULA" and error.field is None:
            error.field = "/formula"
        raise
    update(tx)
    if existing is not None:
        solve_driven_sketch(tx, [exp])
    return {
        "expression": tx.ref(exp, kind="expression"),
        "formula": str(exp.RightHandSide),
        "value": expr.value(tx, exp),
        "units": expr.units(exp),
        "created": existing is None,
    }


def _set(tx, existing, name, formula, units):
    if existing is None:
        exp = expr.create(tx, name, formula, units or "unitless")
    else:
        if units is not None and expr.units(existing) not in (units, _NX_UNITS.get(units)):
            fail(
                "NX_INVALID_ARGUMENT",
                f"{name} exists in {expr.units(existing)}; its units cannot change",
                field="/units",
            )
        if getattr(existing, "Type", "Number") != "Number":
            fail("NX_READ_ONLY", f"{name} is not a Number expression", field="/name")
        exp = expr.set_formula(tx, existing, formula)
    return exp


# NX unit abbreviations of the units a caller may name.
_NX_UNITS = {"mm": "MilliMeter", "in": "Inch", "deg": "Degrees", "rad": "Radian", "unitless": None}


class HealthData(TypedDict):
    features: int
    errors: list[dict]
    warnings: list[dict]
    suppressed: int
    bodies_checked: int


@tool(
    effect="model",
    label="Update model",
    summary="Update the model",
    keywords=("rebuild", "regenerate", "update model", "model health", "recompute"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    example={},
)
def nx_update_model(tx: Tx) -> HealthData:
    """Run the NX model update and report feature errors, warnings and body consistency. Update
    errors roll the model back."""
    part = tx.part
    update(tx)
    errors: list[dict] = []
    warnings: list[dict] = []
    suppressed = 0
    for f in part.Features:
        tx.checkpoint()
        for kind, out in (("Error", errors), ("Warning", warnings)):
            for message in feature_messages(f, kind):
                out.append({"feature": f.JournalIdentifier, "message": message})
        suppressed += bool(getattr(f, "Suppressed", False))
    bodies = list(part.Bodies)
    check = getattr(getattr(tx.uf, "Modeling", None), "AskBodyConsistency", None)
    if check is not None:
        for body in bodies:
            count = check(body.Tag)[0]
            if count:
                errors.append(
                    {"body": body.JournalIdentifier, "message": f"{count} consistency faults"}
                )
    return {
        "features": len(list(part.Features)),
        "errors": errors,
        "warnings": warnings,
        "suppressed": suppressed,
        "bodies_checked": len(bodies) if check is not None else 0,
    }
