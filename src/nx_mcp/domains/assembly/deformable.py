"""Deformable parts (springs, hoses, gas struts): define which expressions of a part vary,
then give each component of it its own values in an assembly.

NX 2406 findings encoded here (probed live):

- ``FeatureCollection.CreateDeformDefinitionBuilder`` creates the ``FLEXIBLE_PART``
  feature from the member features (``SetMemberFeatures``) and one
  ``JaDeformDefinitionExpData`` per varying expression (``SetExpressionsData``): ``Type``
  and ``ScaleType`` take ints (Exp 0, Range 1; Real 1), ``LowEnd``/``HighEnd`` strings.
  A part that is already deformable refuses a second definition; ``SetDeleteExisting
  Definition(True)`` commits a feature that 2406 never lists and that cannot be
  converted, so a redefinition is refused before NX acts.
- NX 2406 deforms through feature templates: ``UserDefinedTemplates.Convert
  DeformToFeaturetemplate(feature)`` turns the definition into a template ``Definition``
  (the ``FLEXIBLE_PART`` is recreated); without it the assembly step fails with "authoring
  part does not have one template entity".
- Deform a component: ``DeformManager.PreProcessDefinitionPart(c, part, False)``,
  ``CreateDeformBuilder(assembly, c).PreCreateNewDeform()``, an ``InstantiationBuilder``
  (``CreateInstantiationBuilder(None)``, ``SetBuilderAndDefinitionPart``, then
  ``Component``), ``GetMatchedExpression`` per template expression (a new assembly
  expression, ``extension`` or ``extension_1``...), ``Commit``, ``PostCreateNewDeform``.
  NX copies the member features into the assembly part (bodies there that follow the
  component's pose) plus a ``FLEXED_PART`` feature; the component's own bodies no longer
  show. The part file keeps its own values.
- Edit: ``PreProcessDefinitionPart(c, part, True)``, ``PreUpdateDeform``, an
  ``InstantiationBuilder`` of the existing ``Instantiation`` whose expressions are the
  assembly ones (in the definition's order), ``Commit``, ``PostUpdateDeform(True)``, then
  a model update. Outside a builder the assembly expressions are locked.
- A deformed value may be a formula of assembly expressions: the copy then follows them
  after a model update (a strut length driven by a hatch parameter).
- NX does not enforce a definition's min/max through the API, and 2406 cannot read them
  back, so ``nx_create_deformable_definition`` records them in a string attribute of the
  ``FLEXIBLE_PART`` and ``nx_set_component_deformation`` checks values against it.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Name, Point3, Text, forbid_extra
from nx_mcp.domains.assembly import common
from nx_mcp.domains.assembly.common import ComponentId, ComponentRow
from nx_mcp.kit import builders, deform, expr, features, licenses, measure, model, readback, result

FLEXIBLE, FLEXED = deform.FLEXIBLE, deform.FLEXED
RANGE_ATTRIBUTE = deform.RANGE_ATTRIBUTE
MAX_VARYING = 20
EXP, RANGE, REAL = 0, 1, 1  # DeformDefinitionBuilder ExpressionType / RangeType values
# NX formula errors on a deformed value ("The specified string contains a syntax error").
FORMULA_CODES = frozenset({3270008, 1050017})
_UNIT_TEXT = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*(mm|cm|m|in|deg|rad)\s*$", re.IGNORECASE
)
UNITS = {
    "mm": ("length", 1.0), "cm": ("length", 10.0), "m": ("length", 1000.0),
    "in": ("length", expr.UNIT_SCALE["Inch"][1]), "deg": ("angle", 1.0),
    "rad": ("angle", expr.UNIT_SCALE["Radian"][1]),
}  # fmt: skip
_EXPRESSION_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,99}$"
ExpressionName = Annotated[str, Text(100, _EXPRESSION_NAME)]


class _VaryingRequired(TypedDict):
    expression: Annotated[ExpressionName, Doc("Work-part expression that varies, e.g. extension")]


class Varying(_VaryingRequired, total=False):
    min: Annotated[float, Doc("Lowest allowed value in the expression's units; give with max")]
    max: Annotated[float, Doc("Highest allowed value; omit min and max for a free value")]


class DeformValue(TypedDict):
    expression: Annotated[ExpressionName, Doc("Varying expression name in the deformable part")]
    value: Annotated[
        float | Annotated[str, Text(4096)],
        Doc("Number in the expression's units, or a formula of assembly expressions"),
    ]


forbid_extra(Varying, DeformValue)


class VaryingRow(TypedDict):
    expression: result.Row
    value: float | None
    units: str
    min: float | None
    max: float | None


class DefinitionData(TypedDict):
    feature: result.Row
    expressions: list[VaryingRow]
    member_features: int


class ValueRow(TypedDict):
    expression: str
    assembly_expression: result.Row
    formula: str
    value: float | None
    units: str
    part_value: float | None


class Bounds(TypedDict):
    low: Point3
    high: Point3


class DeformData(TypedDict):
    component: ComponentRow
    feature: result.Row
    created: bool
    values: list[ValueRow]
    bodies: list[result.Row]
    bounds: Bounds | None


# ---------------------------------------------------------------------------
# Shared lookups


_definition = deform.definition
_ranges = deform.ranges
_deformation = deform.deformation


def _instantiation(assembly, flexed):
    """The template instantiation that owns ``flexed``."""
    for item in assembly.UserDefinedTemplates:
        if any(int(getattr(o, "Tag", 0)) == int(flexed.Tag) for o in item.GetObjects()):
            return item
    fail("NX_NOT_FOUND", "NX lists no template instantiation for the deformation")
    return None


_bodies = deform.flexed_bodies


# ---------------------------------------------------------------------------
# Define a deformable part


@tool(
    effect="model",
    label="Define deformable part",
    summary=lambda a, say: f"Make the part deformable by {say('expressions')}",
    keywords=(
        "deformable part",
        "define deformable",
        "flexible part",
        "spring",
        "hose",
        "gas strut",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    example={"expressions": [{"expression": "extension", "min": 50, "max": 150}]},
)
def nx_create_deformable_definition(
    tx: Tx,
    *,
    expressions: Annotated[list[Varying], Items(1, MAX_VARYING)],
    name: Annotated[Name, Doc("Definition feature name; default DEFORMABLE")] = "DEFORMABLE",
) -> DefinitionData:
    """Make the work part deformable (spring, hose, strut): its listed expressions may vary
    per component in assemblies. All its features are members; min and
    max bound a value (the current value must lie within). One definition per part: save
    the part, then deform components with nx_set_component_deformation."""
    names = [item["expression"] for item in expressions]
    for i, item in enumerate(expressions):
        if names.index(item["expression"]) != i:
            fail("NX_INVALID_ARGUMENT", f"{item['expression']} is listed twice",
                 field=f"/expressions/{i}")  # fmt: skip
        if ("min" in item) != ("max" in item):
            fail("NX_INVALID_ARGUMENT", "Give min and max together, or neither",
                 field=f"/expressions/{i}")  # fmt: skip
        if "min" in item and not float(item["min"]) < float(item["max"]):
            fail("NX_INVALID_ARGUMENT", "min must be below max", field=f"/expressions/{i}/min")
    with tx.checks():
        part = tx.part
        existing = _definition(part)
        if existing is not None:
            fail("NX_ALREADY_EXISTS", "The work part is already deformable",
                 details={"feature": tx.ref(existing, kind="feature"),
                          "expressions": sorted(str(e.Name) for e in existing.GetExpressions()),
                          "ranges": _ranges(tx, existing)},
                 hint="Deform its components with nx_set_component_deformation "
                 "(details.ranges: the recorded min/max); NX 2406 cannot edit a definition")  # fmt: skip
        members = features.timeline(part)
        if not members:
            fail("NX_INVALID_ARGUMENT", "The work part has no features to deform")
        available = {str(e.Name): e for e in part.Expressions}
        chosen = []
        for i, item in enumerate(expressions):
            exp = available.get(item["expression"])
            field = f"/expressions/{i}/expression"
            if exp is None:
                fail("NX_NOT_FOUND", f"The work part has no expression {item['expression']!r}",
                     field=field, details={"available": sorted(available)[:30]})  # fmt: skip
            assert exp is not None
            current = expr.value_or_none(tx, exp)
            if current is None or not expr.editable(exp):
                fail("NX_INVALID_ARGUMENT", f"{exp.Name} is not an editable number expression",
                     field=field)  # fmt: skip
            if "min" in item and not float(item["min"]) <= current <= float(item["max"]):
                fail("NX_INVALID_ARGUMENT",
                     f"{exp.Name} = {current:g} lies outside [{item['min']:g}, {item['max']:g}]",
                     field=f"/expressions/{i}", hint="Widen the range or edit the value first")  # fmt: skip
            chosen.append(exp)
        licenses.require(tx, ("solid_modeling",), "Defining a deformable part needs a "
                         "solid modeling license")  # fmt: skip
    data_type = tx.nx.Features.DeformDefinitionBuilder.JaDeformDefinitionExpData
    with builders.build(tx, part.Features.CreateDeformDefinitionBuilder) as b:
        b.SetName(name)
        b.SetMemberFeatures(members)
        for item, exp in zip(expressions, chosen, strict=True):
            data = data_type()
            data.Type = RANGE if "min" in item else EXP
            data.Exp = exp
            data.ExpName = str(exp.Name)
            data.LowEnd = repr(float(item["min"])) if "min" in item else ""
            data.HighEnd = repr(float(item["max"])) if "max" in item else ""
            data.ScaleType = REAL
            b.SetExpressionsData(data, [])
        made = b.commit()
    readback.expect(made is not None, "feature", "NX created no deformable definition")
    with native(default="NX_BUILD_FAILED"):
        part.UserDefinedTemplates.ConvertDeformToFeaturetemplate(made)
    feature = _definition(part)  # the conversion recreates the feature
    readback.expect(feature is not None, "feature", "The deformable definition is missing")
    assert feature is not None
    readback.expect(len(list(part.UserDefinedTemplates)) > 0, "template",
                    "NX made no feature template of the definition")  # fmt: skip
    got = sorted(str(e.Name) for e in feature.GetExpressions())
    readback.expect(got == sorted(names), "expressions",
                    "The definition varies other expressions", actual=got)  # fmt: skip
    ranges = {
        i["expression"]: [float(i["min"]), float(i["max"])] for i in expressions if "min" in i
    }
    if ranges:
        with native(default="NX_BUILD_FAILED"):
            feature.SetAttribute(RANGE_ATTRIBUTE, json.dumps(ranges, sort_keys=True))
    rows: list[VaryingRow] = []
    for exp in chosen:
        low, high = ranges.get(str(exp.Name), (None, None))
        rows.append(
            {
                "expression": tx.ref(exp, kind="expression"),
                "value": expr.value_or_none(tx, exp),
                "units": expr.units(exp),
                "min": low,
                "max": high,
            }
        )
    return {
        "feature": tx.ref(feature, kind="feature"),
        "expressions": rows,
        "member_features": len(members),
    }


# ---------------------------------------------------------------------------
# Deform a component


def _check_range(name, value, low, high, field) -> None:
    tol = 1e-9 * max(1.0, abs(low), abs(high))
    if not low - tol <= value <= high + tol:
        fail("NX_INVALID_ARGUMENT", f"{name} = {value:g} lies outside [{low:g}, {high:g}]",
             field=field, hint="Stay within the deformable definition's min and max")  # fmt: skip


def _unit_value(text, exp, field) -> float | None:
    """A ``'70 mm'``-style value in ``exp``'s own units (None: not a number with a unit, so
    a formula); a length for an angle (or the reverse) is refused."""
    match = _UNIT_TEXT.match(text)
    if match is None:
        return None
    number, unit = float(match.group(1)), match.group(2).lower()
    kind, scale = UNITS[unit]
    own = getattr(getattr(exp, "Units", None), "Name", None)
    own_kind, own_scale = expr.UNIT_SCALE.get(str(own), (None, None))
    if own_kind is None or own_scale is None:
        return number if own is None else None  # unitless: the number; else NX converts
    if own_kind != kind:
        noun = "an angle" if own_kind == "angle" else "a length"
        fail("NX_INVALID_ARGUMENT", f"{exp.Name} is {noun}, not {text!r}", field=field)
    return number * scale / own_scale


def _formula_value(tx, text, exp, field) -> float | None:
    """Value of the formula ``text`` in ``exp``'s own units, evaluated by NX in a scratch
    expression of the assembly before any deformation starts (a failed deform commit leaves
    NX's deform manager mid-update: the next component's deformation then creates nothing).
    A syntax error, an unknown name or another dimension is refused here. None when ``exp``
    is neither a length nor an angle (NX then judges the formula)."""
    own = str(getattr(getattr(exp, "Units", None), "Name", "") or "")
    kind, scale = expr.UNIT_SCALE.get(own, (None, None))
    if kind is None or scale is None:
        return None
    unit = "deg" if kind == "angle" else "mm"
    value = tx.services.evaluate_formula(text, kind, unit, field=field, scratch=True)
    return float(value) / scale


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else repr(float(value))


def _set_values(builder, pairs, wanted) -> None:
    """Set ``wanted`` (definition name -> RHS) on the builder's assembly expressions."""
    for definition_name, exp in pairs:
        if definition_name in wanted:
            exp.RightHandSide = wanted[definition_name]


def _create(tx, assembly, component, prototype, wanted) -> None:
    manager = tx.nx.Assemblies.DeformManager.GetDeformManager(tx.session)
    templates = assembly.UserDefinedTemplates
    with native(default="NX_BUILD_FAILED"):
        manager.PreProcessDefinitionPart(component, prototype, False)
    with builders.build(tx, manager.CreateDeformBuilder, assembly, component) as deform:
        deform.PreCreateNewDeform()
        with builders.build(tx, templates.CreateInstantiationBuilder, None) as b:
            templates.SetBuilderAndDefinitionPart(b.native, prototype)
            b.Component = component
            pairs = []
            for exp in b.GetExpressions():
                matched, _ = b.GetMatchedExpression(exp)
                if matched is None:
                    fail("NX_BUILD_FAILED", f"NX matched no assembly expression to {exp.Name}")
                pairs.append((str(exp.Name), matched))
            _set_values(b, pairs, wanted)
            b.Commit()
        deform.PostCreateNewDeform()


def _edit(tx, assembly, component, prototype, flexed, definition, wanted) -> None:
    manager = tx.nx.Assemblies.DeformManager.GetDeformManager(tx.session)
    instantiation = _instantiation(assembly, flexed)
    order = [str(e.Name) for e in definition.GetExpressions()]
    with native(default="NX_BUILD_FAILED"):
        manager.PreProcessDefinitionPart(component, prototype, True)
    with builders.build(tx, manager.CreateDeformBuilder, assembly, component) as deform:
        deform.PreUpdateDeform()
        templates = assembly.UserDefinedTemplates
        with builders.build(tx, templates.CreateInstantiationBuilder, instantiation) as b:
            current = list(b.GetExpressions())
            readback.expect(len(current) == len(order), "expressions",
                            "The deformation varies another number of expressions")  # fmt: skip
            _set_values(b, list(zip(order, current, strict=True)), wanted)
            b.Commit()
        deform.PostUpdateDeform(True)


@tool(
    effect="model",
    label="Deform component",
    summary=lambda a, say: f"Deform {a.get('component')}",
    keywords=(
        "deform component",
        "deformable",
        "flexible component",
        "spring length",
        "strut extension",
        "hose",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    example={"component": "o:4", "values": [{"expression": "extension", "value": 120}]},
)
def nx_set_component_deformation(
    tx: Tx,
    *,
    component: ComponentId,
    values: Annotated[list[DeformValue], Items(1, MAX_VARYING)],
) -> DeformData:
    """Deform a direct component of a deformable part: set its varying expressions for this
    occurrence only. The part file keeps its values. Creates or edits the deformation. A value
    is a number, '70 mm' or a formula of assembly expressions ('hatch_len - 40', followed on
    update). Values outside min/max are refused; nx_update_assembly warns when a formula
    drives one out later. The deformed shape is assembly bodies (bodies); the component then
    shows none."""
    wanted: dict[str, str] = {}
    for i, item in enumerate(values):
        name = item["expression"]
        if name in wanted:
            fail("NX_INVALID_ARGUMENT", f"{name} is listed twice", field=f"/values/{i}")
        text = _text(item["value"])
        if not text:
            fail("NX_INVALID_ARGUMENT", "value is empty", field=f"/values/{i}/value")
        wanted[name] = text
    numbers: dict[str, float] = {}  # values given as numbers or unit strings
    with tx.checks():
        assembly = tx.part
        c = common.component(tx, component, "/component", unsuppressed=True)
        prototype = getattr(c, "Prototype", None)
        if prototype is None:
            fail("NX_NOT_LOADED", f"The part of {component} is not loaded", field="/component",
                 hint="Load it with nx_open_part or nx_update_assembly")  # fmt: skip
        definition = _definition(prototype)
        if definition is None:
            fail("NX_INVALID_ARGUMENT", f"The part of {component} is not deformable",
                 field="/component", hint="Define it with nx_create_deformable_definition "
                 "(nx_activate_part on the part first)")  # fmt: skip
        own = {str(e.Name): e for e in definition.GetExpressions()}
        ranges = _ranges(tx, definition)
        for i, item in enumerate(values):
            name = item["expression"]
            if name not in own:
                fail("NX_NOT_FOUND", f"{name} is not a varying expression of the part",
                     field=f"/values/{i}/expression", details={"available": sorted(own)})  # fmt: skip
            low, high = ranges.get(name) or (None, None)
            given = item["value"]
            number: float | None = None
            if isinstance(given, str):
                number = _unit_value(given, own[name], f"/values/{i}/value")
                if number is not None:
                    wanted[name] = repr(number)
            else:
                number = float(given)
            if number is not None:
                numbers[name] = float(number)
            else:  # a formula: NX checks and evaluates it now, before the deform builders
                number = _formula_value(tx, wanted[name], own[name], f"/values/{i}/value")
            if low is not None and number is not None:
                _check_range(name, float(number), low, high, f"/values/{i}/value")
        if not list(prototype.UserDefinedTemplates):
            fail("NX_UNSUPPORTED_OPTION", "The part's deformable definition has no feature "
                 "template (made in an older NX)", field="/component",
                 hint="Ask the engineer to redefine the part as deformable in this NX")  # fmt: skip
        existing = _deformation(tx, assembly, c)
        licenses.require(tx, ("assemblies",), "Deforming a component needs an assemblies "
                         "license")  # fmt: skip
    part_values = {name: expr.value_or_none(tx, own[name]) for name in own}
    try:
        with native(default="NX_BUILD_FAILED"):
            if existing is None:
                _create(tx, assembly, c, prototype, wanted)
            else:
                _edit(tx, assembly, c, prototype, existing, definition, wanted)
            model.update(tx)
    except NXError as error:
        if error.nx_code not in FORMULA_CODES:
            raise
        formulas = [i for i, item in enumerate(values) if item["expression"] not in numbers]
        fail(
            "NX_INVALID_FORMULA",
            f"NX refused a value: {error.message}",
            field=f"/values/{formulas[0]}/value" if len(formulas) == 1 else "/values",
            hint="Send a number, '70 mm', or a formula of the assembly's expressions "
            "(nx_list_objects kind 'expression')",
            nx_code=error.nx_code,
        )
    flexed = _deformation(tx, assembly, c)
    readback.expect(flexed is not None, "deformation", "NX created no deformation")
    assert flexed is not None
    order = [str(e.Name) for e in definition.GetExpressions()]
    mine = list(flexed.GetExpressions())
    readback.expect(len(mine) == len(order), "expressions",
                    "The deformation varies another number of expressions")  # fmt: skip
    rows: list[ValueRow] = []
    for i, item in enumerate(values):
        name = item["expression"]
        exp = mine[order.index(name)]
        got = expr.value_or_none(tx, exp)
        if name in numbers:
            readback.expect_close(got, numbers[name], f"value.{name}",
                                  tol=1e-9 * max(1.0, abs(numbers[name])))  # fmt: skip
        low, high = ranges.get(name) or (None, None)
        if low is not None and got is not None:
            _check_range(name, got, low, high, f"/values/{i}/value")
        kept = expr.value_or_none(tx, own[name])
        if part_values[name] is not None and kept is not None:
            readback.expect_close(kept, part_values[name], f"part_value.{name}", tol=1e-9)
        rows.append(
            {
                "expression": name,
                "assembly_expression": tx.ref(exp, kind="expression"),
                "formula": str(exp.RightHandSide),
                "value": got,
                "units": expr.units(exp),
                "part_value": kept,
            }
        )
    bodies = _bodies(flexed)
    box: Bounds | None = None
    if bodies:
        low, high = measure.bounds(tx, bodies)
        box = {"low": low, "high": high}
    return {
        "component": common.component_row(tx, c),
        "feature": tx.ref(flexed, kind="feature"),
        "created": existing is None,
        "values": rows,
        "bodies": [tx.ref(b, kind="body") for b in bodies],
        "bounds": box,
    }
