"""Sheet-metal part defaults: thickness, bend radius, neutral factor, reliefs, bend method.

NX 2406 findings encoded here:

- ``SheetMetalPreferences.GetMaterialNames`` raises a native memory-access fault, so the
  installed material catalogue is never enumerated: a material or tool name is set as
  given and verified by read-back. On a seat without a sheet-metal standards table NX
  drops any material name (it reads back unset) and keeps the parameter entry ``value``;
  the refusal then says so and that the other defaults go in a call without it.
- Tab, contour flange, lofted flange and from-solid features take the default thickness
  when the call gives none (``part_length``).
- Defaults work in Modeling too (before entering Sheet Metal) and on a plain part.
- The defaults are expressions (``Sheet_Metal_Bend_Radius``, ``Sheet_Metal_Neutral_Factor``,
  ``Sheet_Metal_Relief_Width``...). A flange made with the default radius, K or reliefs
  keeps feature expressions whose formula is that name, so a default change re-bends it
  (R2.5 -> R4 checked live): the change lists those features in ``changes.modified`` with
  a note, and ``apply_to_existing=false`` first sets their formulas to their values. A
  bend method change reaches the features that follow the part's neutral factor.
- ``GetBendTable`` returns NX's table name (``BEND_TABLE``) until a file is set, then the
  path given (returned workspace-relative).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import MM_PER_IN, Doc, Length, PathTo, Range, Text
from nx_mcp.kit import builders, expr, geom, model, readback, result
from nx_mcp.kit.sketch import snake

LENGTHS = {
    "thickness": "MaterialThickness",
    "bend_radius": "BendRadius",
    "bend_relief_width": "BendReliefWidth",
    "bend_relief_depth": "BendReliefDepth",
}
NUMBERS = {**LENGTHS, "neutral_factor": "NeutralFactor"}
ENTRY = {"value": "Value", "material_table": "MaterialTable", "tool_id_table": "ToolIdTable"}
METHODS = {
    "neutral_factor_value": "NeutralFactorValue",
    "bend_table": "BendTable",
    "bend_allowance_formula": "BendAllowanceFormula",
    "material_table": "MaterialTable",
    "tool_table": "ToolTable",
    "bend_allowance_table": "BendAllowanceTable",
    "bend_deduction_table": "BendDeductionTable",
    "bend_deduction_formula": "BendDeductionFormula",
    "din6935_formula": "Din6935Formula",
}
ParameterEntry = Literal["value", "material_table", "tool_id_table"]
BendDefinition = Literal[
    "neutral_factor_value",
    "bend_table",
    "bend_allowance_formula",
    "material_table",
    "tool_table",
    "bend_allowance_table",
    "bend_deduction_table",
    "bend_deduction_formula",
    "din6935_formula",
]
Formula = Annotated[str, Text(1024, r"^[^\x00-\x1f]+$")]
TableName = Annotated[str, Text(128)]
FORMULAS = {
    "bend_allowance_formula": "BendAllowanceFormula",
    "bend_deduction_formula": "BendDeductionFormula",
}


class DefaultsData(TypedDict):
    thickness: float | None
    bend_radius: float | None
    neutral_factor: float | None
    bend_relief_width: float | None
    bend_relief_depth: float | None
    parameter_entry: str
    bend_definition: str
    bend_table: str | None
    bend_allowance_formula: str | None
    bend_deduction_formula: str | None
    material: str | None
    tool: str | None


def preferences(tx):
    return tx.part.Preferences.SheetMetalPreferences


def default_expression(tx, key):
    """The part's default expression of ``key`` (``Sheet_Metal_Bend_Radius``...), or None."""
    return getattr(preferences(tx), "Get" + NUMBERS[key])()


def default_name(tx, key) -> str | None:
    """The name of the part's default expression ``key``: a feature expression set to it
    follows later default changes."""
    exp = default_expression(tx, key)
    name = str(getattr(exp, "Name", "") or "") if exp is not None else ""
    return name or None


def default_value(tx, key) -> float | None:
    """The part's default ``key`` in its expression's units (``neutral_factor``: unitless)."""
    exp = default_expression(tx, key)
    return expr.value_or_none(tx, exp) if exp is not None else None


def part_length(tx, key) -> float | None:
    """A length default (``thickness``...) in work-part units, or None when unset."""
    exp = getattr(preferences(tx), "Get" + LENGTHS[key])()
    number = expr.value(tx, exp) if exp is not None else None
    if number is None:
        return None
    have, want = expr.units(exp), tx.units.get("length")
    if (have, want) == ("in", "mm"):
        return float(number) * MM_PER_IN
    if (have, want) == ("mm", "in"):
        return float(number) / MM_PER_IN
    return float(number)


# Why NX may not keep a table-driven default, and what to send instead.
TABLE_HINTS = {
    "material": "NX keeps only a material of its sheet-metal standards table (site setup; "
    "none is listed). Leave material out to set the other defaults",
    "tool": "NX keeps only a tool of its sheet-metal standards table (site setup). Leave "
    "tool out to set the other defaults",
    "parameter_entry": "material_table and tool_id_table need a sheet-metal standards "
    "table on this NX; use value (or leave parameter_entry out)",
}


def _same_text(got, wanted) -> bool:
    return got is not None and str(got).strip().casefold() == str(wanted).strip().casefold()


def _text(value):
    text = str(value) if value is not None else ""
    return text or None


def _table(tx, value, raw):
    """The bend table as NX keeps it (``raw``), else workspace-relative when inside it."""
    text = _text(value)
    if text is None or raw or not Path(text).is_absolute():
        return text
    root = Path(tx.workspace.root)
    try:
        return Path(text).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):  # a site table outside the workspace: NX's own setting
        return text


def read(tx, raw: bool = False) -> DefaultsData:
    """The work part's sheet-metal defaults (numbers in their expressions' units); ``raw``
    keeps the bend table exactly as NX holds it (the undo sets it back)."""
    prefs = preferences(tx)
    numbers: dict[str, float | None] = {}
    for key, name in NUMBERS.items():
        exp = getattr(prefs, "Get" + name)()
        numbers[key] = expr.value(tx, exp) if exp is not None else None
    choices = tx.nx.Preferences.SheetMetalPreferencesBuilder
    entry = snake(
        result.enum_name(
            prefs.GetParameterEntryType(), choices.ParameterEntryTypes, list(ENTRY.values())
        )
    )
    method = result.enum_name(
        prefs.GetBendDefinitionMethod(), choices.BendDefinitionMethodOptions, list(METHODS.values())
    )
    return {
        "thickness": numbers["thickness"],
        "bend_radius": numbers["bend_radius"],
        "bend_relief_width": numbers["bend_relief_width"],
        "bend_relief_depth": numbers["bend_relief_depth"],
        "neutral_factor": numbers["neutral_factor"],
        "parameter_entry": entry,
        "bend_definition": next((k for k, v in METHODS.items() if v == method), snake(method)),
        "bend_table": _table(tx, prefs.GetBendTable(), raw),
        "bend_allowance_formula": _text(prefs.GetBendAllowanceFormula()),
        "bend_deduction_formula": _text(prefs.GetBendDeductionFormula()),
        "material": _text(prefs.GetMaterialName()),
        "tool": _text(prefs.GetToolName()),
    }


@tool(
    effect="read",
    label="Get sheet-metal defaults",
    summary="Read sheet-metal defaults",
    keywords=("sheet metal preferences", "default thickness", "bend radius", "k factor", "relief"),
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_get_sheet_metal_defaults(tx: Tx) -> DefaultsData:
    """Read the part's sheet-metal creation defaults (null: not set). They cover thickness,
    bend radius, neutral factor, reliefs, bend method, bend table (NX's table name, or the
    file set), both formulas, material and tool; tables and formulas apply only to their
    bend_definition."""
    return read(tx)


def _summary(a, say) -> str:
    parts = []
    for key, short in (("thickness", "t"), ("bend_radius", "R"), ("neutral_factor", "K")):
        if a.get(key) is not None:
            parts.append(f"{short} {say(key)}")
    shown = ("thickness", "bend_radius", "neutral_factor", "apply_to_existing")
    rest = [k for k in a if a.get(k) is not None and k not in shown]
    text = "Sheet-metal defaults " + ", ".join(parts)
    if rest:
        text += f" +{len(rest)}"
    if a.get("apply_to_existing") is False:
        text += ", existing kept"
    return text


# Defaults a bend method change reaches: the features following the part's K.
METHOD_KEYS = ("parameter_entry", "bend_definition", "bend_table", "bend_allowance_formula",
               "bend_deduction_formula", "material", "tool")  # fmt: skip
NAME = re.compile(r"[A-Za-z_]\w*")
LISTED = 10  # features named in the note


def _refers(exp, names) -> bool:
    return bool(names & set(NAME.findall(str(getattr(exp, "RightHandSide", "") or ""))))


def followers(tx, keys) -> dict[int, tuple[Any, list]]:
    """``{feature tag: (feature, [its expressions naming a default])}``: the features whose
    expressions refer to the default expressions of ``keys``, directly or through other
    expressions."""
    exps = [e for e in (default_expression(tx, k) for k in keys) if e is not None]
    names = {str(e.Name) for e in exps}
    reach = {int(e.Tag) for e in exps}
    queue = list(exps)
    while queue:
        for dep in expr.dependents(queue.pop()):
            if int(dep.Tag) not in reach:
                reach.add(int(dep.Tag))
                queue.append(dep)
    out: dict[int, tuple[Any, list]] = {}
    if not exps:
        return out
    for feature in tx.part.Features:
        tx.checkpoint()
        try:
            own = list(feature.GetExpressions())
        except Exception:  # a feature without expressions
            continue
        direct = [e for e in own if _refers(e, names)]
        if direct or any(int(getattr(e, "Tag", 0) or 0) in reach for e in own):
            out[int(feature.Tag)] = (feature, direct)
    return out


def _changed(before, key, value) -> bool:
    have = before.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return have is None or not geom.close(float(have), float(value), 1e-9)
    return not _same_text(have, value)


def _freeze(tx, found) -> list[dict[str, str]]:
    """Set the default-naming expressions of ``found`` features to their current values."""
    frozen = []
    for _feature, direct in found.values():
        for exp in direct:
            number = expr.value_or_none(tx, exp)
            if number is None:
                continue
            frozen.append({"name": str(exp.Name), "formula": str(exp.RightHandSide)})
            expr.set_formula(tx, exp, repr(float(number)))
            tx.modified(exp, "expression")
    return frozen


@tool(
    effect="model",
    label="Set sheet-metal defaults",
    summary=_summary,
    keywords=("set thickness", "set bend radius", "neutral factor", "bend allowance", "relief"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    invalidates="deleted",
    refresh_annotations=False,
    example={"thickness": "2 mm", "bend_radius": 3, "neutral_factor": 0.33},
)
def nx_set_sheet_metal_defaults(
    tx: Tx,
    *,
    thickness: Length | None = None,
    bend_radius: Length | None = None,
    neutral_factor: Annotated[float, Range(ge=0, le=1)] | None = None,
    bend_relief_width: Length | None = None,
    bend_relief_depth: Length | None = None,
    parameter_entry: ParameterEntry | None = None,
    bend_definition: BendDefinition | None = None,
    bend_table: Annotated[str, PathTo("read")] | None = None,
    bend_allowance_formula: Formula | None = None,
    bend_deduction_formula: Formula | None = None,
    bend_formula: Annotated[Formula, Doc("The bend_definition's formula (alias)")] | None = None,
    material: TableName | None = None,
    tool: TableName | None = None,
    apply_to_existing: Annotated[
        bool, Doc("false: features made with default values keep them")
    ] = True,
) -> DefaultsData:
    """Change the part's sheet-metal creation defaults; omitted fields keep their values.
    Existing features made with default values follow the change (changes.modified, a
    note); apply_to_existing=false keeps their values. Values given to a feature stay."""
    numbers = {
        "thickness": thickness,
        "bend_radius": bend_radius,
        "neutral_factor": neutral_factor,
        "bend_relief_width": bend_relief_width,
        "bend_relief_depth": bend_relief_depth,
    }
    given = {k: v for k, v in numbers.items() if v is not None}
    texts = {
        "parameter_entry": parameter_entry,
        "bend_definition": bend_definition,
        "bend_table": bend_table,
        "material": material,
        "tool": tool,
    }
    named = {
        k: v
        for k, v in (
            ("bend_allowance_formula", bend_allowance_formula),
            ("bend_deduction_formula", bend_deduction_formula),
        )
        if v is not None
    }
    nothing = not given and bend_formula is None and not named
    if nothing and all(v is None for v in texts.values()):
        fail("NX_INVALID_ARGUMENT", "Give at least one sheet-metal default to change")
    if thickness is not None and float(thickness) <= 0:
        fail("NX_INVALID_ARGUMENT", "thickness must be positive", field="/thickness")
    for key in ("bend_radius", "bend_relief_width", "bend_relief_depth"):
        value = numbers[key]
        if value is not None and float(value) < 0:
            fail("NX_INVALID_ARGUMENT", f"{key} cannot be negative", field="/" + key)
    if bend_formula is not None and bend_definition not in (None, *FORMULAS):
        fail("NX_INVALID_ARGUMENT", "bend_formula belongs to a formula bend_definition",
             field="/bend_formula")  # fmt: skip
    with tx.checks():
        before = read(tx)
        raw_before = read(tx, raw=True)
        method = bend_definition or before["bend_definition"]
        formulas: dict[str, str] = dict(named)
        if bend_formula is not None:
            if method not in FORMULAS:
                fail("NX_INVALID_ARGUMENT", f"The part's bend method is {method}; set "
                     "bend_definition to a formula method with bend_formula",
                     field="/bend_formula")  # fmt: skip
            if formulas.get(method, bend_formula) != bend_formula:
                fail("NX_INVALID_ARGUMENT", f"bend_formula and {method} differ; give one",
                     field="/bend_formula")  # fmt: skip
            formulas[method] = bend_formula
        changing = [k for k, v in given.items() if _changed(before, k, float(v))]
        wanted = {**texts, **formulas}
        methods = [
            k for k in METHOD_KEYS if wanted.get(k) is not None and _changed(before, k, wanted[k])
        ]
        if methods and not apply_to_existing:
            fail("NX_INVALID_ARGUMENT", "apply_to_existing=false keeps numeric defaults only; "
                 f"{', '.join(methods)} reaches every feature that follows the part's K",
                 field="/apply_to_existing",
                 hint="Change the bend method in a call of its own")  # fmt: skip
        found = followers(tx, [*changing, *(["neutral_factor"] if methods else [])])
    frozen = [] if apply_to_existing else _freeze(tx, found)
    choices = tx.nx.Preferences.SheetMetalPreferencesBuilder
    with builders.build(tx, preferences(tx).CreateSheetMetalPreferencesBuilder) as b:
        if parameter_entry is not None:
            b.ParameterEntryType = getattr(choices.ParameterEntryTypes, ENTRY[parameter_entry])
        if material is not None:
            b.SetMaterial(material)
        if tool is not None:
            b.SetToolName(tool)
        if bend_definition is not None:
            b.SetBendDefinitionMethod(
                getattr(choices.BendDefinitionMethodOptions, METHODS[bend_definition])
            )
        if bend_table is not None:
            b.SetBendTable(str(bend_table))
        for key, text in formulas.items():
            setattr(b, FORMULAS[key], text)
        for key, value in given.items():
            getattr(b, NUMBERS[key]).RightHandSide = repr(float(value))
        b.commit()
    model.update(tx)
    got = read(tx)
    read_back: Mapping[str, Any] = got  # looked up by the argument names
    for key, value in given.items():
        readback.expect(
            read_back[key] is not None and geom.close(read_back[key], float(value), 1e-9),
            key,
            f"NX kept {key} = {read_back[key]}, not {value}",
            wanted=value,
            got=read_back[key],
        )
    if bend_definition is not None:
        readback.expect(read_back["bend_definition"] == bend_definition, "bend_definition",
                        wanted=bend_definition, got=read_back["bend_definition"])  # fmt: skip
    for key in ("parameter_entry", "material", "tool"):
        asked = texts[key]
        if asked is not None and not _same_text(read_back[key], asked):
            fail("NX_VERIFICATION_FAILED", f"NX did not keep {key} {asked!r} (it reads "
                 f"{read_back[key]!r}); nothing was changed", field="/" + key,
                 hint=TABLE_HINTS[key],
                 details={"check": key, "wanted": asked, "got": read_back[key]})  # fmt: skip
    for key, text in formulas.items():
        readback.expect(read_back[key] == text, key, wanted=text, got=read_back[key])
    _report(tx, found, frozen)
    after = read(tx, raw=True)
    tx.note_restore({"before": dict(raw_before), "after": dict(after), "frozen": frozen})
    return got


def _report(tx, found, frozen) -> None:
    """List the existing features the change reached (or kept) in the call's changes."""
    if not found:
        return
    rows = [tx.ref(feature, "feature") for feature, _direct in found.values()]
    names = ", ".join(r["id"] for r in rows[:LISTED]) + (" ..." if len(rows) > LISTED else "")
    if frozen:
        tx.warn("NX_NOTE", f"{len(rows)} existing feature(s) made with default values kept "
                f"them ({len(frozen)} expression(s) set to numbers): {names}")  # fmt: skip
        return
    for row in rows:
        tx.modified(row)
    tx.warn("NX_NOTE", f"{len(rows)} existing feature(s) follow the part defaults and changed "
            f"with them: {names}; apply_to_existing=false keeps their values")  # fmt: skip


def _equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return geom.close(float(a), float(b), 1e-9)
    return _same_text(a, b)


@restorer("nx_set_sheet_metal_defaults")
def _restore_defaults(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set the sheet-metal defaults an ``nx_set_sheet_metal_defaults`` changed back (the
    compensating undo), and the formulas of the expressions it set to numbers: refused
    when one changed again since, or had no value before (NX cannot unset a default)."""
    before, after = data["before"], data["after"]
    keys = [k for k in before if not _equal(before[k], after.get(k))]
    now = read(tx, raw=True)
    moved = [k for k in keys if not _equal(now.get(k), after.get(k))]
    if moved:
        fail(
            "NX_DEPENDENCY_ORDER",
            "The sheet-metal defaults changed since: "
            + ", ".join(
                f"{k} is {now.get(k)!r}, the operation set {after.get(k)!r}" for k in moved
            ),
            details={"changed": moved},
        )
    unset = [k for k in keys if before[k] is None]
    if unset:
        fail(
            "NX_UNSUPPORTED_OPTION",
            f"{', '.join(unset)} had no value before the operation: NX cannot unset a "
            "sheet-metal default",
            details={"unset": unset},
        )
    frozen = data.get("frozen") or []
    gone = [f["name"] for f in frozen if expr.find(tx, f["name"]) is None]
    if gone:
        fail("NX_DEPENDENCY_ORDER", f"Expressions the operation set are gone: {', '.join(gone)}",
             details={"gone": gone})  # fmt: skip
    if apply and keys:
        rest = {k: before[k] for k in keys}
        if any(k in FORMULAS for k in keys):  # a formula is set back with the method it had
            rest.setdefault("bend_definition", before["bend_definition"])
        nx_set_sheet_metal_defaults(tx, **rest)
        got = read(tx, raw=True)
        wrong = [k for k in keys if not _equal(got.get(k), before[k])]
        readback.expect(not wrong, "defaults", "The sheet-metal defaults "
                        f"{', '.join(wrong)} did not read back their previous values",
                        names=wrong)  # fmt: skip
    if apply:
        for item in frozen:
            expr.set_formula(tx, expr.find(tx, item["name"]), item["formula"])
    return "sheet-metal defaults back to " + (
        ", ".join(f"{k} {before[k]!r}" for k in keys) or "(unchanged)"
    )
