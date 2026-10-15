"""Knowledge Fusion (KF): read, add and regenerate the KF rules of the work part.

NX 2406 findings encoded here (``BasePart.RuleManager``, probed live):

- Every part has the root instance ``root:``; its dynamic rules include one rule per
  NX expression (``IsExpressionRule``) and the child rules added to the part.
  ``GetDynamicRules(chain)`` lists an instance's dynamic rules; a child's class rules
  come from ``GetRulesOfClass(GetClass(chain))``.
- ``Evaluate(chain)`` returns a float, int, bool, str, a list or an NXObject (instance
  rules); ``EvaluateAnyAsString`` formats any of them. An unknown rule raises "No such
  rule"; an unknown instance 3270054 ("Referenced Instance does not exist").
- ``CreateChildRule(name, class, [ParameterRule])`` and ``CreateDynamicRule(chain,
  name, RuleType, formula)`` create (or modify) rules in the root; NX objects a child
  makes (an expression of ``ug_expression``, a body of ``ug_block``) appear only after
  ``DoKfUpdate(mark)``. An unknown class raises 3520002 ("Second parameter is invalid").
- Classes load from NX's KF class directories (the installed library and the
  directories NX was started with); a ``.dfa`` saved elsewhere, or a class directory set
  in a running session, is not found by ``CreateChildRule`` (``Reload`` included).
- Undo marks cover KF rules: undoing a call removes its rules and the objects they made.
- ``CreateChildRule`` on an existing child replaces its parameter list: a parameter not
  given goes back to the class default (a block 15 x 25 x 5 at y 50 became 1 x 1 x 9 at
  the origin). The tool merges: ``GetSpecifiedParameters(chain)`` lists the parameters a
  child sets (with ``Class``) and ``GetParameterFormula(chain, name)`` their formulas
  ("15;"). ``IsParameter(class, name)`` matches names in any case and raises "No such
  rule" for a name the class lacks; ``GetDefaultFormula(class, name)`` gives a default.
  Every class also has the KF base parameters (``BASE_PARAMETERS``), left out of lists.
- ``GetDynamicRuleText("root:name:")`` is "(Number) name: formula; ".
  ``GetRuleType(chain)`` returns a ``RuleManager.RuleType`` member whose ``value`` indexes
  ``RULE_TYPE_NAMES`` (``int()`` of the member itself raises; a point rule evaluates to a
  list).
- ``DeleteChildRule(name)`` and ``DeleteDynamicRule("root:", name)`` remove a rule.
- A KF child's syntax error is 3270008; an expression a KF rule drives changes value in
  the update without NX listing it, so values are compared around the update.

Not offered (NXOpen 2406): Product Template Studio templates whose data predates the
NX 2007 format answer ``ConfigurationManager.GetRootNode()`` with null, and NX's upgrade
call for that older data needs a node, so their user parameters are reachable only as
the part's expressions (nx_open_part + nx_set_expression). ``Session.ExecuteGrip`` runs a
compiled GRIP program, but without a GRIP compiler no program could be verified headless.
"""

from __future__ import annotations

import contextlib
import difflib
import math
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Offset, PageLimit, Text
from nx_mcp.kit import licenses, readback, result

ROOT = "root:"
CHAIN = re.compile(r"(?i)root:(?:[A-Za-z0-9_]+\??:){0,8}")
RULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\??")
CLASS = re.compile(r"%?[A-Za-z_][A-Za-z0-9_]{0,63}")
FORMULA_MAX = 1000
LIST_MAX = 50
# NX 2406 RuleManager errors.
NO_INSTANCE, BAD_ARGUMENT, SYNTAX_ERROR = 3270054, 3520002, 3270008
# Parameters every KF class has (NX 2406 ug_block, ug_expression): left out of listings.
BASE_PARAMETERS = frozenset({
    "inMntTree?", "EDUcontrol", "givenName", "inTree?", "Class", "Quantity",
    "referenceFrame", "demandOrder", "demandValue", "onCreate", "onDelete?",
    "onDeleteIgnoreKF?", "onUnload?", "lockRule?", "lockRuleParameters", "lockObject?",
    "onObjectSelected", "onModify?", "deleteTopInstance?",
})  # fmt: skip
# RuleManager.GetRuleType -> type name (NX 2406 GetRuleTypes order).
RULE_TYPE_NAMES = ("boolean", "frame", "integer", "list", "name", "number", "point", "string",
                   "vector", "instance", "user", "any", "group")  # fmt: skip
DYNAMIC_TEXT = re.compile(r"^\s*\([^)]*\)\s*[^:]+:\s*(.*?);?\s*$", re.DOTALL)
LICENSES = ("ug_kf_execute", "ug_kf_author")
RULE_TYPES = {
    "number": "Number", "integer": "Integer", "boolean": "Boolean", "string": "String",
    "list": "List", "point": "Point", "vector": "Vector", "frame": "Frame", "any": "Any",
}  # fmt: skip
FORMULA_HINT = (
    "KF syntax: rules end with ':' (length: * 2), strings in double quotes, true/false; "
    "list the rules with nx_get_kf_rules"
)

Formula = Annotated[str, Text(FORMULA_MAX), Doc("KF formula, e.g. 'length: * 2', '\"text\"'")]


class Parameter(TypedDict):
    name: Annotated[str, Text(64), Doc("Class parameter name, without ':'")]
    value: Annotated[
        float | bool | Annotated[str, Text(FORMULA_MAX)],
        Doc(
            'A number, true/false or a KF formula: 10, "text", Point(0,50,0); no units (not "10 mm")'
        ),
    ]


class RuleRow(TypedDict):
    name: str
    chain: Annotated[str, Doc("Reference chain to evaluate it again")]
    type: Annotated[
        str, Doc("number, integer, boolean, string, list, point, vector, frame, instance, other")
    ]
    value: Annotated[
        float | int | bool | str | list | None, Doc("null: an object or nested list (see text)")
    ]
    text: Annotated[str | None, Doc("NX's text of the value")]
    formula: Annotated[str | None, Doc("The rule's formula as set (null: computed or unknown)")]
    expression: Annotated[bool, Doc("The rule is an NX expression of the part")]
    child: Annotated[str | None, Doc("Class of a child rule (an instance), else null")]
    error: Annotated[str | None, Doc("Why NX could not evaluate it")]


class RulesData(TypedDict, total=False):
    part: result.Row
    instance: str
    class_name: Annotated[str | None, Doc("Class of the instance (null for root:)")]
    rules: list[RuleRow]
    parameters: Annotated[
        list[ClassParameter], Doc("class_name given: the class's parameters and defaults")
    ]
    page: Annotated[dict, Doc("offset, limit, returned, total, next_offset")]


class ClassParameter(TypedDict):
    name: str
    default: Annotated[str | None, Doc("The class's default formula")]


class SetRuleData(TypedDict, total=False):
    part: result.Row
    rule: RuleRow
    created: Annotated[bool, Doc("false: an existing rule was changed")]
    rules: Annotated[list[RuleRow], Doc("A child's class parameters (given ones first)")]
    kept_parameters: Annotated[
        list[str], Doc("Parameters of an existing child kept as they were (not given)")
    ]


class DeleteRuleData(TypedDict):
    part: result.Row
    name: str
    kind: str
    class_name: str | None


class UpdateData(TypedDict):
    part: result.Row
    rules: Annotated[int, Doc("Dynamic rules of root:")]
    failed: Annotated[list[RuleRow], Doc("Root rules that do not evaluate after the update")]


# --- argument checks (no NX) -------------------------------------------------------


def _chain(text: str, field: str) -> str:
    chain = text.strip()
    if not chain.endswith(":"):
        chain += ":"
    if not chain.lower().startswith(ROOT):
        chain = ROOT + chain
    if not CHAIN.fullmatch(chain):
        fail("NX_INVALID_ARGUMENT", f"{text!r} is not a KF instance chain", field=field,
             hint="Give root: or root:<child>: (child rule names end with ':')")  # fmt: skip
    return ROOT + chain[len(ROOT) :]


def _rule_name(text: str, field: str) -> str:
    name = text.strip().rstrip(":")
    if not RULE.fullmatch(name):
        fail("NX_INVALID_ARGUMENT", f"{text!r} is not a KF rule name", field=field,
             hint="Letters, digits and _ (a trailing ? for booleans), no ':'")  # fmt: skip
    return name


def _formula(value, field: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            fail("NX_INVALID_ARGUMENT", "A value must be finite", field=field)
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    text = str(value).strip()
    if not text or "\n" in text or "\r" in text:
        fail("NX_INVALID_ARGUMENT", "A KF formula is one non-empty line", field=field)
    return text


# --- NX reads ------------------------------------------------------------------------


def manager(tx, part):
    with native(default="NX_ERROR"):
        return part.RuleManager


def _last_error(rm) -> str:
    with contextlib.suppress(Exception):
        lines = [str(m).strip() for m in rm.GetLastError() or []]
        return "; ".join(m for m in lines if m)[:300]
    return ""


def _message(rm, error: Exception) -> str:
    detail = _last_error(rm)
    text = str(error).strip()
    return f"{text}: {detail}" if detail and detail not in text else text


def _plain(value):
    """A JSON value for ``Evaluate``'s answer, or None (objects, nested lists)."""
    if isinstance(value, bool):
        return value, "boolean"
    if isinstance(value, int):
        return value, "integer"
    if isinstance(value, float):
        return (value if math.isfinite(value) else None), "number"
    if isinstance(value, str):
        return value, "string"
    if isinstance(value, (list, tuple)):
        items = []
        for item in list(value)[:LIST_MAX]:
            plain, _kind = _plain(item)
            if plain is None or isinstance(plain, list):
                return None, "list"
            items.append(plain)
        return items, "list"
    if value is None:
        return None, "other"
    return None, "instance" if hasattr(value, "Tag") else "other"


def _type_index(member) -> int:
    """The index of a ``RuleManager.RuleType`` member (NX 2406 gives an enum member with
    ``value``; ``int()`` of the member raises)."""
    return int(getattr(member, "value", member))


def _child_class(rm, chain: str) -> str | None:
    try:
        return str(rm.GetClass(chain)) or None
    except Exception:
        return None


def row(rm, instance: str, name: str, *, expressions=None, formula: str | None = None) -> RuleRow:
    """Evaluate ``instance`` + ``name`` into a result row (errors kept in the row)."""
    chain = f"{instance}{name}:"
    out: RuleRow = {
        "name": name, "chain": chain, "type": "other", "value": None, "text": None,
        "formula": formula, "expression": bool(expressions and name in expressions),
        "child": None, "error": None,
    }  # fmt: skip
    try:
        raw = rm.Evaluate(chain)
    except Exception as error:
        out["error"] = _message(rm, error)
        return out
    out["value"], out["type"] = _plain(raw)
    if out["type"] == "list":  # a point, vector or frame evaluates to a list too
        with contextlib.suppress(Exception):
            kind = RULE_TYPE_NAMES[_type_index(rm.GetRuleType(chain))]
            if kind in ("point", "vector", "frame"):
                out["type"] = kind
    with contextlib.suppress(Exception):
        out["text"] = str(rm.EvaluateAnyAsString(chain))[:500]
    if out["type"] == "instance":
        out["child"] = _child_class(rm, chain)
        out["expression"] = False
    if isinstance(out["value"], str) and len(out["value"]) > 500:
        out["value"] = out["value"][:500]
    return out


def _clean(formula) -> str | None:
    """A formula as NX gives it back ("15;"), without the closing ';'."""
    if formula is None:
        return None
    text = str(formula).strip()
    return text[:-1].rstrip() if text.endswith(";") else text


def dynamic_formula(rm, name: str) -> str | None:
    """The formula of the dynamic root rule ``name`` (``GetDynamicRuleText``), or None."""
    try:
        text = str(rm.GetDynamicRuleText(f"{ROOT}{name}:"))
    except Exception:
        return None
    match = DYNAMIC_TEXT.match(text)
    return _clean(match.group(1)) if match and "{" not in match.group(1) else None


def specified(rm, chain: str) -> dict[str, str | None]:
    """``{parameter: formula}`` a child instance sets (``GetSpecifiedParameters``), its
    class not included; ``{}`` when NX cannot tell."""
    try:
        names = [str(n) for n in rm.GetSpecifiedParameters(chain)]
    except Exception:
        return {}
    out: dict[str, str | None] = {}
    for name in names:
        if name.casefold() == "class":
            continue
        try:
            out[name] = _clean(rm.GetParameterFormula(chain, name))
        except Exception:
            out[name] = None
    return out


def class_parameters(rm, klass: str) -> list[str]:
    """The input parameters of a KF class, without the base ones every class has."""
    out = []
    with contextlib.suppress(Exception):
        for name in rm.GetRulesOfClass(klass):
            name = str(name)
            if name in BASE_PARAMETERS:
                continue
            with contextlib.suppress(Exception):
                if rm.IsParameter(klass, name):
                    out.append(name)
    return out


def _parameter_rows(rm, klass: str) -> list[ClassParameter]:
    rows: list[ClassParameter] = []
    for name in class_parameters(rm, klass)[:LIST_MAX]:
        default = None
        with contextlib.suppress(Exception):
            default = _clean(rm.GetDefaultFormula(klass, name))
        rows.append({"name": name, "default": default})
    return rows


def dynamic_rules(rm, instance: str, field: str | None = "/instance") -> list[str]:
    try:
        return [str(n) for n in rm.GetDynamicRules(instance)]
    except Exception as error:
        code = getattr(error, "ErrorCode", None)
        fail("NX_NOT_FOUND", f"The part has no KF instance {instance}", field=field,
             hint="List the root rules with nx_get_kf_rules; children are rules of root:",
             details={"nx_message": _message(rm, error)},
             nx_code=int(code) if isinstance(code, int) else None)  # fmt: skip


def _expression_rules(rm, part, names) -> set[str]:
    known = set()
    with contextlib.suppress(Exception):
        known = {str(e.Name) for e in part.Expressions}
    out = set()
    for name in names:
        if name in known:
            out.add(name)
            continue
        with contextlib.suppress(Exception):
            if rm.IsExpressionRule(name):
                out.add(name)
    return out


def rule_names(rm, instance: str) -> tuple[list[str], str | None]:
    """The instance's rules (dynamic first, then its class's) and its class."""
    names = dynamic_rules(rm, instance)
    if instance == ROOT:
        return names, None
    klass = _child_class(rm, instance)
    if klass is None:  # NX lists no dynamic rules of an instance that does not exist
        fail(
            "NX_NOT_FOUND",
            f"The part has no KF instance {instance}",
            field="/instance",
            hint="List the root rules with nx_get_kf_rules; children are rules of root:",
        )
    if klass:
        with contextlib.suppress(Exception):
            for name in rm.GetRulesOfClass(klass):
                if str(name) not in names:
                    names.append(str(name))
    return names, klass


# --- tools ---------------------------------------------------------------------------


@tool(
    effect="read",
    label="Get KF rules",
    summary=lambda a, say: (
        f"Get the parameters of KF class {a['class_name']}"
        if a.get("class_name")
        else f"Get the Knowledge Fusion rules of {a.get('instance') or ROOT}"
    ),
    keywords=(
        "knowledge fusion",
        "kf rule",
        "kf class parameters",
        "rule formula",
        "dfa",
        "child rule",
        "evaluate",
    ),
    requires=("work_part",),
    cost="light",
    example={"instance": "root:", "rules": ["p0"]},
)
def nx_get_kf_rules(
    tx: Tx,
    *,
    instance: Annotated[
        str, Text(300), Doc("KF instance: root: (the part) or a child chain, root:plate1:")
    ] = ROOT,
    rules: Annotated[
        list[Annotated[str, Text(64)]],
        Items(1, 50),
        Doc("Rule names of the instance to evaluate (no ':'); default all, paged"),
    ]
    | None = None,
    class_name: Annotated[
        str, Text(65), Doc("A KF class to describe instead: its parameters and defaults")
    ]
    | None = None,
    internal: Annotated[
        bool, Doc("Child instance: also its computed and internal rules, not only parameters")
    ] = False,
    offset: Offset = 0,
    limit: PageLimit = 50,
) -> RulesData:
    """Evaluate the Knowledge Fusion rules of a KF instance of the work part. root: holds
    the part's NX expressions and child rules; each row gives value, type, formula and NX's
    text. A child rule's row names its class: pass its chain as instance to read its
    parameters. class_name lists a class's parameters with defaults. Add rules with
    nx_set_kf_rule."""
    part = tx.part
    rm = manager(tx, part)
    if class_name is not None:
        described = _class_name(class_name)
        with tx.checks():
            _require_class(rm, described)
        rows = _parameter_rows(rm, described)
        shown_rows, page = result.page(rows, offset, limit)
        return {
            "part": tx.ref(part, "part"),
            "instance": ROOT,
            "class_name": described,
            "rules": [],
            "parameters": shown_rows,
            "page": page,
        }
    chain = _chain(instance, "/instance")
    names_given = [_rule_name(n, f"/rules/{i}") for i, n in enumerate(rules or [])]
    with tx.checks():
        names, klass = rule_names(rm, chain)
    formulas: dict[str, str | None] = {}
    if klass:
        formulas = specified(rm, chain)
        if not internal and not names_given:
            wanted = {n.casefold() for n in class_parameters(rm, klass)}
            first = [n for n in names if n.casefold() in {f.casefold() for f in formulas}]
            names = first + [n for n in names if n.casefold() in wanted and n not in first]
    if names_given:
        names = names_given
    shown, page = result.page(names, offset, limit)
    expressions = _expression_rules(rm, part, shown) if chain == ROOT else set()
    out_rows = []
    for name in shown:
        formula = None
        if chain == ROOT and name not in expressions:
            formula = dynamic_formula(rm, name)
        elif chain == ROOT:
            formula = _expression_formula(part, name)
        else:
            formula = next((f for n, f in formulas.items() if n.casefold() == name.casefold()),
                           None)  # fmt: skip
        out_rows.append(row(rm, chain, name, expressions=expressions, formula=formula))
    return {
        "part": tx.ref(part, "part"),
        "instance": chain,
        "class_name": klass,
        "rules": out_rows,
        "page": page,
    }


def _expression_formula(part, name: str) -> str | None:
    with contextlib.suppress(Exception):
        for e in part.Expressions:
            if str(e.Name) == name:
                return str(e.RightHandSide)
    return None


def _class_name(text) -> str:
    klass = str(text or "").strip()
    if not CLASS.fullmatch(klass):
        fail("NX_INVALID_ARGUMENT", f"{text!r} is not a KF class name", field="/class_name")
    return klass


def _require_class(rm, klass: str) -> list[str]:
    """The class's rules; ``NX_NOT_FOUND`` when NX's KF class directories lack it."""
    try:
        names = [str(n) for n in rm.GetRulesOfClass(klass)]
    except Exception as error:
        code = getattr(error, "ErrorCode", None)
        fail(
            "NX_NOT_FOUND",
            f"NX's Knowledge Fusion cannot load the class {klass}: {_message(rm, error)}",
            field="/class_name",
            hint="Use a class from NX's KF class directories, e.g. ug_expression or ug_block",
            nx_code=int(code) if isinstance(code, int) else None,
        )
    if not names:
        fail("NX_NOT_FOUND", f"NX's Knowledge Fusion has no class {klass}", field="/class_name",
             hint="Use a class from NX's KF class directories, e.g. ug_expression or ug_block")  # fmt: skip
    return names


@tool(
    effect="model",
    label="Set KF rule",
    summary={
        "child": "Add KF child rule {name} of class {class_name}",
        "dynamic": "Set KF rule {name} = {formula}",
    },
    keywords=("knowledge fusion", "kf rule", "child rule", "dynamic rule", "kf class", "dfa"),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={"child": ("class_name",), "dynamic": ("formula",)},
    cost="heavy",
    example={
        "kind": "child",
        "name": "size",
        "class_name": "ug_expression",
        "parameters": [{"name": "Value", "value": 25}],
    },  # fmt: skip
)
def nx_set_kf_rule(
    tx: Tx,
    *,
    kind: Annotated[
        Literal["child", "dynamic"],
        Doc("child: an instance of a KF class; dynamic: one typed rule with a formula"),
    ],
    name: Annotated[str, Text(64), Doc("Rule name in root: (no ':'); an existing one is changed")],
    class_name: Annotated[
        str, Text(65), Doc("child: a class NX's KF class directories hold, e.g. ug_expression")
    ]
    | None = None,
    parameters: Annotated[
        list[Parameter],
        Items(1, 30),
        Doc("child: parameters to set; an existing child keeps the others"),
    ]
    | None = None,
    replace: Annotated[
        bool, Doc("child: reset the parameters not given to the class defaults")
    ] = False,
    formula: Annotated[Formula, Doc("dynamic: KF formula, e.g. 'p0: * 2', '\"text\"'")]
    | None = None,
    rule_type: Annotated[
        Literal[
            "number", "integer", "boolean", "string", "list", "point", "vector", "frame", "any"
        ],
        Doc("dynamic: the rule's data type"),
    ] = "number",  # fmt: skip
) -> SetRuleData:
    """Add or change a Knowledge Fusion rule in the root instance of the work part. The KF
    update that follows builds what a child's class makes (ug_expression: a unitless
    expression). child: a class instance with parameter values in KF syntax; changing a
    child keeps the parameters not given. dynamic: a typed formula rule. A class's
    parameters: nx_get_kf_rules class_name; expressions: nx_set_expression."""
    rule = _rule_name(name, "/name")
    klass, text = "", ""
    params: list[tuple[str, str]] = []
    if kind == "child":
        klass = _class_name(class_name)
        seen: set[str] = set()
        for i, item in enumerate(parameters or []):
            pname = _rule_name(str(item["name"]), f"/parameters/{i}/name")
            if pname.casefold() in seen:
                fail("NX_INVALID_ARGUMENT", f"Parameter {pname} is listed twice",
                     field=f"/parameters/{i}/name")  # fmt: skip
            seen.add(pname.casefold())
            params.append((pname, _formula(item["value"], f"/parameters/{i}/value")))
    else:
        if parameters:
            fail("NX_INVALID_ARGUMENT", "parameters belong to kind child", field="/parameters")
        if replace:
            fail("NX_INVALID_ARGUMENT", "replace belongs to kind child", field="/replace")
        text = _formula(formula, "/formula")
    part = tx.part
    kept: list[str] = []
    with tx.checks():
        licenses.require(tx, LICENSES, "Knowledge Fusion rules need a Knowledge Fusion license")
        rm = manager(tx, part)
        existing = dynamic_rules(rm, ROOT, None)
        present = next((n for n in existing if n.casefold() == rule.casefold()), None)
        if present is not None:
            rule = present
            _check_existing(rm, part, rule, kind, klass if kind == "child" else None)
        if kind == "child":
            _require_class(rm, klass)
            _check_parameters(rm, klass, params)
            if present is not None and not replace:
                given = {n.casefold() for n, _ in params}
                for pname, ptext in specified(rm, f"{ROOT}{rule}:").items():
                    if pname.casefold() not in given and ptext is not None:
                        params.append((pname, ptext))
                        kept.append(pname)
    values_before = _expression_values(part)
    with native(codes={BAD_ARGUMENT: "NX_INVALID_ARGUMENT"}, default="NX_ERROR"):
        try:
            if kind == "child":
                values = [tx.nx.RuleManager.ParameterRule(n, v) for n, v in params]
                rm.CreateChildRule(rule, klass, values)
            else:
                kinds = tx.nx.RuleManager.RuleType
                rm.CreateDynamicRule(ROOT, rule, getattr(kinds, RULE_TYPES[rule_type]), text)
        except NXError as error:
            raise _refused(rm, error, kind, params) from None
        except Exception as error:
            raise _refused(rm, error, kind, params) from None
    try:
        _kf_update(tx, rm)
    except NXError as error:
        raise _refused(rm, error, kind, params) from None
    readback.expect(
        any(n.casefold() == rule.casefold() for n in dynamic_rules(rm, ROOT, None)),
        "rule", f"{rule} is not a rule of root: after the update",
    )  # fmt: skip
    _report_expressions(tx, part, values_before)
    expressions = _expression_rules(rm, part, [rule])
    formula_now = dynamic_formula(rm, rule) if kind == "dynamic" else None
    out = row(rm, ROOT, rule, expressions=expressions, formula=formula_now)
    if out["error"]:
        fail("NX_INVALID_FORMULA" if kind == "dynamic" else "NX_INVALID_ARGUMENT",
             f"{rule} does not evaluate: {out['error']}",
             field="/formula" if kind == "dynamic" else "/parameters", hint=FORMULA_HINT)  # fmt: skip
    children: list[RuleRow] = []
    if kind == "child":
        chain = f"{ROOT}{rule}:"
        formulas = specified(rm, chain)
        wanted = [n for n, _ in params]
        taken = {w.casefold() for w in wanted}
        others = [n for n in class_parameters(rm, klass) if n.casefold() not in taken]
        for n in (wanted + others)[:LIST_MAX]:
            known = next((f for k, f in formulas.items() if k.casefold() == n.casefold()), None)
            children.append(row(rm, chain, n, formula=known))
        bad = [r for r in children if r["name"] in wanted and r["error"]]
        if bad:
            fail("NX_INVALID_ARGUMENT", f"Parameter {bad[0]['name']} does not evaluate: "
                 f"{bad[0]['error']}", field=f"/parameters/{wanted.index(bad[0]['name'])}/value",
                 hint=FORMULA_HINT)  # fmt: skip
    data: SetRuleData = {
        "part": tx.ref(part, "part"),
        "rule": out,
        "created": present is None,
        "rules": children,
    }
    if kept:
        data["kept_parameters"] = kept
    return data


def _check_parameters(rm, klass: str, params: list[tuple[str, str]]) -> None:
    """Refuse a parameter the class lacks before NX (``IsParameter`` ignores case), with
    the class's parameters and the closest names."""
    for i, (pname, _value) in enumerate(params):
        try:
            known = bool(rm.IsParameter(klass, pname))
        except Exception:
            known = False
        if known:
            continue
        valid = class_parameters(rm, klass)
        close = difflib.get_close_matches(
            pname.casefold(), [v.casefold() for v in valid], n=3, cutoff=0.6
        )
        named = [v for v in valid if v.casefold() in close]
        fail(
            "NX_INVALID_ARGUMENT",
            f"The KF class {klass} has no parameter {pname}"
            + (f"; did you mean {', '.join(named)}?" if named else ""),
            field=f"/parameters/{i}/name",
            hint="details.valid lists the class's parameters (nx_get_kf_rules class_name too)",
            details={"valid": valid[:LIST_MAX], "suggestions": named},
        )


def _expression_values(part) -> dict[str, tuple[object, float | None]]:
    out: dict[str, tuple[object, float | None]] = {}
    with contextlib.suppress(Exception):
        for e in part.Expressions:
            value = None
            with contextlib.suppress(Exception):
                value = float(e.Value)
            out[str(e.Name)] = (e, value)
    return out


def _report_expressions(tx, part, before) -> None:
    """List the expressions whose value the KF update changed (NX does not report them):
    ``changes.modified``."""
    for name, (exp, value) in _expression_values(part).items():
        old = before.get(name)
        if old is None or old[1] is None or value is None:
            continue
        if abs(value - old[1]) > 1e-9 * max(1.0, abs(old[1])):
            with contextlib.suppress(Exception):
                tx.modified(exp, "expression")


@tool(
    effect="model",
    label="Delete KF rule",
    summary="Delete KF rule {name}",
    keywords=("knowledge fusion", "delete kf rule", "remove rule", "child rule", "dynamic rule"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    example={"name": "size"},
)
def nx_delete_kf_rule(
    tx: Tx,
    *,
    name: Annotated[str, Text(64), Doc("Child or dynamic rule of root: (no ':')")],
) -> DeleteRuleData:
    """Delete a child or dynamic Knowledge Fusion rule of the work part's root instance. The
    KF update then removes what a child made (its expression or body). The part's NX
    expressions are not deleted here: use nx_delete_objects."""
    rule = _rule_name(name, "/name")
    part = tx.part
    with tx.checks():
        licenses.require(tx, LICENSES, "Knowledge Fusion rules need a Knowledge Fusion license")
        rm = manager(tx, part)
        existing = dynamic_rules(rm, ROOT, None)
        present = next((n for n in existing if n.casefold() == rule.casefold()), None)
        if present is None:
            fail("NX_NOT_FOUND", f"root: has no rule {rule}", field="/name",
                 hint="List the rules with nx_get_kf_rules",
                 details={"rules": existing[:LIST_MAX]})  # fmt: skip
        klass = _child_class(rm, f"{ROOT}{present}:")
        if klass is None and present in _expression_rules(rm, part, [present]):
            fail("NX_WRONG_KIND", f"{present} is an NX expression of the part, not a KF rule",
                 field="/name", hint="Delete expressions with nx_delete_objects")  # fmt: skip
    values_before = _expression_values(part)
    with native(default="NX_ERROR"):
        if klass:
            rm.DeleteChildRule(present)
        else:
            rm.DeleteDynamicRule(ROOT, present)
    _kf_update(tx, rm)
    left = [n.casefold() for n in dynamic_rules(rm, ROOT, None)]
    readback.expect(present.casefold() not in left, "deleted", f"{present} is still a rule")
    _report_expressions(tx, part, values_before)
    return {
        "part": tx.ref(part, "part"),
        "name": present,
        "kind": "child" if klass else "dynamic",
        "class_name": klass,
    }


def _check_existing(rm, part, rule: str, kind: str, klass: str | None) -> None:
    current = _child_class(rm, f"{ROOT}{rule}:")
    if current is None and rule in _expression_rules(rm, part, [rule]):
        fail("NX_ALREADY_EXISTS", f"{rule} is an NX expression of the part", field="/name",
             hint="Change it with nx_set_expression, or choose another name")  # fmt: skip
    if kind == "dynamic" and current:
        fail("NX_ALREADY_EXISTS", f"{rule} is a child rule of class {current}", field="/name",
             hint="Use kind child to change it, or choose another name")  # fmt: skip
    if kind == "child" and current is None:
        fail("NX_ALREADY_EXISTS", f"{rule} is a dynamic rule, not a child rule", field="/name",
             hint="Use kind dynamic to change it, or choose another name")  # fmt: skip
    if kind == "child" and klass and current and current.casefold() != klass.casefold():
        fail("NX_ALREADY_EXISTS", f"{rule} is a child rule of class {current}, not {klass}",
             field="/class_name", hint="Choose another name for the new child rule")  # fmt: skip


def _refused(rm, error: Exception, kind: str, params=()) -> NXError:
    message = _message(rm, error)
    code = getattr(error, "nx_code", None) or getattr(error, "ErrorCode", None)
    if code == SYNTAX_ERROR and kind == "child":
        index = next((i for i, (n, _v) in enumerate(params)
                      if re.search(rf"\b{re.escape(n)}\b", message, re.IGNORECASE)), None)  # fmt: skip
        return NXError(
            "NX_INVALID_FORMULA",
            f"A parameter value is not KF syntax: {message}",
            field=f"/parameters/{index}/value" if index is not None else "/parameters",
            hint="Values are KF formulas: 10 (not '10 mm'), \"text\", Point(0,50,0); "
            + FORMULA_HINT,
            nx_code=SYNTAX_ERROR,
        )
    if isinstance(error, NXError) and error.code == "NX_UPDATE_FAILED":
        return error
    if (
        getattr(error, "nx_code", None) == BAD_ARGUMENT
        or getattr(error, "ErrorCode", None) == BAD_ARGUMENT
    ):
        if kind == "child":
            return NXError(
                "NX_NOT_FOUND",
                f"NX's Knowledge Fusion cannot load this class: {message}",
                field="/class_name",
                hint="Use a class from NX's KF class directories, e.g. ug_expression or ug_block",
                nx_code=BAD_ARGUMENT,
            )
        return NXError("NX_INVALID_FORMULA", message, field="/formula", hint=FORMULA_HINT,
                       nx_code=BAD_ARGUMENT)  # fmt: skip
    if isinstance(error, NXError):
        return error
    code = getattr(error, "ErrorCode", None)
    return NXError(
        "NX_INVALID_FORMULA" if kind == "dynamic" else "NX_ERROR",
        message,
        field="/formula" if kind == "dynamic" else None,
        hint=FORMULA_HINT if kind == "dynamic" else None,
        nx_code=int(code) if isinstance(code, int) else None,
    )


def _kf_update(tx, rm) -> None:
    """``RuleManager.DoKfUpdate`` under the call's mark (NX rolls back to it on errors)."""
    with native(default="NX_UPDATE_FAILED"):
        count = rm.DoKfUpdate(tx.mark)
    if count:
        detail = _last_error(rm)
        fail("NX_UPDATE_FAILED",
             f"The Knowledge Fusion update reported {count} error(s)"
             + (f": {detail}" if detail else ""),
             hint="Check the rule values with nx_get_kf_rules")  # fmt: skip


@tool(
    effect="model",
    label="Update KF rules",
    summary="Regenerate the Knowledge Fusion rules of the work part",
    keywords=("knowledge fusion", "kf rule", "regenerate", "kf update", "rule update"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
)
def nx_update_kf_rules(tx: Tx) -> UpdateData:
    """Regenerate every Knowledge Fusion rule of the work part and list the root rules that
    no longer evaluate. Not needed after nx_set_expression or nx_set_kf_rule (NX updates KF
    values then); use it after reloading the part or changing KF classes."""
    part = tx.part
    with tx.checks():
        licenses.require(tx, LICENSES, "Knowledge Fusion rules need a Knowledge Fusion license")
        rm = manager(tx, part)
        names = dynamic_rules(rm, ROOT, None)
    with native(default="NX_ERROR"):
        rm.RegenerateAll()
    _kf_update(tx, rm)
    rows = [row(rm, ROOT, n) for n in names[:200]]
    return {
        "part": tx.ref(part, "part"),
        "rules": len(names),
        "failed": [r for r in rows if r["error"]][:LIST_MAX],
    }
