"""Interpart expressions: work-part expressions that read expressions of another loaded part
(``x = "master"::x``), so one master part drives parameters of others.

NX findings encoded here:

- ``ExpressionCollection.CreateInterpartExpressionsBuilder()`` creates them in the work
  part from ``SetExpressions(sources, names)``; an empty destination name lets NX's naming
  rule (prefix, default empty) name it, so the tool always passes the name it wants.
- The new expression is an interpart expression (``IsInterpartExpression``) whose
  formula names the source part and expression; its value follows the source after a
  model update of the work part.
- WAVE relinking is not used: the link names the source part by its file name.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core import target as targets
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Text, forbid_extra
from nx_mcp.kit import builders, expr, model, parts, readback, result

MAX_LINKS = 50
_EXPRESSION_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,99}$"
ExpressionName = Annotated[str, Text(100, _EXPRESSION_NAME)]


class _LinkRequired(TypedDict):
    source: Annotated[ExpressionName, Doc("Expression name in source_part (case-sensitive)")]


class Link(_LinkRequired, total=False):
    name: Annotated[ExpressionName, Doc("New work-part expression name; default the source name")]


forbid_extra(Link)


class Source(TypedDict):
    part: str
    expression: str
    value: float | None


class LinkRow(TypedDict):
    expression: result.Row
    formula: str
    value: float | None
    units: str
    source: Source


class InterpartData(TypedDict):
    items: list[LinkRow]


def _value(tx, exp):
    try:
        return expr.value(tx, exp)
    except Exception:  # a string or list expression has no numeric value
        return None


def _in_mm_or_deg(value, units):
    """``value`` in mm or degrees when ``units`` is a known length or angle unit."""
    known = {"mm": 1.0, "in": 25.4, "deg": 1.0, "rad": 57.29577951308232}
    return value * known[units] if value is not None and units in known else value


@tool(
    effect="model",
    label="Link interpart parameters",
    summary=lambda a, say: f"Link {say('expressions')} from {a.get('source_part')}",
    keywords=(
        "interpart",
        "link between parts",
        "master parameters",
        "control part",
        "linked parameter",
        "interpart link",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="light",
    example={"source_part": "master", "expressions": [{"source": "deck_len", "name": "len"}]},
)
def nx_create_interpart_link(
    tx: Tx,
    *,
    source_part: Annotated[
        str, Text(260), Doc("Loaded part holding the sources: file name, path or p:N")
    ],
    expressions: Annotated[list[Link], Items(1, MAX_LINKS)],
) -> InterpartData:
    """Create work-part expressions linked to expressions of another loaded part (master
    parameters): name = "source"::source. Read-only in the work part; edit the source
    (nx_set_expression in source_part), then nx_update_model in the work part updates them."""
    wanted = [(link["source"], link.get("name") or link["source"]) for link in expressions]
    seen: set[str] = set()
    for i, (_source, name) in enumerate(wanted):
        if name in seen:
            fail("NX_INVALID_ARGUMENT", f"{name} is named twice", field=f"/expressions/{i}")
        seen.add(name)
    with tx.checks():
        part = tx.part
        source = targets.part_from_text(tx.services, source_part, "/source_part")
        if parts.same_part(source, part):
            fail("NX_INVALID_ARGUMENT", "source_part is the work part itself",
                 field="/source_part", hint="Name another loaded part")  # fmt: skip
        available = {str(e.Name): e for e in source.Expressions}
        sources = []
        for i, (name, new_name) in enumerate(wanted):
            exp = available.get(name)
            if exp is None:
                fail("NX_NOT_FOUND", f"{parts.file_of(source)} has no expression {name!r}",
                     field=f"/expressions/{i}/source",
                     details={"available": sorted(available)[:30]})  # fmt: skip
            if expr.find(tx, new_name) is not None:
                fail("NX_NAME_CONFLICT", f"The work part already has an expression {new_name}",
                     field=f"/expressions/{i}", hint="Pass another name")  # fmt: skip
            sources.append(exp)
    names = [new for _src, new in wanted]
    with builders.build(tx, part.Expressions.CreateInterpartExpressionsBuilder) as b:
        with native(default="NX_BUILD_FAILED"):
            b.SetExpressions(sources, names)
        b.commit()
    model.update(tx)
    file_name = parts.file_of(source).replace("\\", "/").rsplit("/", 1)[-1]
    out: list[LinkRow] = []
    for (src_name, name), src in zip(wanted, sources, strict=True):
        exp = expr.find(tx, name)
        readback.expect(exp is not None, "expression", f"NX created no expression {name}")
        assert exp is not None
        readback.expect(bool(getattr(exp, "IsInterpartExpression", False)), "interpart",
                        f"{name} is not an interpart expression")  # fmt: skip
        value, units = _value(tx, exp), expr.units(exp)
        source_value, source_units = _value(tx, src), expr.units(src)
        if value is not None and source_value is not None:
            readback.expect_close(
                _in_mm_or_deg(value, units), _in_mm_or_deg(source_value, source_units),
                f"value.{name}", tol=1e-9 * max(1.0, abs(source_value)),
            )  # fmt: skip
        out.append(
            {
                "expression": tx.ref(exp, kind="expression"),
                "formula": str(exp.RightHandSide),
                "value": value,
                "units": units,
                "source": {"part": file_name, "expression": src_name, "value": source_value},
            }
        )
    return {"items": out}
