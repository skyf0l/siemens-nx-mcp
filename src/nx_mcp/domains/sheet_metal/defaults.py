"""Sheet-metal part defaults: thickness, bend radius, neutral factor, reliefs, bend method.

NX 2406 findings encoded here:

- ``SheetMetalPreferences.GetMaterialNames`` raises a native memory-access fault, so the
  installed material catalogue is never enumerated: a material or tool name is set as
  given and verified by read-back.
- Defaults work in Modeling too (before entering Sheet Metal) and on a plain part; they
  are creation defaults, existing features keep their own expressions.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Length, PathTo, Range, Text
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


def _text(value):
    text = str(value) if value is not None else ""
    return text or None


def read(tx) -> dict:
    """The work part's sheet-metal defaults (numbers in their expressions' units)."""
    prefs = preferences(tx)
    out: dict = {}
    for key, name in NUMBERS.items():
        exp = getattr(prefs, "Get" + name)()
        out[key] = expr.value(tx, exp) if exp is not None else None
    choices = tx.nx.Preferences.SheetMetalPreferencesBuilder
    out["parameter_entry"] = snake(
        result.enum_name(
            prefs.GetParameterEntryType(), choices.ParameterEntryTypes, list(ENTRY.values())
        )
    )
    method = result.enum_name(
        prefs.GetBendDefinitionMethod(), choices.BendDefinitionMethodOptions, list(METHODS.values())
    )
    out["bend_definition"] = next((k for k, v in METHODS.items() if v == method), snake(method))
    out["bend_table"] = _text(prefs.GetBendTable())
    out["bend_allowance_formula"] = _text(prefs.GetBendAllowanceFormula())
    out["bend_deduction_formula"] = _text(prefs.GetBendDeductionFormula())
    out["material"] = _text(prefs.GetMaterialName())
    out["tool"] = _text(prefs.GetToolName())
    return out


@tool(
    effect="read",
    label="Get sheet-metal defaults",
    summary="Read sheet-metal defaults",
    keywords=("sheet metal preferences", "default thickness", "bend radius", "k factor", "relief"),
    requires=("work_part",),
    cost="instant",
    example={},
)
def nx_get_sheet_metal_defaults(tx: Tx) -> DefaultsData:
    """Read the part's sheet-metal creation defaults. They cover thickness, bend radius,
    neutral factor, reliefs, bend method, bend table, formulas, material and tool."""
    return read(tx)


def _summary(a, say) -> str:
    parts = []
    for key, short in (("thickness", "t"), ("bend_radius", "R"), ("neutral_factor", "K")):
        if a.get(key) is not None:
            parts.append(f"{short} {say(key)}")
    rest = [k for k in a if a.get(k) is not None and k not in ("thickness", "bend_radius",
                                                                "neutral_factor")]  # fmt: skip
    text = "Sheet-metal defaults " + ", ".join(parts)
    if rest:
        text += f" +{len(rest)}"
    return text


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
    bend_table: PathTo("read") | None = None,  # type: ignore[valid-type]
    bend_formula: Annotated[Formula, Doc("Allowance or deduction formula")] | None = None,
    material: TableName | None = None,
    tool: TableName | None = None,
) -> DefaultsData:
    """Change the part's sheet-metal creation defaults. Omitted fields and existing
    features keep their values."""
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
    if not given and bend_formula is None and all(v is None for v in texts.values()):
        fail("NX_INVALID_ARGUMENT", "Give at least one sheet-metal default to change")
    if thickness is not None and thickness <= 0:
        fail("NX_INVALID_ARGUMENT", "thickness must be positive", field="/thickness")
    for key in ("bend_radius", "bend_relief_width", "bend_relief_depth"):
        if numbers[key] is not None and numbers[key] < 0:
            fail("NX_INVALID_ARGUMENT", f"{key} cannot be negative", field="/" + key)
    if bend_formula is not None and bend_definition not in (None, *FORMULAS):
        fail("NX_INVALID_ARGUMENT", "bend_formula belongs to a formula bend_definition",
             field="/bend_formula")  # fmt: skip
    with tx.checks():
        method = bend_definition or read(tx)["bend_definition"]
        if bend_formula is not None and method not in FORMULAS:
            fail("NX_INVALID_ARGUMENT", f"The part's bend method is {method}; set "
                 "bend_definition to a formula method with bend_formula",
                 field="/bend_formula")  # fmt: skip
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
        if bend_formula is not None:
            setattr(b, FORMULAS[method], bend_formula)
        for key, value in given.items():
            getattr(b, NUMBERS[key]).RightHandSide = repr(float(value))
        b.commit()
    model.update(tx)
    got = read(tx)
    for key, value in given.items():
        readback.expect(
            got[key] is not None and geom.close(got[key], float(value), 1e-9),
            key,
            f"NX kept {key} = {got[key]}, not {value}",
            wanted=value,
            got=got[key],
        )
    for key in ("parameter_entry", "bend_definition", "material", "tool"):
        if texts[key] is not None:
            readback.expect(got[key] == texts[key], key, wanted=texts[key], got=got[key])
    if bend_formula is not None:
        readback.expect(got[method] == bend_formula, "bend_formula", wanted=bend_formula,
                        got=got[method])  # fmt: skip
    return got
