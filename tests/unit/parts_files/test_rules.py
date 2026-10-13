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
        return list(CLASSES[klass])

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
        if klass not in CLASSES:
            raise NXException(3520002, "Second parameter is invalid")
        values = dict(self.children[name][1]) if name in self.children else dict(CLASSES[klass])
        values.update(dict(params))
        self.children[name] = (klass, values)

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
        return self.update_errors

    def RegenerateAll(self):
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
    nx.licensed = True
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
    nx.licensed = False
    out = call(nx, rules.nx_update_kf_rules)
    assert out.error["code"] == "NX_LICENSE_UNAVAILABLE"
    assert out.mutation_outcome == "not_started" and kf.regenerated == 0


def test_update_regenerates(nx, kf):
    kf.dynamic["bad"] = ("Number", "nothing")
    out = call(nx, rules.nx_update_kf_rules)
    assert out.ok, out
    assert kf.regenerated == 1 and kf.updates == 1
    assert out.data["rules"] == 3 and [r["name"] for r in out.data["failed"]] == ["bad"]
