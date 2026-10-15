"""Drafting symbols on a sheet: ID symbols, NX library symbols, datum target points,
intersection symbols and edge condition symbols.

NX 2406 findings encoded here:

- ID symbols (``IdSymbols.CreateIdSymbolBuilder``): ``Type``, ``UpperText``/``LowerText``
  and ``Size`` (part units: a circle's diameter, else the circumscribed circle); the
  origin is the symbol's centre. Read back from a reopened builder.
- Library symbols come from NX's symbol library (``UGII_PMI_SYMBOL_LIBRARY_DIR``, files
  ``<name>.sym.prt``): ``SelectSymbol(<full path>)`` loads one; the reopened builder
  reports its ``MasterSymbolPath``. A leader appended to its ``Leader`` is dropped (the
  committed symbol has none), so library symbols are placed without one.
- A target point (``TargetPointBuilder.Location`` = a circular edge in a view) sits at the
  circle's centre and follows it; ``Height`` is the size of its cross.
- An intersection symbol (``IntersectionObject1``/``2`` = two edges in a view) has its
  origin at the virtual intersection of the two extended edges (a corner removed by a
  chamfer or blend).
- Edge condition symbols (ISO 13715, ``EdgeConditionSymbol`` collection) keep only the
  deviations of their method and drop others silently: ``UndefinedDirectionDefinedSize``
  keeps ``LimitDeviationA1``/``A2``, ``UndefinedDirectionEqualBilateralSize``
  ``LimitDeviation``; ``UndefinedDirectionandSize`` keeps the sign (``SymbolElement``).
- Every symbol is view dependent in its sheet's own view. All need a drafting license.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Doc,
    Items,
    Range,
    RefTo,
    SheetLength,
    SheetPoint,
    Text,
)
from nx_mcp.domains.drawing.common import (
    SheetArg,
    ViewArg,
    add_leader,
    check_leader,
    commit,
    default_origin,
    geometry,
    leader_row,
    open_or_only_sheet,
    report_bounds,
    set_origin,
    sheet_of,
    true_shape,
    update,
    verify_origin,
)
from nx_mcp.kit import args, builders, drawing, facts, licenses, readback, result

LIBRARY_ENV = "UGII_PMI_SYMBOL_LIBRARY_DIR"
SUFFIX = ".sym.prt"
SHAPES = {
    "circle": "Circle",
    "divided_circle": "DividedCircle",
    "triangle": "Triangle",
    "triangle_up": "TriangleUp",
    "square": "Square",
    "divided_square": "DividedSquare",
    "hexagon": "Hexagon",
    "divided_hexagon": "DividedHexagon",
    "quadrant_circle": "QuadrantCircle",
    "rounded_box": "RoundedBox",
    "underline": "Underline",
}
TWO_TEXTS = ("divided_circle", "divided_square", "divided_hexagon", "quadrant_circle")
Shape = Literal[
    "circle", "divided_circle", "triangle", "triangle_up", "square", "divided_square",
    "hexagon", "divided_hexagon", "quadrant_circle", "rounded_box", "underline",
]  # fmt: skip
CONDITIONS = {
    "basic": "Basic",
    "plus": "UndefinedDirectionandSize",
    "minus": "UndefinedDirectionandSize",
    "size": "UndefinedDirectionDefinedSize",
    "bilateral": "UndefinedDirectionEqualBilateralSize",
}
SIGNED = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
UNSIGNED = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
EdgeOrFace = Annotated[str, RefTo("edge", "face")]
Deviation = Annotated[str, Text(16), Doc("'+0.3', '-0.1' or '0.2'")]


class SymbolData(TypedDict, total=False):
    annotation: result.Row
    sheet: result.Row
    origin: Annotated[list[float], Doc("The symbol's origin on the sheet (sheet units)")]
    bounds: list[float]
    text: list[str]
    leader: dict
    symbol: dict


def _sheet(tx, view_ref, sheet_ref):
    """``(sheet, view or None)``: the view's sheet, the given sheet or the open/only one."""
    if view_ref is not None:
        view, owner = drawing.view(tx, view_ref)
        if sheet_ref is not None and int(sheet_of(tx, sheet_ref, "/sheet").Tag) != int(owner.Tag):
            fail("NX_WRONG_OWNER", "view is not on the given sheet", field="/view")
        return owner, view
    if sheet_ref is not None:
        return sheet_of(tx, sheet_ref, "/sheet"), None
    return open_or_only_sheet(tx), None


def library_symbols(tx):
    """``{name (lower case): path}`` of the symbols in NX's symbol library."""
    root = None
    getter = getattr(tx.session, "GetEnvironmentVariableValue", None)
    if getter is not None:
        try:
            root = str(getter(LIBRARY_ENV) or "")
        except Exception:  # NX raises for an unset variable
            root = None
    root = root or os.environ.get(LIBRARY_ENV)
    if not root or not Path(root).is_dir():
        fail("NX_NOT_FOUND", "NX has no symbol library folder",
             details={"variable": LIBRARY_ENV})  # fmt: skip
    found = {}
    for path in sorted(Path(root).rglob("*" + SUFFIX)):
        found.setdefault(path.name[: -len(SUFFIX)].casefold(), path)
    return found


def _library_path(tx, name):
    symbols = library_symbols(tx)
    path = symbols.get(name.casefold())
    if path is None:
        fail(
            "NX_NOT_FOUND",
            f"NX's symbol library has no symbol {name!r}",
            field="/name",
            hint="Pick a name from details.names",
            details={"names": sorted(symbols)[:100]},
        )
    return path


def _deviations(condition, deviations):
    """The deviation texts a condition takes, checked before NX."""
    given = list(deviations or [])
    if condition in ("size", "bilateral") and not given:
        fail("NX_INVALID_ARGUMENT", f"condition {condition} needs deviations",
             field="/deviations")  # fmt: skip
    if condition not in ("size", "bilateral") and given:
        fail("NX_INVALID_ARGUMENT", "deviations apply to condition size or bilateral",
             field="/deviations")  # fmt: skip
    if condition == "bilateral" and len(given) != 1:
        fail("NX_INVALID_ARGUMENT", "bilateral takes one deviation (shown as +/- value)",
             field="/deviations")  # fmt: skip
    pattern = UNSIGNED if condition == "bilateral" else SIGNED
    for i, text in enumerate(given):
        if not pattern.match(text.strip()):
            fail("NX_INVALID_ARGUMENT", f"{text!r} is not a deviation such as "
                 + ("'0.2'" if condition == "bilateral" else "'+0.3' or '-0.1'"),
                 field=f"/deviations/{i}")  # fmt: skip
    return [t.strip() for t in given]


def _line_meet(p0, p1, q0, q1):
    """Where the lines through ``p0 p1`` and ``q0 q1`` meet (2D), or None if parallel."""
    d1 = [p1[0] - p0[0], p1[1] - p0[1]]
    d2 = [q1[0] - q0[0], q1[1] - q0[1]]
    den = d1[0] * d2[1] - d1[1] * d2[0]
    span = max(1.0, *(abs(v) for v in (*d1, *d2)))
    if abs(den) <= 1e-9 * span * span:
        return None
    t = ((q0[0] - p0[0]) * d2[1] - (q0[1] - p0[1]) * d2[0]) / den
    return [p0[0] + t * d1[0], p0[1] + t * d1[1]]


def _check_shape(symbol, view, objects, origin, text, lower_text, shape, name, condition,
                 deviations, size):  # fmt: skip
    """Cross-field rules of each variant, before NX."""
    wanted = {"target_point": 1, "intersection": 2, "edge_condition": 1}.get(symbol)
    if objects is not None and len(objects) != (wanted or 1):
        fail("NX_INVALID_ARGUMENT", f"{symbol} takes {wanted or 1} object(s)"
             + ("" if wanted else " (the leader's edge or face)"), field="/objects")  # fmt: skip
    if objects is not None and view is None:
        fail("NX_INVALID_ARGUMENT", "objects are picked as shown in a view: pass view",
             field="/view")  # fmt: skip
    if symbol == "custom" and objects is not None:
        fail("NX_UNSUPPORTED_OPTION", "Library symbols are placed without a leader: omit "
             "objects", field="/objects")  # fmt: skip
    if symbol in ("id", "custom") and origin is None and objects is None:
        fail("NX_INVALID_ARGUMENT", "origin is required without a leader", field="/origin")
    if symbol in ("target_point", "intersection") and origin is not None:
        fail("NX_INVALID_ARGUMENT", f"{symbol} sits on its geometry: omit origin",
             field="/origin")  # fmt: skip
    extra = {
        "lower_text": lower_text is not None and (symbol != "id" or shape not in TWO_TEXTS),
        "text": text is not None and symbol != "id",
        "shape": shape != "circle" and symbol != "id",
        "name": name is not None and symbol != "custom",
        "condition": condition != "basic" and symbol != "edge_condition",
        "size": size is not None and symbol not in ("id", "target_point"),
    }
    for field, bad in extra.items():
        if bad:
            fail("NX_INVALID_ARGUMENT", f"{field} does not apply here (see the description)",
                 field=f"/{field}")  # fmt: skip
    if symbol == "custom" and not NAME.match(name or ""):
        fail("NX_INVALID_ARGUMENT", f"{name!r} is not a symbol name", field="/name")
    if symbol == "edge_condition":
        return _deviations(condition, deviations)
    if deviations is not None:
        fail("NX_INVALID_ARGUMENT", "deviations apply to edge_condition", field="/deviations")
    return []


@tool(
    effect="model",
    label="Create sheet symbol",
    summary={
        "id": "ID symbol {text}",
        "custom": "Library symbol {name}",
        "target_point": "Target point on {objects} in {view}",
        "intersection": "Intersection symbol of {objects} in {view}",
        "edge_condition": "Edge condition symbol on {objects} in {view}",
    },
    keywords=(
        "id symbol",
        "custom symbol",
        "symbol library",
        "datum target point",
        "intersection symbol",
        "virtual sharp",
        "edge condition",
        "ISO 13715",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="symbol",
    required_by={
        "id": ("text",),
        "custom": ("name",),
        "target_point": ("view", "objects"),
        "intersection": ("view", "objects"),
        "edge_condition": ("view", "objects"),
    },
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"symbol": "id", "text": "1", "shape": "circle", "origin": [300, 250]},
)
def nx_create_sheet_symbol(
    tx: Tx,
    *,
    symbol: Literal["id", "custom", "target_point", "intersection", "edge_condition"],
    view: ViewArg | None = None,
    objects: Annotated[
        list[EdgeOrFace],
        Items(1, 2),
        Doc(
            "Leader target (id, edge_condition); target_point: 1 circular edge; "
            "intersection: 2 straight edges"
        ),
    ]
    | None = None,
    origin: SheetPoint | None = None,
    sheet: SheetArg | None = None,
    text: Annotated[str, Text(64), Doc("id: the text (upper half of a divided shape)")]
    | None = None,
    lower_text: Annotated[str, Text(64), Doc("id: lower half of a divided shape")] | None = None,
    shape: Shape = "circle",
    size: Annotated[SheetLength, Range(gt=0), Doc("id: symbol size; target_point: cross size")]
    | None = None,
    name: Annotated[str, Text(64), Doc("custom: NX library symbol, e.g. 'balloon_note'")]
    | None = None,
    condition: Literal["basic", "plus", "minus", "size", "bilateral"] = "basic",
    deviations: Annotated[list[Deviation], Items(1, 2)] | None = None,
) -> SymbolData:
    """Place an ID, library, target point, intersection or edge condition symbol on a sheet.
    id: text in a shape at origin, optional leader (view + objects); custom: an NX library
    symbol by name at origin. target_point sits on a circle's center, intersection
    at the virtual corner of 2 straight edges. edge_condition (ISO 13715, with a leader):
    condition plus/minus (sign), size (deviations [upper] or [upper, lower]) or
    bilateral ([value], shown +/-). Surface finish, weld: nx_create_drawing_symbol."""
    deviation_texts = _check_shape(symbol, view, objects, origin, text, lower_text, shape,
                                   name, condition, deviations, size)  # fmt: skip
    if objects is not None:
        args.distinct("/objects", objects, "objects")
    with tx.checks():
        target, shown = _sheet(tx, view, sheet)
        picked = []
        for i, ref in enumerate(objects or []):
            kinds = ("edge",) if symbol in ("target_point", "intersection") else ("edge", "face")
            picked.append(geometry(tx, ref, kinds, f"/objects/{i}"))
        spot = _spot(tx, symbol, shown, picked)
        path = _library_path(tx, name) if symbol == "custom" else None
    licenses.require(tx, ("drafting",), "Drafting symbols need a drafting license")
    with drawing.on_sheet(tx, target) as sh:
        if symbol in ("target_point", "intersection"):
            out = _on_geometry(tx, symbol, sh, shown, picked, spot, size)
        else:
            spec = {"text": text, "lower_text": lower_text, "shape": shape, "size": size,
                    "path": path, "condition": condition, "deviations": deviation_texts}  # fmt: skip
            out = _with_leader(tx, symbol, sh, shown, picked[0] if picked else None, origin, spec)
        out["sheet"] = tx.ref(target, "drawing_sheet")
        return out


def _spot(tx, symbol, view, picked):
    """The sheet point a target point or intersection symbol must land on (checked)."""
    if symbol == "target_point":
        f = facts.edge(tx, picked[0])
        if f["kind"] != "arc":
            fail("NX_WRONG_GEOMETRY", "A target point goes on a circular edge",
                 field="/objects/0")  # fmt: skip
        true_shape(view, f, "/objects/0")
        return drawing.to_sheet(tx, view, f["center"])
    if symbol == "intersection":
        ends = []
        for i, edge in enumerate(picked):
            f = facts.edge(tx, edge)
            if f["kind"] != "line":
                fail("NX_WRONG_GEOMETRY", "An intersection symbol takes straight edges",
                     field=f"/objects/{i}")  # fmt: skip
            ends.append((drawing.to_sheet(tx, view, f["start"]),
                         drawing.to_sheet(tx, view, f["end"])))  # fmt: skip
        corner = _line_meet(*ends[0], *ends[1])
        if corner is None:
            fail("NX_INVALID_ARGUMENT", "The edges look parallel in this view: they never meet",
                 field="/objects")  # fmt: skip
        return corner
    return None


def _on_geometry(tx, symbol, sh, view, picked, spot, size):
    A = tx.nx.Annotations
    part = tx.part
    if symbol == "target_point":
        factory, null = part.Annotations.TargetPoints.CreateTargetPointBuilder, A.TargetPoint.Null
    else:
        collection = part.Annotations.IntersectionSymbols
        factory, null = collection.CreateIntersectionSymbolBuilder, A.IntersectionSymbol.Null
    with builders.build(tx, factory, null) as b:
        if symbol == "target_point":
            edge = picked[0]
            b.Location.SetValue(edge, view, tx.nx.Point3d(*facts.edge(tx, edge)["center"]))
            if size is not None:
                b.Height = float(size) * sh.scale
        else:
            for selection, edge in zip((b.IntersectionObject1, b.IntersectionObject2), picked,
                                       strict=True):  # fmt: skip
                selection.SetValue(edge, view, tx.nx.Point3d(*facts.on_object(tx, edge)))
        obj = commit(b, symbol.replace("_", " "))
    update(tx)
    position = sh.to_sheet(obj.AnnotationOrigin)
    tol = 1e-3 * max(1.0, *map(abs, spot))
    readback.expect(
        abs(position[0] - spot[0]) <= tol and abs(position[1] - spot[1]) <= tol,
        "origin",
        "NX placed the symbol off the expected point",
        origin=position,
        expected=spot,
    )
    with builders.build(tx, factory, obj) as reader:
        if symbol == "target_point":
            first = reader.Location.GetValue()[0]
            stored = {"height": float(reader.Height) / sh.scale}
        else:
            first = reader.IntersectionObject1.GetValue()[0]
            stored = {"extension": float(reader.Extension) / sh.scale}
    readback.expect(first is not None and int(first.Tag) == int(picked[0].Tag), "geometry",
                    "The symbol is not attached to the requested edge")  # fmt: skip
    if size is not None:
        readback.expect_close(stored["height"], float(size), "size", 1e-6 * float(size))
    out: SymbolData = {
        "annotation": tx.ref(obj, "annotation"),
        "origin": position,
        "symbol": {
            "type": symbol,
            **stored,
            "objects": [tx.ref(e, "edge") for e in picked],
        },  # fmt: skip
    }
    return report_bounds(tx, out, obj, sh, "symbol", on_views=True)  # it sits on its edges


def _with_leader(tx, symbol, sh, view, obj, origin, spec):
    A = tx.nx.Annotations
    part = tx.part
    if symbol == "id":
        factory, null = part.Annotations.IdSymbols.CreateIdSymbolBuilder, A.IdSymbol.Null
    elif symbol == "custom":
        collection = part.Annotations.CustomSymbols
        factory, null = collection.CreateDraftingCustomSymbolBuilder, A.CustomSymbol.Null
    else:
        collection = part.Annotations.EdgeConditionSymbol
        factory, null = collection.CreateEdgeConditionSymbolBuilder, A.EdgeConditionSymbol.Null
    with builders.build(tx, factory, null) as b:
        if symbol == "id":
            b.Type = getattr(A.IdSymbolBuilder.SymbolTypes, SHAPES[spec["shape"]])
            b.UpperText = spec["text"]
            if spec["lower_text"] is not None:
                b.LowerText = spec["lower_text"]
            if spec["size"] is not None:
                b.Size = float(spec["size"]) * sh.scale
        elif symbol == "custom":
            b.SelectSymbol(str(spec["path"]))
        else:
            _configure_edge(tx, b, spec)
        anchor = add_leader(tx, b.Leader, obj, view) if obj is not None else None
        origin = origin if origin is not None else default_origin(sh, anchor, view)
        set_origin(tx, b.Origin, sh, origin)
        made = commit(b, "symbol")
    update(tx)
    with builders.build(tx, factory, made) as reader:
        stored = _read_symbol(tx, symbol, reader, sh)
        if obj is not None:
            check_leader(reader, obj, view)
    wanted = _wanted(symbol, spec)
    readback.expect(all(stored.get(k) == v for k, v in wanted.items()), "symbol",
                    "NX stored another symbol", actual=stored, wanted=wanted)  # fmt: skip
    if symbol == "id" and spec["size"] is not None:
        readback.expect_close(stored["size"], float(spec["size"]), "size",
                              1e-6 * float(spec["size"]))  # fmt: skip
    out: SymbolData = {
        "annotation": tx.ref(made, "annotation"),
        "origin": verify_origin(sh, made, origin),
        "symbol": {"type": symbol, **{k: v for k, v in stored.items() if k != "path"}},
    }
    if symbol == "id":
        out["text"] = [t for t in (stored["text"], stored.get("lower_text")) if t]
    elif symbol == "edge_condition":
        out["text"] = list(spec["deviations"])
    if obj is not None:
        out["leader"] = leader_row(tx, obj, view, anchor)
    return report_bounds(tx, out, made, sh, "symbol")


def _configure_edge(tx, b, spec):
    E = tx.nx.Annotations.BaseEdgeConditionSymbolBuilder
    condition = spec["condition"]
    b.Method = getattr(E.MethodType, CONDITIONS[condition])
    if condition in ("plus", "minus"):
        b.SymbolElement = getattr(E.SymbolElementEnum, condition.capitalize())
    texts = spec["deviations"]
    if condition == "size":
        b.LimitDeviationA1 = texts[0]
        if len(texts) > 1:
            b.LimitDeviationA2 = texts[1]
    elif condition == "bilateral":
        b.LimitDeviation = texts[0]


def _read_symbol(tx, symbol, reader, sh):
    A = tx.nx.Annotations
    if symbol == "id":
        shape = result.enum_name(reader.Type, A.IdSymbolBuilder.SymbolTypes, list(SHAPES.values()))
        return {
            "shape": next((k for k, v in SHAPES.items() if v == shape), shape),
            "text": str(reader.UpperText),
            "lower_text": str(reader.LowerText),
            "size": float(reader.Size) / sh.scale,
        }
    if symbol == "custom":
        path = str(reader.MasterSymbolPath)
        return {"name": Path(path).name[: -len(SUFFIX)] if path.endswith(SUFFIX) else path,
                "path": path.casefold(), "title": str(reader.MasterSymbolName)}  # fmt: skip
    E = A.BaseEdgeConditionSymbolBuilder
    method = result.enum_name(reader.Method, E.MethodType, list(dict.fromkeys(CONDITIONS.values())))
    element = result.enum_name(reader.SymbolElement, E.SymbolElementEnum, ["Plus", "Minus"])
    condition = next(k for k, v in CONDITIONS.items() if v == method) if method in set(
        CONDITIONS.values()) else method  # fmt: skip
    if method == "UndefinedDirectionandSize":
        condition = element.lower()
    deviations = {
        "size": [str(reader.LimitDeviationA1), str(reader.LimitDeviationA2)],
        "bilateral": [str(reader.LimitDeviation)],
    }.get(condition, [])
    return {"condition": condition, "deviations": [d for d in deviations if d]}


def _wanted(symbol, spec):
    if symbol == "id":
        out = {"shape": spec["shape"], "text": spec["text"]}
        if spec["lower_text"] is not None:
            out["lower_text"] = spec["lower_text"]
        return out
    if symbol == "custom":
        return {"path": str(spec["path"]).casefold()}
    return {"condition": spec["condition"], "deviations": list(spec["deviations"])}
