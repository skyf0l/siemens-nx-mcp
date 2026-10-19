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
- ``ReorderFeature`` of a feature before the current feature of a rolled-back timeline
  makes the feature before the new position current (the moved feature and the old
  current one turn inactive): the move puts the old current feature (or, when the
  timeline was at its end, the last feature) back as current.
- A model update leaves an active sketch unsolved after a driving expression
  changed; ``kit.sketch.solve`` (``LocalUpdate``) solves it and checks the
  driven dimensions.
- Bad formulas are native errors: ``NX_INVALID_FORMULA`` keeps ``nx_code``.
- An NX formula has no unit words ("10 mm" is syntax error 3270008): a number with a
  unit (``'10 mm'``, ``'0.5 in'``, ``'30 deg'``) is converted here to a bare number in
  the expression's own units (``as_number``).
"""

from __future__ import annotations

import contextlib
import math
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import hooks, restorer, tool
from nx_mcp.core.errors import NXError, counted, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import MM_PER_IN, Doc, FeatureRef, Items, Pose, Text, forbid_extra
from nx_mcp.kit import args, builders, deform, expr, readback, result
from nx_mcp.kit import features as kfeatures
from nx_mcp.kit import sketch as ksketch

ExpressionName = Annotated[
    str, Text(100, r"^[A-Za-z_][A-Za-z0-9_]{0,99}$"), Doc("Expression name (case-sensitive)")
]
Formula = Annotated[
    float | Annotated[str, Text(4096)], Doc("NX formula ('p3*2', '25'), a number, or '10 mm'")
]
MAX_FEATURES = 100
MAX_DRIVEN = 50
# NX 2406 refuses some new values only at the update ("Update undo happened", 670024) and
# names no parameter: an inner diameter at or above the outer, a negative size.
UPDATE_HINT = (
    "NX rejected the new values for this feature's geometry (e.g. a tube's inner diameter at "
    "or above its outer, a size at or below zero); compare them with the other parameters "
    "nx_get_feature lists"
)
FORMULA_HINT = (
    "Send a number, a number with units ('10 mm', '0.5 in', '30 deg'), another "
    "expression's name, or NX syntax such as 'p3*2'"
)


# ---------------------------------------------------------------------------
# Numbers with units

# "<number> <unit>": a quantity where NX wants a bare number (an NX formula has no units).
_QUANTITY = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(mm|in|deg|rad)\s*$", re.IGNORECASE
)
# unit -> (dimension, size in the dimension's base unit: mm, deg)
_SCALE = {
    "mm": ("length", 1.0),
    "in": ("length", MM_PER_IN),
    "deg": ("angle", 1.0),
    "rad": ("angle", 180.0 / math.pi),
}


def unit_key(exp) -> str:
    """``mm``, ``in``, ``deg``, ``rad``, ``unitless`` or NX's abbreviation of other units."""
    unit = getattr(exp, "Units", None)
    if unit is None:
        return "unitless"
    names = {v: k for k, v in expr.UNIT_NAMES.items() if v}
    for text in (getattr(unit, "Abbreviation", None), getattr(unit, "Name", None)):
        if text in _SCALE:
            return str(text)
        if text in names:
            return names[text]
    return expr.units(exp)


def quantity(value) -> tuple[float, str] | None:
    """``(number, unit)`` of a ``'10 mm'`` string, else None."""
    match = _QUANTITY.match(value) if isinstance(value, str) else None
    if match is None or not math.isfinite(float(match.group(1))):
        return None
    return float(match.group(1)), match.group(2).lower()


def convert(number: float, unit: str, to: str) -> float | None:
    """``number`` ``unit`` in ``to`` units; None when they measure different things."""
    a, b = _SCALE.get(unit), _SCALE.get(to)
    if a is None or b is None or a[0] != b[0]:
        return None
    return number if unit == to else number * a[1] / b[1]


def number_text(value: float) -> str:
    """A number as NX formula text (``25`` rather than ``25.0``), without the float noise of
    a unit conversion (``3 in`` is ``76.2``, not ``76.19999999999999``)."""
    text = repr(float(f"{float(value):.15g}"))
    return text[:-2] if text.endswith(".0") else text


def as_number(value, exp, field: str):
    """``value`` for ``exp``: ``'20 mm'`` becomes a number in the expression's units;
    numbers and formulas pass unchanged."""
    q = quantity(value)
    if q is None:
        return value
    own = unit_key(exp)
    converted = convert(*q, own)
    if converted is None:
        what = "is unitless" if own == "unitless" else f"is in {own}"
        fail(
            "NX_INVALID_ARGUMENT",
            f"{exp.Name} {what}; {value.strip()!r} does not fit it",
            field=field,
            hint=f"Give a plain number in {own}" if own != "unitless" else "Give a plain number",
        )
    return converted


# ---------------------------------------------------------------------------
# Timeline state


# kit.features: the timeline and feature states are shared with the inspection domain.
_internal = kfeatures.internal
timeline = kfeatures.timeline
states = kfeatures.states


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


def display_name(f) -> str:
    """The name a feature row shows (its own name, else NX's journal identifier)."""
    return str(getattr(f, "Name", "") or getattr(f, "JournalIdentifier", "") or "")


def owner_of(part, f):
    """The user-visible feature an internal one belongs to, else None: NX names an internal
    feature after its owner (``SKETCH(1:1B)`` of ``SKETCH(1)``) and gives it the owner's
    timestamp."""
    jid = str(getattr(f, "JournalIdentifier", "") or "")
    owned = jid.split(":", 1)[0] + ")" if ":" in jid else None
    stamp = getattr(f, "Timestamp", None)
    visible = [o for o in timeline(part) if int(o.Tag) != int(f.Tag)]
    match = [o for o in visible if owned and getattr(o, "JournalIdentifier", None) == owned]
    if not match and stamp is not None:
        match = [o for o in visible if getattr(o, "Timestamp", None) == stamp]
    return match[0] if match else None


def _feature(tx, ref, field="/feature"):
    """A feature of the work part; an internal one (a hole's or a sketch's own sub-feature)
    is refused with its owner's ref."""
    try:
        f = tx.resolve(ref, "feature", work_part=True)
    except NXError as error:
        error.field = error.field or field
        raise
    if _internal(f):
        owner = owner_of(tx.part, f)
        if owner is None:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{display_name(f)} is an internal sub-feature of another feature (such as a "
                "hole's or a sketch's own); it follows its owner",
                field=field,
                hint="Use the owning feature; nx_list_objects kind feature lists only those",
            )
        row = tx.ref(owner, kind="feature")
        fail(
            "NX_INVALID_ARGUMENT",
            f"{display_name(f)} is an internal sub-feature of {row['name']} and follows it",
            field=field,
            hint=f"Use the owner {row['id']} ({row['name']}) instead",
            details={"owner": row},
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
    aliases: list[str]


class _FeatureInfo(TypedDict):
    feature: result.Row
    type: str
    timestamp: int | None
    suppressed: bool
    active: bool
    parents: list[result.Row]
    children: list[result.Row]
    parameters: list[Parameter]
    errors: list[str]


class FeatureInfo(_FeatureInfo, total=False):
    edge_count: Annotated[int, Doc("Blend or chamfer: the edges it acts on")]
    edges: list[result.Row]


# Feature types whose edit builder lists the edges they act on.
EDGE_FEATURES = {"BLEND": "CreateEdgeBlendBuilder", "CHAMFER": "CreateChamferBuilder"}


def acted_edges(tx, f) -> list | None:
    """The edges a blend or chamfer acts on (read from its edit builder, which is destroyed
    uncommitted), or None for other features. Blended edges are consumed: their refs
    name the edges as the feature selected them."""
    factory = EDGE_FEATURES.get(str(getattr(f, "FeatureType", "")).upper())
    if factory is None:
        return None
    edges: list = []
    with builders.build(tx, getattr(tx.part.Features, factory), f) as b:
        if factory == "CreateChamferBuilder":
            edges = list(b.SmartCollector.GetObjects())
        else:
            for i in range(int(b.GetNumberOfValidChainsets())):
                collector, _radius = b.GetChainset(i)
                edges += list(collector.GetObjects())
    return edges


# A word of an expression description, unless a feature name follows it ("Block(1)").
_DESCRIPTION_WORD = re.compile(r"\b([A-Za-z]{3,})\b(?!\()")


def _description_words(f) -> list[str]:
    """Candidate aliases from the feature's own expression descriptions ("Block(1) Length
    (XC)" -> length): every word that is not the feature's name or type."""
    kind = str(getattr(f, "FeatureType", "") or "").casefold()
    own = set(re.split(r"[^a-z]+", kind)) | set(
        re.split(r"[^a-z]+", str(getattr(f, "JournalIdentifier", "") or "").casefold())
    )
    words: list[str] = []
    for e in f.GetExpressions():
        for word in _DESCRIPTION_WORD.findall(str(getattr(e, "Description", "") or "")):
            word = word.casefold()
            if word not in own and word not in words:
                words.append(word)
    return words


def aliases(f) -> dict:
    """``{alias: expression tag}``: the names ``nx_edit_feature`` resolves on this feature
    (semantic names such as ``distance`` and the words of the expression descriptions,
    such as a block's ``length``), each kept when the resolver finds exactly one expression,
    so p-numbered expressions read clearly. A locked parameter keeps its aliases (editable
    says whether nx_edit_feature can set it)."""
    out = {}
    for name in (*expr.alias_names(), *_description_words(f)):
        if name in out:
            continue
        try:
            found = expr.alias(f, name)
        except NXError:
            continue
        out[name] = int(found.Tag)
    return out


def editable(f, e) -> bool:
    """Whether nx_edit_feature can set ``e`` of ``f`` (a threaded hole's tap drill: yes,
    through a custom radial engagement)."""
    return kfeatures.parameter_lock(f, e) in (None, "engagement")


def _parameter(tx, f, e, named=None) -> Parameter:
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
        "editable": editable(f, e),
        "aliases": [a for a, tag in (named or {}).items() if tag == int(e.Tag)],
    }


def info(tx, f) -> FeatureInfo:
    part = tx.part
    state = states(tx, part).get(int(f.Tag), (f, bool(getattr(f, "Suppressed", False)), True))
    children = child_map(list(part.Features)).get(int(f.Tag), set())
    by_tag = {int(x.Tag): x for x in part.Features}
    stamp = getattr(f, "Timestamp", None)
    expressions = list(f.GetExpressions())
    named = aliases(f) if expressions else {}
    out: FeatureInfo = {
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
        "parameters": [_parameter(tx, f, e, named) for e in expressions],
        "errors": feature_messages(f),
    }
    edges = acted_edges(tx, f)
    if edges is not None:
        out["edge_count"] = len(edges)
        out["edges"] = [tx.ref(e, kind="edge") for e in edges]
    return out


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
    cost="light",
    example={"feature": "f:12"},
)
def nx_get_feature(tx: Tx, *, feature: FeatureRef) -> FeatureInfo:
    """Read a feature: type, timeline position, state, parents and children. Parameters give
    formula, value, units and nx_edit_feature aliases; a blend or chamfer its edges. feature: a ref like f:12 (from a create
    result or nx_list_objects)."""
    return info(tx, _feature(tx, feature))


# ---------------------------------------------------------------------------
# nx_edit_feature


class ParameterValue(TypedDict):
    name: Annotated[
        str,
        Text(100),
        Doc("Expression name (p12) or alias: distance, start, end, radius, diameter, depth, angle, "
            "count, spacing, outer_diameter"),
    ]  # fmt: skip
    value: Annotated[
        float | Annotated[str, Text(4096)], Doc("Number in expression units, '20 mm' or formula")
    ]


class TransformDefinition(TypedDict):
    transform: Pose


forbid_extra(ParameterValue, TransformDefinition)


def _unknown(f, owned, error: NXError) -> None:
    """Say what the feature does take when a parameter name matches nothing."""
    if error.code != "NX_NOT_FOUND":
        return
    if not owned:
        error.message = f"{f.JournalIdentifier} has no parameters to edit"
        error.hint = (
            "A sketch's dimensions are expressions: nx_get_sketch lists them and "
            "nx_set_expression changes them"
            if "SKETCH" in str(getattr(f, "FeatureType", "")).upper()
            else "Edit the feature's parents or its definition instead"
        )
        return
    by_tag = {int(e.Tag): e.Name for e in owned}
    named = {a: by_tag[t] for a, t in aliases(f).items() if t in by_tag}
    error.details = {**(error.details or {}), "aliases": named}
    error.hint = (
        "Use an expression name from details.expressions or an alias from details.aliases "
        "(nx_get_feature lists both)"
    )


def _parameter_targets(tx, f, parameters):
    owned = list(f.GetExpressions())
    seen, out = set(), []
    for i, item in enumerate(parameters):
        field = f"/parameters/{i}"
        name = item["name"]
        exp = next((e for e in owned if e.Name == name), None)
        folded = [e for e in owned if e.Name.casefold() == name.strip().casefold()]
        if exp is None and len(folded) == 1:  # 'P1' for p1: the feature's own names
            exp = folded[0]
        if exp is None:
            try:
                exp = expr.alias(f, name.strip().casefold())
            except NXError as error:
                error.field = field + "/name"
                _unknown(f, owned, error)
                raise
        if int(exp.Tag) in seen:
            fail("NX_INVALID_ARGUMENT", f"{name} names an expression twice", field=field)
        seen.add(int(exp.Tag))
        lock = kfeatures.parameter_lock(f, exp)
        if lock not in (None, "engagement"):
            _locked(f, exp, lock, field)
        value = _string_value(tx, exp, as_number(item["value"], exp, field + "/value"))
        number = literal(value)
        if number is not None and not math.isfinite(number):
            fail("NX_INVALID_FORMULA", f"{str(value).strip()!r} is not a finite number",
                 field=field + "/value", hint=FORMULA_HINT)  # fmt: skip
        out.append((exp, value, field))
    return out


LOCK_HINTS = {
    "thread_table": "The thread table sets it: change the thread size instead (name 'size', "
    "value 'M8 x 1.25'; nx_list_thread_sizes lists them)",
    "interpart": "It links from another part: edit it there",
    "locked": "NX derives it from the feature's other parameters; edit those",
}


def _locked(f, exp, lock: str, field: str) -> None:
    """Refuse a parameter nx_edit_feature cannot set, saying why (details.reason)."""
    what = kfeatures.hole_parameter(exp) or str(getattr(exp, "Description", "") or "")
    fail(
        "NX_READ_ONLY",
        f"Expression {exp.Name} ({what}) is "
        + ("set by the thread table" if lock == "thread_table" else f"{lock}")
        + f" in {display_name(f)}",
        field=field,
        hint=LOCK_HINTS[lock],
        details={"name": str(exp.Name), "reason": lock, "description": what},
    )


def _string_value(tx, exp, value):
    """A String parameter (a hole's thread size) takes plain text: quoted for NX unless it
    is already quoted or names an expression."""
    if getattr(exp, "Type", "Number") != "String" or not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith('"') or expr.find(tx, text) is not None:
        return value
    return '"' + text.replace('"', '\\"') + '"'


def _free_tap_drill(tx, f, targets) -> list:
    """Set a threaded hole's radial engagement to "Custom" when the call edits its tap drill
    or tip angle (the thread table drives them otherwise, and NX locks them after the
    first update); ``[(engagement, old formula)]`` when it did."""
    if not any(kfeatures.parameter_lock(f, exp) == "engagement" for exp, _, _ in targets):
        return []
    engage = kfeatures.engagement(f)
    if engage is None or any(int(exp.Tag) == int(engage.Tag) for exp, _, _ in targets):
        return []
    old = str(engage.RightHandSide)
    expr.bind(tx, engage, kfeatures.CUSTOM_ENGAGEMENT)
    update(tx)
    tx.warn("NX_NOTE", f"{display_name(f)}: radial engagement {old} -> Custom, so the thread "
            "table no longer sets the tap drill and the tip angle")  # fmt: skip
    return [(engage, old)]


def _table_tap_drill(tx, f, targets) -> list:
    """A new thread size of a threaded hole at a "Custom" radial engagement: set the
    engagement back to the table's (0.75) so the tap drill follows the new size (NX 2406
    fails the update when the old custom tap drill is below the new size's minor diameter),
    unless the call sets the tap drill, tip angle or engagement itself; ``[(expression, old
    formula)]`` of the engagement and the tap drill when it did."""
    if not any(kfeatures.hole_parameter(exp) == "thread size" for exp, _, _ in targets):
        return []
    engage = kfeatures.engagement(f)
    if engage is None or str(engage.RightHandSide).strip() != kfeatures.CUSTOM_ENGAGEMENT:
        return []
    sent = {kfeatures.hole_parameter(exp) for exp, _, _ in targets}
    if sent & {*kfeatures.ENGAGEMENT_DRIVEN, "radial engage"}:
        return []
    taps = [e for e in f.GetExpressions() if kfeatures.hole_parameter(e) in
            kfeatures.ENGAGEMENT_DRIVEN]  # fmt: skip
    old = [(engage, str(engage.RightHandSide)), *((e, str(e.RightHandSide)) for e in taps)]
    expr.bind(tx, engage, TABLE_ENGAGEMENT)
    tx.warn("NX_NOTE", f"{display_name(f)}: radial engagement Custom -> {TABLE_ENGAGEMENT}, so "
            "the thread table sets the tap drill and the tip angle of the new size (send tap "
            "with size to keep a custom tap drill)")  # fmt: skip
    return old


TABLE_ENGAGEMENT = '"0.75"'


# A plain number written as text ('25', '-2.5', '1e3'): NX reads it as that number.
_LITERAL = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$")


def literal(value) -> float | None:
    """The number ``value`` gives: a number, or text that is only a number (``'1e400'`` is
    infinite); None for a formula."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _LITERAL.match(value):
        return float(value)
    return None


def _non_positive(error: NXError, targets) -> None:
    """Point an update failure at the length set at or below zero, when there is one (NX
    names no parameter: "Update undo happened")."""
    for exp, value, field in targets:
        number = literal(value)
        if number is not None and number <= 0 and unit_key(exp) in ("mm", "in"):
            error.field = error.field or field + "/value"
            error.hint = (
                f"{exp.Name} is a length: NX refused {number:g}; give a length above zero "
                "unless the feature takes a signed offset (a start limit)"
            )
            return


def _read_back_values(tx, targets) -> None:
    """Each edited expression evaluates to a finite value, and a number reads back as sent;
    NX keeps an invalid formula's text with the old value ('1e400'): rolled back as
    ``NX_INVALID_FORMULA``."""
    for exp, value, field in targets:
        try:
            got = expr.value(tx, exp)
        except Exception:
            got = None
        number = literal(value)
        if isinstance(got, float) and not math.isfinite(got):
            fail("NX_INVALID_FORMULA", f"{exp.Name} = {exp.RightHandSide} does not evaluate to "
                 "a finite number", field=field + "/value", hint=FORMULA_HINT)  # fmt: skip
        if number is not None and got is not None:
            readback.expect_close(got, number, field.strip("/"))
        elif isinstance(value, str):
            _fresh(tx, exp, field + "/value")


def _fresh(tx, exp, field: str) -> None:
    """Refuse a formula NX stored without evaluating it (the old value stays)."""
    fresh = expr.stale(tx, exp)
    if fresh is not None:
        fail("NX_INVALID_FORMULA", f"NX kept the old value of {exp.Name}: its formula "
             f"{str(exp.RightHandSide)!r} does not evaluate (a number beyond a double?)",
             field=field, hint=FORMULA_HINT, details={"evaluates_to": fresh})  # fmt: skip


def _same_formula(now: str, wanted: str) -> bool:
    """The same formula text, or the same plain number ('5' and '5.0')."""
    a, b = literal(now.split("//", 1)[0]), literal(wanted.split("//", 1)[0])
    return now.strip() == wanted.strip() or (a is not None and a == b)


def _restore_steps(owner, found) -> tuple[list, list, list]:
    """``(values, engagement, driven)``: the formulas to set back in the updates NX takes
    for a threaded hole: its size and other values first (the table tap drill then fits
    the size), then its radial engagement, then the tap drill and tip angle the restored
    engagement frees (one the table still locks is left to the table)."""

    def what(e):
        return kfeatures.hole_parameter(e) if owner is not None else None

    engage = [(e, b) for e, b in found if what(e) == "radial engage"]
    driven = [(e, b) for e, b in found if engage and what(e) in kfeatures.ENGAGEMENT_DRIVEN]
    held = {int(e.Tag) for e, _ in (*engage, *driven)}
    return [(e, b) for e, b in found if int(e.Tag) not in held], engage, driven


@restorer("nx_edit_feature")
def _restore_feature(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set back the formulas an ``nx_edit_feature`` replaced (the compensating undo);
    refused when one of them changed since."""
    rows = list(data.get("parameters") or [])
    found = []
    for row in rows:
        exp = expr.find(tx, str(row["name"]))
        if exp is None:
            fail("NX_NOT_FOUND", f"Expression {row['name']} no longer exists",
                 details={"name": row["name"]})  # fmt: skip
        now = str(exp.RightHandSide)
        if now.strip() != str(row["after"]).strip():
            fail("NX_DEPENDENCY_ORDER", f"{row['name']} changed since: it reads {now!r}, the "
                 f"operation set {row['after']!r}", details={"name": row["name"], "formula": now})  # fmt: skip
        found.append((exp, str(row["before"])))
    if apply:
        owner = None
        with contextlib.suppress(NXError):
            owner = tx.resolve(data["feature"], "feature") if data.get("feature") else None
        values, engage, driven = _restore_steps(owner, found)
        for phase in (values, engage):
            for exp, before in phase:
                expr.bind(tx, exp, before)
            if phase:
                update(tx)
        free = [(e, b) for e, b in driven if kfeatures.parameter_lock(owner, e) is None]
        for exp, before in free:
            expr.bind(tx, exp, before)
        if free or not found:
            update(tx)
        solve_driven_sketch(tx, [exp for exp, _ in found])
        wrong = [
            str(e.Name)
            for e, b in found
            if not _same_formula(str(e.RightHandSide), b)
            # NX derives a hole's tap drill again from its restored engagement
            and not (owner is not None and kfeatures.parameter_lock(owner, e) == "engagement")
        ]
        readback.expect(not wrong, "formula", "Formulas not set back: " + ", ".join(wrong))
    return f"{data.get('feature')}: " + ", ".join(
        f"{r['name']} back to {r['before']}" for r in rows
    )


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
        "resize feature",
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
    radius, count). nx_get_feature lists both for each parameter; p-names (p12) number the
    whole part, not the feature. A tube's diameters are outer_diameter and inner_diameter (NX
    describes them as Limit 1 and Limit 2). definition replaces a transform's
    (nx_create_transform) pose instead."""
    args.exactly_one(parameters=parameters, definition=definition)
    with tx.checks():
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
        with tx.checks():
            targets = _parameter_targets(tx, f, parameters)
        olds = [str(exp.RightHandSide) for exp, _, _ in targets]
        freed = _free_tap_drill(tx, f, targets) or _table_tap_drill(tx, f, targets)
        for exp, value, field in targets:
            try:
                expr.bind(tx, exp, value)
            except Exception as error:
                if getattr(error, "field", "") is None:
                    error.field = field + "/value"  # type: ignore[attr-defined]  # any exception without a field
                if isinstance(error, NXError) and error.code == "NX_INVALID_FORMULA":
                    error.hint = FORMULA_HINT  # a generic code hint does not fit a formula
                raise
        try:
            update(tx)
        except NXError as error:
            if error.code == "NX_UPDATE_FAILED":
                _non_positive(error, targets)
                error.hint = error.hint or UPDATE_HINT
            raise
        _read_back_values(tx, targets)
        solve_driven_sketch(tx, [exp for exp, _, _ in targets])
        for exp, _, _ in targets:
            tx.modified(exp, "expression")
        tx.note_restore({
            "feature": tx.ref(f, kind="feature")["id"],
            "parameters": [
                {"name": str(exp.Name), "before": old, "after": str(exp.RightHandSide)}
                for exp, old in [*((e, o) for (e, _, _), o in zip(targets, olds, strict=True)),
                                 *freed]
            ],
        })  # fmt: skip
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
    """Suppress (suppressed: true) or unsuppress (false) features given as refs like f:12
    from nx_list_objects. Dependents follow a suppress; unsuppress restores the listed ones and
    the suppressed parents they need, not their dependents (still_suppressed). Reacquire faces
    and edges after."""
    args.distinct("/features", features, "features")
    part = tx.part
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        targets = [_feature(tx, ref, f"/features/{i}") for i, ref in enumerate(features)]
        if len({int(f.Tag) for f in targets}) != len(targets):
            fail("NX_INVALID_ARGUMENT", "features name the same feature twice",
                 field="/features")  # fmt: skip
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
        parents = [r["name"] for r in changed if not r.get("requested")]
        if parents:  # NX unsuppresses the suppressed parents a feature needs
            tx.warn("NX_NOTE", "NX also unsuppressed the parents the features need: "
                    + ", ".join(parents[:10]) + " (changed, requested false)")  # fmt: skip
    base = base_csys(before)
    if suppressed and base in asked:
        tx.warn("NX_NOTE", "The base datum CSYS is suppressed: features placed on it are "
                "suppressed with it (changed)")  # fmt: skip
    if changed:
        tx.note_restore({
            "suppressed": [r["id"] for r in changed if r.get("suppressed")],
            "unsuppressed": [r["id"] for r in changed if not r.get("suppressed")],
        })  # fmt: skip
    return {
        "changed": changed,
        "unchanged": [
            _row(tx, f, suppressed=suppressed)
            for f in targets
            if int(f.Tag) not in {int(p.Tag) for p in pending}
        ],
        "still_suppressed": still,
    }


@restorer("nx_set_feature_suppression")
def _restore_suppression(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set back the suppression state an ``nx_set_feature_suppression`` changed (the
    compensating undo): unsuppress what it suppressed, suppress what it unsuppressed;
    refused when one of them changed state since."""
    part = tx.part
    sup, unsup = _tags(tx, data.get("suppressed") or []), _tags(tx, data.get("unsuppressed") or [])
    state = states(tx, part)
    moved = [t for t in sup if t in state and not state[t][1]]
    moved += [t for t in unsup if t in state and state[t][1]]
    if moved:
        fail("NX_DEPENDENCY_ORDER", "The suppression of "
             + ", ".join(display_name(state[t][0]) for t in moved[:5]) + " changed since",
             details={"features": [_row(tx, state[t][0]) for t in moved[:20]]})  # fmt: skip
    if apply:
        with native(default="NX_BUILD_FAILED"):
            if sup:
                failed = list(part.Features.UnsuppressFeatures([state[t][0] for t in sup]) or [])
                readback.expect(not failed, "unsuppressed", "NX could not unsuppress some "
                                "features", features=[f.JournalIdentifier for f in failed])  # fmt: skip
            if unsup:
                part.Features.SuppressFeatures([state[t][0] for t in unsup])
        update(tx)
        now = states(tx, part)
        wrong = [t for t in sup if now.get(t, (None, True))[1]]
        wrong += [t for t in unsup if not now.get(t, (None, False))[1]]
        readback.expect(not wrong, "suppressed", "NX left features in the wrong suppression "
                        "state", features=[display_name(now[t][0]) for t in wrong if t in now])  # fmt: skip
    parts = [f"{len(sup)} unsuppressed"] if sup else []
    parts += [f"{len(unsup)} suppressed"] if unsup else []
    return "feature suppression set back (" + ", ".join(parts) + ")"


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
    """Roll the timeline to a feature (a ref like f:12 from nx_list_objects), or to the end
    when feature is omitted. Later features become inactive (not suppressed) and new features
    are inserted after the current one. Their bodies go until rolled forward, then come back
    under new refs."""
    with tx.checks():
        part = tx.part
        order = timeline(part)
        if not order:
            fail("NX_NOT_FOUND", "The work part has no features")
        chosen = _feature(tx, feature) if feature is not None else order[-1]
        bodies = {int(b.Tag) for b in part.Bodies}
        was = getattr(part, "CurrentFeature", None)
        was_end = was is None or int(was.Tag) == int(order[-1].Tag)
    with native(default="NX_BUILD_FAILED"):
        chosen.MakeCurrentFeature()
    update(tx)
    after = states(tx, part)
    gone = len(bodies - {int(b.Tag) for b in part.Bodies})
    if gone:  # NX removes the bodies of inactive features: listed as deleted, not destroyed
        tx.warn(
            "NX_NOTE",
            f"NX removed {counted(gone, 'body')} of the now inactive features until the timeline "
            "rolls forward (changes.deleted); rolling forward rebuilds them under new refs",
        )
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
    tx.note_restore({
        "before": None if was_end else tx.ref(was, kind="feature")["id"],
        "after": tx.ref(chosen, kind="feature")["id"],
    })  # fmt: skip
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


def _current_tag(part) -> int | None:
    current = getattr(part, "CurrentFeature", None)
    return None if current is None else int(current.Tag)


@restorer("nx_set_current_feature")
def _restore_current(tx: Tx, data: dict, *, apply: bool) -> str:
    """Make the feature that was current before an ``nx_set_current_feature`` current again
    (the timeline's end when it was there): the compensating undo. Refused when another
    feature was made current since."""
    part = tx.part
    after = _tags(tx, [data["after"]])[0]
    if _current_tag(part) not in (after, None):
        fail("NX_DEPENDENCY_ORDER", f"Another feature was made current since {data['after']}",
             details={"feature": data["after"]})  # fmt: skip
    if apply:
        order = timeline(part)
        tag = _tags(tx, [data["before"]])[0] if data.get("before") else int(order[-1].Tag)
        target = next(f for f in order if int(f.Tag) == tag)
        with native(default="NX_BUILD_FAILED"):
            target.MakeCurrentFeature()
        update(tx)
        readback.expect(_current_tag(part) in (tag, None), "current",
                        "NX made a different feature current")  # fmt: skip
    return "timeline rolled " + (
        f"back to {data['before']}" if data.get("before") else "to its end"
    )


class Renamed(result.Row):
    was: str


class _OrderState(TypedDict, total=False):
    current: Annotated[Row, Doc("The current feature after the move (the timeline's end)")]
    activated: Annotated[list[Row], Doc("Features the move made active (now before current)")]
    deactivated: Annotated[list[Row], Doc("Features the move made inactive (now after current)")]


class OrderData(_OrderState):
    feature: Row
    anchor: Row
    moved: bool
    renamed: Annotated[list[Renamed], Doc("Features NX renumbered (was: the old name)")]


def _placement(anchor, position, before, after) -> tuple[str, str]:
    """``(anchor ref, position)`` from anchor + position, or from before / after alone."""
    if before is not None or after is not None:
        if anchor is not None or position is not None or (before and after):
            fail(
                "NX_INVALID_ARGUMENT",
                "Give one placement: before, after, or anchor with position",
                field="/after" if after is not None else "/before",
                hint="e.g. {feature: 'f:12', after: 'f:9'} or "
                "{feature: 'f:12', anchor: 'f:9', position: 'after'}",
            )
        return (before, "before") if before is not None else (after, "after")
    if anchor is None or position is None:
        missing = "anchor" if anchor is None else "position"
        fail(
            "NX_INVALID_ARGUMENT",
            f"{missing} is required: give anchor (a feature ref) with position 'before' or "
            "'after', or before/after alone",
            field="/" + missing,
            hint="e.g. {feature: 'f:12', after: 'f:9'}",
        )
    return anchor, position


@tool(
    effect="model",
    label="Reorder feature",
    summary=lambda a, say: (
        f"Move {say('feature')} "
        + next(
            (
                f"{p} {say(p)}" if p != "anchor" else f"{a.get('position')} {say('anchor')}"
                for p in ("before", "after", "anchor")
                if a.get(p) is not None
            ),
            "",
        )
    ).strip(),
    keywords=(
        "reorder",
        "move feature",
        "timeline order",
        "feature order",
        "drag in history",
        "move hole before blend",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"feature": "f:12", "after": "f:9"},
)
def nx_set_feature_order(
    tx: Tx,
    *,
    feature: FeatureRef,
    anchor: FeatureRef | None = None,
    position: Literal["before", "after"] | None = None,
    before: FeatureRef | None = None,
    after: FeatureRef | None = None,
) -> OrderData:
    """Move a feature directly before or after another: before or after alone, or anchor with
    position. Refs like f:12. A feature cannot move before a parent or after a child
    (NX_DEPENDENCY_ORDER lists them); the base datum CSYS stays first. NX renumbers names
    (see renamed). In a rolled-back
    timeline, a move across the current feature changes which features are active
    (activated, deactivated)."""
    ref, where = _placement(anchor, position, before, after)
    field = "/" + ("anchor" if before is None and after is None else where)
    if feature == ref:
        fail("NX_INVALID_ARGUMENT", "feature and anchor must differ", field=field)
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        part = tx.part
        moved, target = _feature(tx, feature), _feature(tx, ref, field)
        key, at = int(moved.Tag), int(target.Tag)
        if key == at:
            fail("NX_INVALID_ARGUMENT", "feature and anchor must differ", field=field)
        old = states(tx, part)
        order = list(old)
        _keep_base_first(tx, old, key, at, where, field)
        was = getattr(part, "CurrentFeature", None)
        # The timeline's end stays its end; a rolled-back one keeps its current feature.
        keep = None if was is None or int(was.Tag) == order[-1] else int(was.Tag)
        names = {tag: display_name(f) for tag, (f, _, _) in old.items()}
        parents = {key: {int(p.Tag) for p in moved.GetParents()}}
        children = {key: child_map(list(part.Features)).get(key, set())}
        conflicts = reorder_conflicts(order, parents, children, key, at, where)
        if conflicts:
            relation = "after its child" if conflicts[0][1] == "child" else "before its parent"
            fail(
                "NX_DEPENDENCY_ORDER",
                f"Moving {names[key]} {where} {names[at]} would put it {relation} "
                + ", ".join(names[k] for k, _ in conflicts),
                field=field,
                hint="Pick an anchor that keeps the feature after its parents and before its "
                "children (details.conflicts)",
                details={
                    "conflicts": [{**_row(tx, old[k][0]), "relation": r} for k, r in conflicts]
                },
            )
    wanted = expected_order(order, key, at, where)
    new = old
    if wanted != order:
        kinds = tx.nx.Features.FeatureCollection.ReorderType
        # NX 2406 refuses some moves natively (670529 "being referenced").
        with native(default="NX_DEPENDENCY_ORDER"):
            part.Features.ReorderFeature(
                [moved], target, kinds.Before if where == "before" else kinds.After
            )
        update(tx)
        _keep_current(tx, part, keep if keep is not None else wanted[-1])
        new = states(tx, part)
        readback.expect(list(new) == wanted, "order", "NX timeline order differs from the request")
    # Rows from the refreshed timeline: NX renumbers names after a move.
    fresh = {tag: f for tag, (f, _, _) in new.items()}
    if wanted != order:
        ids = {tag: tx.ref(f, kind="feature")["id"] for tag, f in fresh.items()}
        tx.note_restore({
            "feature": ids[key],
            "before": [ids[t] for t in order if t in ids],
            "after": [ids[t] for t in wanted if t in ids],
            "current": ids.get(int(was.Tag)) if was is not None else None,
        })  # fmt: skip
    renamed: list[Renamed] = []
    for tag, f in fresh.items():
        if tag in names and display_name(f) != names[tag] and len(renamed) < MAX_FEATURES:
            renamed.append({**tx.ref(f, kind="feature"), "was": names[tag]})
    return {
        "feature": _row(tx, fresh.get(key, moved), index=wanted.index(key)),
        "anchor": _row(tx, fresh.get(at, target), index=wanted.index(at)),
        "moved": wanted != order,
        "renamed": renamed,
        **_activity(tx, part, old, new),
    }


def base_csys(old) -> int | None:
    """Tag of the part's base datum CSYS (the timeline's first feature when it is a
    DATUM_CSYS), else None."""
    first = next(iter(old.values()), None)
    if first is None:
        return None
    kind = str(getattr(first[0], "FeatureType", "") or "").upper().replace(" ", "_")
    return int(first[0].Tag) if kind == "DATUM_CSYS" else None


def _keep_base_first(tx, old, key: int, at: int, where: str, field: str) -> None:
    """Refuse a move that would take the base datum CSYS off the timeline's start: NX
    would number the moved feature 0 (BLOCK(0)) and the CSYS after it."""
    base = base_csys(old)
    if base is None or not (key == base or (at == base and where == "before")):
        return
    row = _row(tx, old[base][0])
    fail(
        "NX_DEPENDENCY_ORDER",
        f"{row['name']} is the part's base datum CSYS and stays the first feature",
        field="/feature" if key == base else field,
        hint=f"Place the feature after {row['id']} or after a later feature",
        details={"base": row},
    )


def _keep_current(tx, part, tag: int) -> None:
    """Make the feature ``tag`` current again when the move made another one current (NX
    rolls back to the feature before a move across the current feature)."""
    now = getattr(part, "CurrentFeature", None)
    if now is None or int(now.Tag) == tag:
        return
    feature = next((f for f in timeline(part) if int(f.Tag) == tag), None)
    if feature is None:
        return
    with native(default="NX_BUILD_FAILED"):
        feature.MakeCurrentFeature()
    update(tx)


def _activity(tx, part, old, new) -> dict:
    """The current feature and the features a move turned active or inactive (a move across
    the current feature of a rolled-back timeline), with a warning when there are any."""
    flips = [(tag, f, a) for tag, (f, _, a) in new.items() if tag in old and old[tag][2] != a]
    out: dict = {}
    current = getattr(part, "CurrentFeature", None)
    if current is not None:
        out["current"] = _row(tx, current)
    if flips:
        out["activated"] = [_row(tx, f) for _, f, a in flips if a][:MAX_FEATURES]
        out["deactivated"] = [_row(tx, f) for _, f, a in flips if not a][:MAX_FEATURES]
        names = ", ".join(display_name(f) + (" active" if a else " inactive") for _, f, a in flips)
        tx.warn("NX_NOTE", f"The move crossed the current feature: now {names[:300]}")
    return out


def _tags(tx, refs) -> list[int]:
    """Tags of feature ``refs``; a feature deleted since refuses the compensating undo."""
    out = []
    for ref in refs:
        try:
            out.append(int(tx.resolve(ref, "feature").Tag))
        except NXError:
            fail("NX_DEPENDENCY_ORDER", f"Feature {ref} no longer exists: the timeline "
                 "changed since", details={"feature": ref})  # fmt: skip
    return out


@restorer("nx_set_feature_order")
def _restore_order(tx: Tx, data: dict, *, apply: bool) -> str:
    """Move the feature an ``nx_set_feature_order`` moved back next to its old neighbour
    (the compensating undo); refused when the timeline order changed since."""
    part = tx.part
    before, after = _tags(tx, data["before"]), _tags(tx, data["after"])
    key = _tags(tx, [data["feature"]])[0]

    def relative() -> list[int]:
        recorded = set(before)
        return [int(f.Tag) for f in timeline(part) if int(f.Tag) in recorded]

    if relative() != after:
        fail("NX_DEPENDENCY_ORDER", f"The timeline order changed since the move of "
             f"{data['feature']}", details={"feature": data["feature"]})  # fmt: skip
    if apply:
        i = before.index(key)
        by_tag = {int(f.Tag): f for f in timeline(part)}
        anchor, how = (before[i + 1], "Before") if i + 1 < len(before) else (before[i - 1], "After")
        kinds = tx.nx.Features.FeatureCollection.ReorderType
        with native(default="NX_DEPENDENCY_ORDER"):
            part.Features.ReorderFeature([by_tag[key]], by_tag[anchor], getattr(kinds, how))
        update(tx)
        current = _tags(tx, [data["current"]])[0] if data.get("current") else None
        if current is None or current == before[-1]:
            current = int(timeline(part)[-1].Tag)  # the timeline's end stays its end
        _keep_current(tx, part, current)
        readback.expect(relative() == before, "order", "NX timeline order differs from the one "
                        "before the move")  # fmt: skip
    return f"feature {data['feature']} moved back to its old place in the timeline"


# ---------------------------------------------------------------------------
# Expressions and update


class ExpressionData(TypedDict):
    expression: result.Row
    formula: str
    value: float | None
    units: str
    created: bool
    features: list[result.Row]


def driven_features(tx, expressions) -> list[result.Row]:
    """Features whose parameters use ``expressions``, directly or through other ones."""
    tags = ksketch._expression_closure(expressions)
    out = []
    for f in timeline(tx.part):
        query = getattr(f, "GetExpressions", None)
        if query is not None and any(int(getattr(e, "Tag", -1)) in tags for e in query()):
            out.append(tx.ref(f, kind="feature"))
            if len(out) == MAX_DRIVEN:
                break
    return out


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
    """Create a named Number expression or change its formula ('10 mm' converts to the
    expression's units). units apply to a new one only. Lists the features it drives."""
    with tx.checks():
        existing = expr.find(tx, name)
        if existing is not None and not expr.editable(existing):
            _read_only(tx, existing)
        number, unit = _formula_value(existing, formula, units)
        plain = literal(formula)
        if plain is not None and not math.isfinite(plain):
            fail("NX_INVALID_FORMULA", f"{str(formula).strip()!r} is not a finite number",
                 field="/formula", hint=FORMULA_HINT)  # fmt: skip
    text = formula if number is None else number_text(number)
    before = str(existing.RightHandSide) if existing is not None else None
    try:
        exp = _set(tx, existing, name, text, unit)
    except NXError as error:
        if error.code == "NX_INVALID_FORMULA":
            error.field = error.field or "/formula"
            error.hint = FORMULA_HINT  # _name_missing may name the unknown words
            _name_missing(tx, error, name, text)
        raise
    if existing is None and unit in (None, "unitless") and units is None:
        tx.warn(
            "NX_NOTE",
            f"{name} was created unitless: pass units 'mm' (or a formula like '10 mm') for a "
            "length, 'deg' for an angle",
        )
    update(tx)
    if existing is not None:
        solve_driven_sketch(tx, [exp])
    value = expr.value(tx, exp)
    if number is not None:
        readback.expect_close(value, number, "value")
    if number is None and plain is None:
        _fresh(tx, exp, "/formula")
    if isinstance(value, float) and not math.isfinite(value):
        fail("NX_INVALID_FORMULA", f"{name} = {exp.RightHandSide} does not evaluate to a "
             "finite number", field="/formula", hint=FORMULA_HINT)  # fmt: skip
    detached = sorted(_names_in(tx, before) - _names_in(tx, str(exp.RightHandSide)) - {name})
    if detached:
        tx.warn("NX_NOTE", f"{name} no longer follows {', '.join(detached[:5])} (its formula was "
                f"{before!r})")  # fmt: skip
    if existing is not None:
        tx.modified(exp, "expression")
    tx.note_restore({"name": str(exp.Name), "before": before, "after": str(exp.RightHandSide)})
    return {
        "expression": tx.ref(exp, kind="expression"),
        "formula": str(exp.RightHandSide),
        "value": value,
        "units": expr.units(exp),
        "created": existing is None,
        "features": driven_features(tx, [exp]),
    }


@restorer("nx_set_expression")
def _restore_expression(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set back the formula an ``nx_set_expression`` replaced, or delete the expression it
    created (the compensating undo); refused when the formula changed since, or when a
    created expression is in use."""
    name, before, after = str(data["name"]), data.get("before"), str(data["after"])
    exp = expr.find(tx, name)
    if exp is None:
        fail("NX_NOT_FOUND", f"Expression {name} no longer exists", details={"name": name})
    now = str(exp.RightHandSide)
    if now.strip() != after.strip():
        fail(
            "NX_DEPENDENCY_ORDER",
            f"{name} changed since: it reads {now!r}, the operation set {after!r}",
            details={"name": name, "formula": now},
        )
    if before is None:
        users = [str(e.Name) for e in expr.dependents(exp)] + [
            str(r.get("name")) for r in driven_features(tx, [exp])
        ]
        if users:
            fail(
                "NX_DEPENDENCY_ORDER",
                f"{name}, which the operation created, is used by {', '.join(users[:5])}",
                details={"name": name, "used_by": users[:20]},
            )
        if apply:
            with native(default="NX_DEPENDENCY_ORDER"):
                tx.part.Expressions.Delete(exp)
            readback.expect(expr.find(tx, name) is None, "deleted", f"{name} is still present")
        return f"expression {name} deleted (the operation created it)"
    if apply:
        expr.set_formula(tx, exp, str(before))
        update(tx)
        solve_driven_sketch(tx, [exp])
        got = str(exp.RightHandSide)
        readback.expect(got.strip() == str(before).strip(), "formula",
                        f"{name} reads {got!r}, not {before!r}", formula=got)  # fmt: skip
    return f"{name} back to {before}"


# NX formula words that are not expressions: functions and constants.
FORMULA_WORDS = frozenset((
    "abs", "acos", "asin", "atan", "atan2", "ceiling", "cos", "cosh", "deg", "exp", "fact",
    "floor", "hypot", "if", "else", "then", "ln", "log", "log10", "max", "min", "mod", "pi",
    "rad", "round", "sgn", "sin", "sinh", "sqrt", "tan", "tanh", "true", "false", "and", "or",
    "not", "xor", "arccos", "arcsin", "arctan",
))  # fmt: skip
_IDENTIFIER = re.compile(r"(?<![\w.])[A-Za-z_]\w*")


def _name_missing(tx, error, name, formula) -> None:
    """Name the identifiers of ``formula`` the part has no expression for (NX says only
    "The specified expression variable does not exist")."""
    if not isinstance(formula, str) or (
        error.nx_code != expr.NO_SUCH_EXPRESSION and "does not exist" not in error.message
    ):
        return
    words = _IDENTIFIER.findall(re.sub(r'"[^"]*"', "", formula))
    missing = sorted(
        {w for w in words if w != name and w.lower() not in FORMULA_WORDS}
        - {str(e.Name) for e in tx.part.Expressions}
    )
    if missing:
        error.message = f"{error.message} Unknown: {', '.join(missing[:10])}"
        error.details = {**(error.details or {}), "missing": missing[:20]}
        error.hint = (
            f"Create {missing[0]} first with nx_set_expression, or fix the name "
            "(names are case-sensitive)"
        )


def _names_in(tx, formula) -> set[str]:
    """The part's expression names a formula refers to (none for None)."""
    if not isinstance(formula, str):
        return set()
    words = set(_IDENTIFIER.findall(re.sub(r'"[^"]*"', "", formula.split("//", 1)[0])))
    words = {w for w in words if w.lower() not in FORMULA_WORDS}
    return words & {str(e.Name) for e in tx.part.Expressions} if words else set()


def _read_only(tx, exp) -> None:
    """Refuse an expression the agent cannot edit, saying why and what drives it."""
    name = str(exp.Name)
    users = driven_features(tx, [exp])[:5]
    if getattr(exp, "IsInterpartExpression", False):
        reason, hint = "interpart", "Edit it in the part it links from"
    else:
        reason = "locked"
        hint = (
            f"It is a value NX derives (e.g. from a thread or hole size): edit "
            f"{users[0]['name']} with nx_edit_feature instead"
            if users
            else "Ask the engineer to unlock it"
        )
    fail(
        "NX_READ_ONLY",
        f"Expression {name} is {reason}: NX does not let it be edited",
        field="/name",
        hint=hint,
        details={"name": name, "reason": reason, "features": users},
    )


def _formula_value(existing, formula, units) -> tuple[float | None, str | None]:
    """``(number or None, units)``: a number, or ``'10 mm'`` converted to the existing
    expression's units (a new expression takes the string's unit unless units is given)."""
    if isinstance(formula, (int, float)):
        return float(formula), units
    q = quantity(formula)
    if q is None:
        return None, units
    if existing is not None:
        return float(as_number(formula, existing, "/formula")), units
    target = units or q[1]
    converted = convert(*q, target)
    if converted is None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{formula.strip()!r} does not fit units={target}",
            field="/formula",
            hint="Omit units to take the formula's unit, or give a plain number",
        )
    return converted, target


def _set(tx, existing, name, formula, units):
    if existing is None:
        exp = expr.create(tx, name, formula, units or "unitless")
    else:
        if units is not None and expr.units(existing) not in (units, _NX_UNITS.get(units)):
            fail(
                "NX_INVALID_ARGUMENT",
                f"{name} exists in {expr.units(existing)}; its units cannot change",
                field="/units",
                hint="Omit units for an existing expression ('0.5 in' still converts)",
            )
        if getattr(existing, "Type", "Number") != "Number":
            fail("NX_READ_ONLY", f"{name} is not a Number expression", field="/name")
        exp = expr.set_formula(tx, existing, formula)
    return exp


# NX unit abbreviations of the units a caller may name.
_NX_UNITS = {"mm": "MilliMeter", "in": "Inch", "deg": "Degrees", "rad": "Radian", "unitless": None}


class HealthData(TypedDict):
    features: Annotated[
        int, Doc("Every feature of the part, NX's internal ones (a sketch's or a hole's) included")
    ]
    errors: Annotated[list[dict], Doc("{feature or body: ref row, message}")]
    warnings: Annotated[list[dict], Doc("{feature: ref row, message}")]
    suppressed: Annotated[int, Doc("Features suppressed by the user")]
    inactive: Annotated[
        int, Doc("Features after the current feature (rolled back), the suppressed not counted")
    ]
    bodies_checked: int


@tool(
    effect="model",
    label="Update model",
    summary="Update the model",
    keywords=(
        "rebuild",
        "regenerate",
        "update model",
        "model health",
        "recompute",
        "rebuild after expression change",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    example={},
)
def nx_update_model(tx: Tx) -> HealthData:
    """Run the NX model update and report feature errors, warnings and body consistency. Takes
    no arguments. Counts user-suppressed and inactive (rolled back) features apart. Update
    errors roll the model back."""
    part = tx.part
    update(tx)
    with contextlib.suppress(NXError):  # an assembly NX cannot read: no such warning
        for message in deform.range_messages(tx, part):  # a formula may drive one out of range
            tx.warn("NX_UPDATE_WARNING", message)
    errors: list[dict] = []
    warnings: list[dict] = []
    for f in part.Features:
        tx.checkpoint()
        for kind, out in (("Error", errors), ("Warning", warnings)):
            for message in feature_messages(f, kind):
                out.append({"feature": tx.ref(f, kind="feature"), "message": message})
    state = states(tx, part).values()
    suppressed = sum(s for _, s, _ in state)
    inactive = sum(not a and not s for _, s, a in state)  # apart: a suppressed one is not
    bodies = list(part.Bodies)
    check = getattr(getattr(tx.uf, "Modeling", None), "AskBodyConsistency", None)
    if check is not None:
        for body in bodies:
            count = check(body.Tag)[0]
            if count:
                errors.append(
                    {"body": tx.ref(body, kind="body"), "message": f"{count} consistency faults"}
                )
    return {
        "features": len(list(part.Features)),
        "errors": errors,
        "warnings": warnings,
        "suppressed": suppressed,
        "inactive": inactive,
        "bodies_checked": len(bodies) if check is not None else 0,
    }
