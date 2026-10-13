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

Not offered (NXOpen 2406): Product Template Studio templates whose data predates the
NX 2007 format answer ``ConfigurationManager.GetRootNode()`` with null, and NX's upgrade
call for that older data needs a node, so their user parameters are reachable only as
the part's expressions (nx_open_part + nx_set_expression). ``Session.ExecuteGrip`` runs a
compiled GRIP program, but without a GRIP compiler no program could be verified headless.
"""

from __future__ import annotations

import contextlib
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
NO_INSTANCE, BAD_ARGUMENT = 3270054, 3520002
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
        Doc("A number, true/false or a KF formula (strings need double quotes)"),
    ]


class RuleRow(TypedDict):
    name: str
    chain: Annotated[str, Doc("Reference chain to evaluate it again")]
    type: Annotated[str, Doc("number, integer, boolean, string, list, instance or other")]
    value: Annotated[
        float | int | bool | str | list | None, Doc("null: an object or nested list (see text)")
    ]
    text: Annotated[str | None, Doc("NX's text of the value")]
    expression: Annotated[bool, Doc("The rule is an NX expression of the part")]
    child: Annotated[str | None, Doc("Class of a child rule (an instance), else null")]
    error: Annotated[str | None, Doc("Why NX could not evaluate it")]


class RulesData(TypedDict):
    part: result.Row
    instance: str
    class_name: Annotated[str | None, Doc("Class of the instance (null for root:)")]
    rules: list[RuleRow]
    page: Annotated[dict, Doc("offset, limit, returned, total, next_offset")]


class SetRuleData(TypedDict):
    part: result.Row
    rule: RuleRow
    created: Annotated[bool, Doc("false: an existing rule was changed")]
    rules: Annotated[list[RuleRow], Doc("A child's rules (its parameters first), up to 50")]


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


def _child_class(rm, chain: str) -> str | None:
    try:
        return str(rm.GetClass(chain)) or None
    except Exception:
        return None


def row(rm, instance: str, name: str, *, expressions=None) -> RuleRow:
    """Evaluate ``instance`` + ``name`` into a result row (errors kept in the row)."""
    chain = f"{instance}{name}:"
    out: RuleRow = {
        "name": name, "chain": chain, "type": "other", "value": None, "text": None,
        "expression": bool(expressions and name in expressions), "child": None, "error": None,
    }  # fmt: skip
    try:
        raw = rm.Evaluate(chain)
    except Exception as error:
        out["error"] = _message(rm, error)
        return out
    out["value"], out["type"] = _plain(raw)
    with contextlib.suppress(Exception):
        out["text"] = str(rm.EvaluateAnyAsString(chain))[:500]
    if out["type"] == "instance":
        out["child"] = _child_class(rm, chain)
        out["expression"] = False
    if isinstance(out["value"], str) and len(out["value"]) > 500:
        out["value"] = out["value"][:500]
    return out


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
    summary="Get the Knowledge Fusion rules of {instance}",
    keywords=("knowledge fusion", "kf rule", "dfa", "rule value", "child rule", "evaluate"),
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
    offset: Offset = 0,
    limit: PageLimit = 50,
) -> RulesData:
    """Evaluate the Knowledge Fusion rules of a KF instance of the work part. root: holds
    the part's NX expressions and child rules; each row gives value, type and NX's text. A
    child rule's row names its class: pass its chain as instance to read its rules. A rule
    NX cannot evaluate reports error in its row. Add rules with nx_set_kf_rule."""
    chain = _chain(instance, "/instance")
    names_given = [_rule_name(n, f"/rules/{i}") for i, n in enumerate(rules or [])]
    part = tx.part
    rm = manager(tx, part)
    with tx.checks():
        names, klass = rule_names(rm, chain)
    if names_given:
        names = names_given
    shown, page = result.page(names, offset, limit)
    expressions = _expression_rules(rm, part, shown) if chain == ROOT else set()
    return {
        "part": tx.ref(part, "part"),
        "instance": chain,
        "class_name": klass,
        "rules": [row(rm, chain, n, expressions=expressions) for n in shown],
        "page": page,
    }


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
        list[Parameter], Items(1, 30), Doc("child: class parameters to set (others keep defaults)")
    ]
    | None = None,
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
    update that follows builds what a child's class makes (ug_expression: an expression).
    child: an instance of a class with parameter values (only classes in NX's KF class
    directories load, not a .dfa elsewhere); dynamic: a typed formula rule. Expressions:
    use nx_set_expression. Read values with nx_get_kf_rules."""
    rule = _rule_name(name, "/name")
    klass, text = "", ""
    params: list[tuple[str, str]] = []
    if kind == "child":
        klass = str(class_name or "").strip()
        if not CLASS.fullmatch(klass):
            fail("NX_INVALID_ARGUMENT", f"{class_name!r} is not a KF class name",
                 field="/class_name")  # fmt: skip
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
        text = _formula(formula, "/formula")
    part = tx.part
    with tx.checks():
        licenses.require(tx, LICENSES, "Knowledge Fusion rules need a Knowledge Fusion license")
        rm = manager(tx, part)
        existing = dynamic_rules(rm, ROOT, None)
        present = next((n for n in existing if n.casefold() == rule.casefold()), None)
        if present is not None:
            rule = present
            _check_existing(rm, part, rule, kind, klass if kind == "child" else None)
    with native(codes={BAD_ARGUMENT: "NX_INVALID_ARGUMENT"}, default="NX_ERROR"):
        try:
            if kind == "child":
                values = [tx.nx.RuleManager.ParameterRule(n, v) for n, v in params]
                rm.CreateChildRule(rule, klass, values)
            else:
                kinds = tx.nx.RuleManager.RuleType
                rm.CreateDynamicRule(ROOT, rule, getattr(kinds, RULE_TYPES[rule_type]), text)
        except NXError as error:
            raise _refused(rm, error, kind) from None
        except Exception as error:
            raise _refused(rm, error, kind) from None
    _kf_update(tx, rm)
    readback.expect(
        any(n.casefold() == rule.casefold() for n in dynamic_rules(rm, ROOT, None)),
        "rule", f"{rule} is not a rule of root: after the update",
    )  # fmt: skip
    expressions = _expression_rules(rm, part, [rule])
    out = row(rm, ROOT, rule, expressions=expressions)
    if out["error"]:
        fail("NX_INVALID_FORMULA" if kind == "dynamic" else "NX_INVALID_ARGUMENT",
             f"{rule} does not evaluate: {out['error']}",
             field="/formula" if kind == "dynamic" else "/parameters", hint=FORMULA_HINT)  # fmt: skip
    children: list[RuleRow] = []
    if kind == "child":
        chain = f"{ROOT}{rule}:"
        names, _klass = rule_names(rm, chain)
        wanted = [n for n, _ in params]
        ordered = wanted + [n for n in names if n not in wanted]
        children = [row(rm, chain, n) for n in ordered[:LIST_MAX]]
        bad = [r for r in children if r["name"] in wanted and r["error"]]
        if bad:
            fail("NX_INVALID_ARGUMENT", f"Parameter {bad[0]['name']} does not evaluate: "
                 f"{bad[0]['error']}", field=f"/parameters/{wanted.index(bad[0]['name'])}/value",
                 hint=FORMULA_HINT)  # fmt: skip
    return {
        "part": tx.ref(part, "part"),
        "rule": out,
        "created": present is None,
        "rules": children,
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


def _refused(rm, error: Exception, kind: str) -> NXError:
    message = _message(rm, error)
    if (
        getattr(error, "nx_code", None) == BAD_ARGUMENT
        or getattr(error, "ErrorCode", None) == BAD_ARGUMENT
    ):
        if kind == "child":
            return NXError(
                "NX_NOT_FOUND",
                f"NX's Knowledge Fusion cannot load this class: {message}",
                field="/class_name",
                hint="Use a class from NX's KF class directories (the KF navigator lists them)",
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
        fail("NX_UPDATE_FAILED", f"The Knowledge Fusion update reported {count} error(s)",
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
    """Regenerate every Knowledge Fusion rule of the work part and run the KF update. Values
    and the NX objects child rules make then follow edited expressions and parameters.
    Lists root rules that do not evaluate afterwards. Read values with nx_get_kf_rules."""
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
