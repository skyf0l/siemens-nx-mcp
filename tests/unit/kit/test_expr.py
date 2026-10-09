from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import expr
from tests.kit import NXException, fake_nx


def _code(call, *a, **k):
    with pytest.raises(NXError) as error:
        call(*a, **k)
    return error.value


def test_create_validates_and_maps_formula_errors():
    nx = fake_nx()
    exp = expr.create(nx.tx, "height", "10", "mm")
    assert exp.Name == "height" and exp.Units.Name == "MilliMeter"
    assert _code(expr.create, nx.tx, "height", "5").code == "NX_NAME_CONFLICT"
    assert _code(expr.create, nx.tx, "2bad", "5").field == "/name"
    assert _code(expr.create, nx.tx, "w", "5", "inch").field == "/units"
    assert _code(expr.create, nx.tx, "w", "").field == "/formula"
    err = _code(expr.create, nx.tx, "w", "invalid")
    assert err.code == "NX_INVALID_FORMULA" and err.nx_code == 1050017


def test_set_formula_value_units_and_dependents():
    nx = fake_nx()
    exp = nx.expression("p1", "10")
    assert expr.set_formula(nx.tx, exp, "20") is exp and exp.RightHandSide == "20"
    assert _code(expr.set_formula, nx.tx, exp, "invalid").code == "NX_INVALID_FORMULA"
    exp.IsRightHandSideLockedFromEdit = True
    assert _code(expr.set_formula, nx.tx, exp, "30").code == "NX_READ_ONLY"
    assert expr.value(nx.tx, exp) == 10.0
    assert expr.value(nx.tx, NS(Type="Integer", IntegerValue=3)) == 3
    assert expr.value(nx.tx, NS(Type="String")) is None
    assert expr.units(exp) == "unitless" and expr.units(NS(Units=NS(Abbreviation="mm"))) == "mm"
    assert expr.dependents(exp) == [] and expr.dependents(NS()) == []
    assert expr.find(nx.tx, "p1") is exp and expr.find(nx.tx, "P1") is None


def test_alias_matches_descriptions():
    start = NS(Name="p3", Description="Extrude(2) Start Limit")
    end = NS(Name="p4", Description="Extrude(2) End Limit")
    feature = NS(GetExpressions=lambda: [start, end])
    assert expr.alias(feature, "distance") is end
    assert expr.alias(feature, "start") is start
    err = _code(expr.alias, feature, "radius")
    assert err.code == "NX_NOT_FOUND" and [e["name"] for e in err.details["expressions"]] == [
        "p3",
        "p4",
    ]
    twin = NS(
        GetExpressions=lambda: [
            NS(Name="a", Description="Radius 1"),
            NS(Name="b", Description="Radius 2"),
        ]
    )
    assert _code(expr.alias, twin, "radius").code == "NX_AMBIGUOUS_REFERENCE"


def test_bind():
    nx = fake_nx()
    target, source = nx.expression("p9", "0"), nx.expression("width", "5")
    expr.bind(nx.tx, target, source)
    assert target.RightHandSide == "width"
    expr.bind(nx.tx, target, 2.5)
    assert target.RightHandSide == "2.5"

    def dimension_error(_exp, _formula):
        raise NXException(3270081, "Dimension error")

    nx.part.Expressions.EditExpression = dimension_error
    assert _code(expr.bind, nx.tx, target, source).code == "NX_INVALID_ARGUMENT"
    assert _code(expr.bind, nx.tx, target, True).code == "NX_INVALID_ARGUMENT"
