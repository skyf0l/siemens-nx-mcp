"""nx_get_kf_rules, nx_set_kf_rule and nx_update_kf_rules on a fake RuleManager."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import evidence
from nx_mcp.domains.parts_files import rules
from tests.kit import NXException
from tests.kit.objects import Object
from tests.unit.parts_files.support import call

CLASSES = {
    "ug_expression": {"Value": "0", "Comment": '""'},
    "plate": {"length": "100", "width": "40"},
}


def _number(text):
    try:
        return float(text)
    except ValueError:
        return None


class RuleManager:
    """NX 2406 behaviour: expressions are root rules, children need a known class, the
    KF update makes a ug_expression child's expression."""

    def __init__(self, nx):
        self.nx, self.part = nx, nx.part
        self.children: dict[str, tuple[str, dict]] = {}
        self.specified: dict[str, list[str]] = {}  # the parameters a child sets
        self.dynamic: dict[str, tuple[str, str]] = {}
        self.updates, self.regenerated, self.update_errors = 0, 0, 0
        self.error = ""

    def _expressions(self):
        return {str(e.Name): e for e in self.part.Expressions}

    def _fail(self, code, message, detail=""):
        self.error = detail
        raise NXException(code, message)

    def GetDynamicRules(self, chain):
        if chain.lower() == "root:":
            return [*self._expressions(), *self.dynamic, *self.children]
        return []  # NX 2406: no error for an instance that does not exist

    def GetClass(self, chain):
        name = chain[5:-1]
        if name not in self.children:
            self._fail(3520002, "Second parameter is invalid")
        return self.children[name][0]

    def GetRulesOfClass(self, klass):
        if klass not in CLASSES:
            self._fail(3520002, "Second parameter is invalid")
        return ["onCreate", "inTree?", *CLASSES[klass], "volume"]  # base, parameters, computed

    def IsParameter(self, klass, name):
        if name == "volume":
            return False
        if name in ("onCreate", "inTree?"):
            return True  # NX lists the KF base parameters of every class
        if all(name.casefold() != p.casefold() for p in CLASSES[klass]):
            self._fail(1, "No such rule")
        return True

    def GetDefaultFormula(self, klass, name):
        return CLASSES[klass][name] + ";"

    def _canonical(self, klass, name):
        return next((p for p in CLASSES[klass] if p.casefold() == name.casefold()), name)

    def GetSpecifiedParameters(self, chain):
        name = chain[5:-1]
        return ["Class", *self.specified.get(name, [])]

    def GetParameterFormula(self, chain, name):
        child = chain[5:-1]
        return self.children[child][1][name] + ";"

    def GetDynamicRuleText(self, chain):
        name = chain[5:-1]
        if name not in self.dynamic:
            self._fail(1, "No such rule")
        kind, formula = self.dynamic[name]
        return f"({kind}) {name}: {formula}; "

    def GetRuleType(self, chain):
        name = chain[5:-1]
        # NX 2406: an enum member with ``value`` (``int()`` of it raises), not an int.
        return NS(value=6 if self.dynamic.get(name, ("",))[0] == "Point" else 3)

    def DeleteChildRule(self, name):
        del self.children[name]
        self.specified.pop(name, None)

    def DeleteDynamicRule(self, chain, name):
        assert chain == "root:"
        del self.dynamic[name]

    def IsExpressionRule(self, name):
        return name in self._expressions()

    def GetLastError(self):
        return [self.error] if self.error else []

    def Evaluate(self, chain):
        parts = chain[5:-1].split(":")
        if len(parts) == 1:
            (name,) = parts
            if name in self.children:
                return Object(name)
            if name in self._expressions():
                return float(self._expressions()[name].Value)
            if name in self.dynamic:
                kind, formula = self.dynamic[name]
                if kind == "String":
                    return formula.strip('"')
                if kind == "Boolean":
                    return formula == "true"
                if kind == "List":
                    return [1.0, 2.0]
                if kind == "Point":
                    return [0.0, 50.0, 0.0]
                if formula.endswith(": * 2"):
                    return 2 * self.Evaluate(f"root:{formula.split(':')[0]}:")
                value = _number(formula)
                if value is None:
                    self._fail(1, "No such rule", f"Cannot find rule '{formula}'")
                return value
        else:
            child, rule = parts
            if child not in self.children:
                self._fail(3270054, "Referenced Instance does not exist")
            klass, values = self.children[child]
            rule = next((k for k in values if k.casefold() == rule.casefold()), rule)
            if rule in values:
                value = _number(values[rule])
                if value is None and not values[rule].startswith('"'):
                    self._fail(1, "No such rule", f"Cannot find rule '{values[rule]}'")
                return value if value is not None else values[rule].strip('"')
        self._fail(1, "No such rule", f"Cannot find rule '{parts[-1]}'")

    def EvaluateAnyAsString(self, chain):
        value = self.Evaluate(chain)
        if isinstance(value, Object):
            name = chain[5:-1]
            return f"{name} ({self.children[name][0]})"
        return str(value)

    def CreateChildRule(self, name, klass, params):
        """NX 2406: the parameter list replaces the child's; the others are the defaults."""
        if klass not in CLASSES:
            raise NXException(3520002, "Second parameter is invalid")
        values = dict(CLASSES[klass])
        for key, value in params:
            if value == "10 mm":
                self._fail(3270008, "Syntax error", f"Syntax error in rule {key}")
            values[self._canonical(klass, key)] = value
        self.children[name] = (klass, values)
        self.specified[name] = [self._canonical(klass, key) for key, _ in params]

    def CreateDynamicRule(self, chain, name, kind, formula):
        assert chain == "root:"
        if formula == "(":
            raise NXException(3520002, "Second parameter is invalid")
        self.dynamic[name] = (kind, formula)

    def DoKfUpdate(self, mark):
        assert mark is not None
        self.updates += 1
        for name, (klass, values) in self.children.items():
            if klass == "ug_expression" and name not in self._expressions():
                self.nx.expression(name, values["Value"], float(values["Value"]))
            elif klass == "ug_expression":  # NX changes the expression without a note
                self._expressions()[name].Value = float(values["Value"])
        return self.update_errors

    def RegenerateAll(self):
        if self.nx.refused:  # NX's own license check of Knowledge Fusion
            raise NXException(948801, "License Error, its token feature does not exist.")
        self.regenerated += 1


@pytest.fixture
def kf(nx):
    nx.expression("p0", "40", 40.0)
    nx.expression("p1", "20", 20.0)
    rm = RuleManager(nx)
    nx.part.RuleManager = rm
    nx.nx.RuleManager = NS(
        ParameterRule=lambda name, value: (name, value),
        RuleType=NS(**{v: v for v in rules.RULE_TYPES.values()}),
    )
    nx.licensed, nx.refused = True, False
    nx.session.LicenseManager = NS(CheckPresence=lambda name: nx.licensed)
    evidence.forget_licenses()
    yield rm
    evidence.forget_licenses()


def get(nx, **args):
    return call(nx, rules.nx_get_kf_rules, **args)


def kfset(nx, **args):
    return call(nx, rules.nx_set_kf_rule, **args)


def test_lists_root_rules_with_values_and_expressions(nx, kf):
    kf.children["holder"] = ("plate", dict(CLASSES["plate"]))
    out = get(nx)
    assert out.ok, out
    data = out.data
    assert data["instance"] == "root:" and data["class_name"] is None
    by = {r["name"]: r for r in data["rules"]}
    assert by["p0"]["value"] == 40.0 and by["p0"]["type"] == "number"
    assert by["p0"]["expression"] is True and by["p0"]["chain"] == "root:p0:"
    assert by["holder"]["type"] == "instance" and by["holder"]["child"] == "plate"
    assert by["holder"]["value"] is None and by["holder"]["text"] == "holder (plate)"
    assert out.page["total"] == 3


def test_reads_a_child_instance_and_named_rules(nx, kf):
    kf.children["holder"] = ("plate", {"length": "120", "width": "40"})
    out = get(nx, instance="holder", rules=["length", "missing"])
    assert out.ok, out
    data = out.data
    assert data["instance"] == "root:holder:" and data["class_name"] == "plate"
    assert data["rules"][0]["value"] == 120.0 and data["rules"][0]["error"] is None
    assert "No such rule" in data["rules"][1]["error"]
    assert "missing" in data["rules"][1]["error"]


def test_get_refuses_bad_chains_and_unknown_instances(nx, kf):
    out = get(nx, instance="root:a b:")
    assert out.error["code"] == "NX_INVALID_ARGUMENT" and out.error["field"] == "/instance"
    out = get(nx, rules=["p0:x"])
    assert out.error["field"] == "/rules/0"
    out = get(nx, instance="root:nothing:")
    assert out.error["code"] == "NX_NOT_FOUND" and out.error["field"] == "/instance"


def test_get_pages(nx, kf):
    out = get(nx, limit=1, offset=1)
    assert [r["name"] for r in out.data["rules"]] == ["p1"]
    assert out.page["next_offset"] is None and out.page["total"] == 2


def test_child_rule_runs_the_kf_update(nx, kf):
    out = kfset(nx, kind="child", name="size", class_name="ug_expression",
                parameters=[{"name": "Value", "value": 25}])  # fmt: skip
    assert out.ok, out
    assert out.mutation_outcome == "committed"
    data = out.data
    assert data["created"] is True and data["rule"]["child"] == "ug_expression"
    assert data["rules"][0] == {**data["rules"][0], "name": "Value", "value": 25.0}
    assert kf.updates == 1
    assert any(e.Name == "size" for e in nx.part.Expressions)


def test_child_rule_changes_an_existing_child(nx, kf):
    kfset(nx, kind="child", name="holder", class_name="plate")
    out = kfset(nx, kind="child", name="holder", class_name="plate",
                parameters=[{"name": "width", "value": "55"}])  # fmt: skip
    assert out.ok, out
    assert out.data["created"] is False
    values = {r["name"]: r["value"] for r in out.data["rules"]}
    assert values == {"width": 55.0, "length": 100.0}


def test_changing_one_parameter_of_a_child_keeps_the_others(nx, kf):
    """NX resets every parameter a CreateChildRule does not list; the tool merges."""
    kfset(nx, kind="child", name="holder", class_name="plate",
          parameters=[{"name": "length", "value": 15}, {"name": "width", "value": 25}])  # fmt: skip
    out = kfset(nx, kind="child", name="holder", class_name="plate",
                parameters=[{"name": "Width", "value": 9}])  # fmt: skip
    assert out.ok, out
    assert kf.children["holder"][1] == {"length": "15", "width": "9"}
    assert out.data["kept_parameters"] == ["length"]
    rows = {r["name"]: r for r in out.data["rules"]}
    assert rows["length"]["formula"] == "15" and rows["Width"]["value"] == 9.0
    assert "onCreate" not in rows and "volume" not in rows  # base and computed rules
    reset = kfset(nx, kind="child", name="holder", class_name="plate", replace=True,
                  parameters=[{"name": "width", "value": 7}])  # fmt: skip
    assert reset.ok and kf.children["holder"][1] == {"length": "100", "width": "7"}
    assert "kept_parameters" not in reset.data


def test_a_misspelled_parameter_is_refused_with_the_class_parameters(nx, kf):
    out = kfset(nx, kind="child", name="rib", class_name="ug_expression",
                parameters=[{"name": "Valeu", "value": 4}])  # fmt: skip
    assert out.error["code"] == "NX_INVALID_ARGUMENT", out
    assert out.error["field"] == "/parameters/0/name"
    assert out.error["details"]["valid"] == ["Value", "Comment"]
    assert out.error["details"]["suggestions"] == ["Value"] and "Value" in out.error["message"]
    assert out.mutation_outcome == "not_started" and kf.children == {}


def test_a_value_with_units_is_a_formula_error_on_its_parameter(nx, kf):
    out = kfset(nx, kind="child", name="rib", class_name="ug_expression",
                parameters=[{"name": "Comment", "value": '"x"'},
                            {"name": "Value", "value": "10 mm"}])  # fmt: skip
    assert out.error["code"] == "NX_INVALID_FORMULA", out
    assert out.error["field"] == "/parameters/1/value" and out.error["nx_code"] == 3270008
    assert "10 mm" in out.error["hint"]


def test_kf_driven_expression_changes_are_reported(nx, kf):
    kfset(nx, kind="child", name="size", class_name="ug_expression",
          parameters=[{"name": "Value", "value": 25}])  # fmt: skip
    out = kfset(nx, kind="child", name="size", class_name="ug_expression",
                parameters=[{"name": "Value", "value": 30}])  # fmt: skip
    assert out.ok, out
    assert [r["name"] for r in out.changes["modified"]] == ["size"]


def test_class_parameters_formulas_and_delete(nx, kf):
    described = get(nx, class_name="plate")
    assert described.ok, described
    assert described.data["parameters"] == [
        {"name": "length", "default": "100"}, {"name": "width", "default": "40"}
    ]  # fmt: skip
    unknown = get(nx, class_name="nothing")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.error["field"] == "/class_name"
    kfset(nx, kind="dynamic", name="twice", formula="p0: * 2")
    point = kfset(nx, kind="dynamic", name="pt", formula="Point(0,50,0)", rule_type="point")
    assert point.ok and point.data["rule"]["type"] == "point", point
    assert point.data["rule"]["value"] == [0.0, 50.0, 0.0]
    rows = {r["name"]: r for r in get(nx).data["rules"]}
    assert rows["twice"]["formula"] == "p0: * 2" and rows["p0"]["formula"] == "40"
    assert rows["pt"]["formula"] == "Point(0,50,0)"
    gone = call(nx, rules.nx_delete_kf_rule, name="twice")
    assert gone.ok and gone.data["kind"] == "dynamic" and "twice" not in kf.dynamic, gone
    kfset(nx, kind="child", name="holder", class_name="plate")
    child = call(nx, rules.nx_delete_kf_rule, name="HOLDER")
    assert child.ok and child.data == {**child.data, "name": "holder", "kind": "child",
                                       "class_name": "plate"}  # fmt: skip
    assert "holder" not in kf.children
    missing = call(nx, rules.nx_delete_kf_rule, name="nope")
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.mutation_outcome == "not_started"
    expression = call(nx, rules.nx_delete_kf_rule, name="p0")
    assert expression.error["code"] == "NX_WRONG_KIND"


def test_dynamic_rules(nx, kf):
    out = kfset(nx, kind="dynamic", name="twice", formula="p0: * 2")
    assert out.ok, out
    assert out.data["rule"]["value"] == 80.0 and out.data["rules"] == []
    out = kfset(nx, kind="dynamic", name="label", formula='"plate"', rule_type="string")
    assert out.data["rule"] == {**out.data["rule"], "value": "plate", "type": "string"}
    out = kfset(nx, kind="dynamic", name="long?", formula=True, rule_type="boolean")
    assert out.ok is False  # a formula is text
    out = kfset(nx, kind="dynamic", name="long?", formula="true", rule_type="boolean")
    assert out.data["rule"]["value"] is True and out.data["rule"]["name"] == "long?"


def test_set_refuses_before_nx(nx, kf):
    cases = [
        ({"kind": "child", "name": "a"}, "/class_name"),
        ({"kind": "child", "name": "a", "class_name": "bad class"}, "/class_name"),
        ({"kind": "dynamic", "name": "a"}, "/formula"),
        ({"kind": "dynamic", "name": "a:b", "formula": "1"}, "/name"),
        ({"kind": "child", "name": "a", "class_name": "plate",
          "parameters": [{"name": "w", "value": 1}, {"name": "W", "value": 2}]},
         "/parameters/1/name"),
        ({"kind": "dynamic", "name": "a", "formula": "1",
          "parameters": [{"name": "w", "value": 1}]}, "/parameters"),
        ({"kind": "child", "name": "p0", "class_name": "plate"}, "/name"),
    ]  # fmt: skip
    for args, field in cases:
        out = kfset(nx, **args)
        assert out.ok is False, args
        assert out.error["field"] == field, (args, out.error)
        assert out.mutation_outcome == "not_started", args
    assert kf.children == {} and kf.dynamic == {} and kf.updates == 0


def test_set_refuses_kind_changes(nx, kf):
    kfset(nx, kind="child", name="holder", class_name="plate")
    out = kfset(nx, kind="dynamic", name="holder", formula="1")
    assert out.error["code"] == "NX_ALREADY_EXISTS" and "child rule" in out.error["message"]
    out = kfset(nx, kind="child", name="holder", class_name="ug_expression")
    assert out.error["field"] == "/class_name"
    kfset(nx, kind="dynamic", name="d", formula="1")
    out = kfset(nx, kind="child", name="d", class_name="plate")
    assert out.error["code"] == "NX_ALREADY_EXISTS"


def test_unknown_class_and_bad_values(nx, kf):
    out = kfset(nx, kind="child", name="x", class_name="my_class")
    assert out.error["code"] == "NX_NOT_FOUND" and out.error["field"] == "/class_name"
    assert out.error["nx_code"] == 3520002
    out = kfset(nx, kind="child", name="y", class_name="plate",
                parameters=[{"name": "width", "value": "nope"}])  # fmt: skip
    assert out.error["field"] == "/parameters/0/value"
    out = kfset(nx, kind="dynamic", name="z", formula="(")
    assert out.error["code"] == "NX_INVALID_FORMULA" and out.error["field"] == "/formula"
    out = kfset(nx, kind="dynamic", name="w", formula="nothing")
    assert out.error["code"] == "NX_INVALID_FORMULA"


def test_update_errors_and_license(nx, kf):
    kf.update_errors = 2
    out = kfset(nx, kind="dynamic", name="z", formula="1")
    assert out.error["code"] == "NX_UPDATE_FAILED"
    kf.update_errors = 0
    nx.licensed = False  # the license server lists none: NX is asked all the same
    out = call(nx, rules.nx_update_kf_rules)
    assert out.ok and kf.regenerated == 1, out
    nx.refused = True
    out = call(nx, rules.nx_update_kf_rules)
    assert out.error["code"] == "NX_LICENSE_UNAVAILABLE" and kf.regenerated == 1
    assert out.error["details"]["license"] == "ug_kf_execute"
    assert out.error["details"]["licenses"] == ["ug_kf_execute", "ug_kf_author"]
    assert out.error["details"]["license_check"] == "missing"


def test_update_regenerates(nx, kf):
    kf.dynamic["bad"] = ("Number", "nothing")
    out = call(nx, rules.nx_update_kf_rules)
    assert out.ok, out
    assert kf.regenerated == 1 and kf.updates == 1
    assert out.data["rules"] == 3 and [r["name"] for r in out.data["failed"]] == ["bad"]
