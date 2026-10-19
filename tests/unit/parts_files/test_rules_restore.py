"""nx_set_kf_rule keeps the rule it changed: the compensating undo sets a child's parameters
or a dynamic rule's formula back, deletes a rule the call created, and refuses a rule
changed since."""

from __future__ import annotations

from tests.kit import run
from tests.unit.parts_files.test_rules import kf, kfset  # noqa: F401  # the fixture


def compensate(nx, **kw):
    return run(nx, "nx_undo_operation", mode="compensate", **kw)


def test_a_child_rule_change_is_set_back(nx, kf):  # noqa: F811
    assert kfset(nx, kind="child", name="holder", class_name="plate",
                 parameters=[{"name": "length", "value": 15}]).ok  # fmt: skip
    changed = kfset(nx, kind="child", name="holder", class_name="plate",
                    parameters=[{"name": "width", "value": 9}])  # fmt: skip
    assert changed.ok and kf.children["holder"][1]["width"] == "9", changed
    undo = compensate(nx)
    assert undo.ok and undo.data["restored"] == ["KF rule holder set back"], undo
    assert kf.children["holder"][1] == {"length": "15", "width": "40"}
    assert kf.specified["holder"] == ["length"]


def test_a_dynamic_rule_formula_is_set_back_and_a_new_rule_deleted(nx, kf):  # noqa: F811
    assert kfset(nx, kind="dynamic", name="twice", formula="p0: * 2").ok
    assert kfset(nx, kind="dynamic", name="twice", formula="5").ok
    back = compensate(nx)
    assert back.ok and kf.dynamic["twice"] == ("Number", "p0: * 2"), back
    gone = compensate(nx)
    assert gone.ok and "twice" not in kf.dynamic, gone
    assert gone.data["restored"] == ["KF rule twice deleted (the operation created it)"]


def test_a_rule_changed_since_is_never_overwritten(nx, kf):  # noqa: F811
    first = kfset(nx, kind="dynamic", name="twice", formula="3")
    assert first.ok and kfset(nx, kind="dynamic", name="twice", formula="4").ok
    refused = compensate(nx, operation=first.operation_id)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert kf.dynamic["twice"][1] == "4"
