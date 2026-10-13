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


def test_rhs_keeps_an_evaluated_formula_and_writes_numbers_exactly():
    from nx_mcp.core.types import CountFormula, Formula

    assert expr.rhs(Formula(5.0, "t/2")) == "t/2" and expr.rhs(2.5) == "2.5"
    assert expr.rhs(CountFormula(4, "n")) == "n" and expr.rhs(4, integer=True) == "4"
    target = NS(RightHandSide="")
    assert expr.drive(target, Formula(1.0, "a")) is target and target.RightHandSide == "a"
    kept = Formula(3.0, "b")
    assert expr.number(kept) is kept and type(expr.number(3)) is float


def test_evaluate_reads_names_and_needs_a_scratch_expression_for_formulas():
    from tests.kit.kernel import services

    nx = fake_nx()
    svc = services(nx)
    t = expr.create(nx.tx, "t", "2", "in")
    t.Value = 50.8  # NX's Value is in part units; GetValueUsingUnits in the expression's
    t.expression_value = 2.0
    assert expr.evaluate(svc, "t", "length", "mm") == pytest.approx(50.8)
    assert expr.evaluate(svc, "t", "length", "in") == pytest.approx(2.0)
    assert expr.evaluate(svc, "t*2", "length", "mm", scratch=True) == pytest.approx(101.6)
    assert [e.Name for e in nx.part.Expressions] == ["t"]  # the scratch expression is gone
    read_only = _code(expr.evaluate, svc, "t*2", "length", "mm", field="/tol")
    assert read_only.field == "/tol" and "expression name, not a formula" in read_only.message
    assert "no expression 'u'" in _code(expr.evaluate, svc, "u", "length", "mm").message
    assert "not an angle" in _code(expr.evaluate, svc, "t", "angle", "deg").message


def test_value_or_none_swallows_unreadable_expressions():
    nx = fake_nx()
    exp = NS(Type="Number", GetValueUsingUnits=lambda _option: 7)
    assert expr.value_or_none(nx.tx, exp) == 7.0
    assert expr.value_or_none(nx.tx, NS(Type="String")) is None

    def refuse(_option):
        raise NXException(1050017, "no value")

    assert expr.value_or_none(nx.tx, NS(Type="Number", GetValueUsingUnits=refuse)) is None


def test_units_are_the_short_names_the_tools_take():
    """NX 2406 abbreviates Degrees as a degree sign; tools say deg everywhere."""
    assert expr.units(NS(Units=NS(Name="Degrees", Abbreviation="°"))) == "deg"
    assert expr.units(NS(Units=NS(Name="Radian", Abbreviation="rad"))) == "rad"
    assert expr.units(NS(Units=NS(Name="Inch", Abbreviation="in"))) == "in"
    assert expr.units(NS(Units=NS(Name="Other", Abbreviation="°"))) == "deg"
    assert expr.units(NS(Units=NS(Name="Newton", Abbreviation="N"))) == "N"


def test_tube_diameter_aliases_match_only_a_tube():
    """NX 2406 describes a tube's outer diameter as Limit 1 and the inner as Limit 2 (live:
    OD 10, ID 4 -> p0 (Tube(2) Limit 1) = 10, p1 (Tube(2) Limit 2) = 4)."""
    outer = NS(Name="p0", Description="(Tube(2) Limit 1)")
    inner = NS(Name="p1", Description="(Tube(2) Limit 2)")
    tube = NS(GetExpressions=lambda: [outer, inner])
    assert expr.alias(tube, "outer_diameter") is outer
    assert expr.alias(tube, "inner_diameter") is inner
    other = NS(GetExpressions=lambda: [NS(Name="p5", Description="Revolve(3) Limit 1")])
    err = _code(expr.alias, other, "outer_diameter")
    assert err.code == "NX_NOT_FOUND" and err.details["expressions"][0]["name"] == "p5"
    assert {"outer_diameter", "inner_diameter", "distance"} <= set(expr.alias_names())
