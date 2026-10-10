"""Gate 1 takes natural spellings, turns them into canonical values and, when it refuses a
value, says what to send. The sidecar's Pydantic adapter accepts the same values."""

import copy
from typing import Annotated, Literal, TypedDict

import pytest

from nx_mcp.core import types as T
from nx_mcp.core.catalogue import Param
from nx_mcp.core.errors import NXError
from nx_mcp.core.validate import match_option, suggest_field, validate
from nx_mcp.sidecar import schema
from nx_mcp.sidecar.proxy import CatalogueProxy
from tests.sidecar.test_schema import SAMPLE


def params(**hints):
    return {k: Param(k, h, None, True) for k, h in hints.items()}


def refused(spec, args) -> NXError:
    with pytest.raises(NXError) as error:
        validate(spec, args)
    return error.value


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("Through All", "through_all"),
        ("through-all", "through_all"),
        (" ONE_SIDE ", "one_side"),
        ("X", "x"),
        ("-X", "-x"),
        ("features", "feature"),  # a plural of a singular option
        ("bodies", "body"),
    ],
)
def test_options_match_in_any_case_spacing_or_number(value, canonical):
    spec = params(v=Literal["through_all", "one_side", "x", "-x", "feature", "body"])
    assert validate(spec, {"v": value}).args["v"] == canonical


def test_aliases_name_options_and_core_types_declare_some():
    spec = params(op=Annotated[Literal["unite", "subtract"], T.Aliases(union="unite")])
    assert validate(spec, {"op": "Union"}).args["op"] == "unite"
    core = params(combine=T.Combine, units=T.Units, plane=T.Plane)
    args = {
        "combine": {"operation": "Cut", "targets": ["b:1"]},
        "units": "Millimeters",
        "plane": {"principal": "Front"},  # NX's front view looks at XZ
    }
    out = validate(core, args).args
    assert out["combine"]["operation"] == "subtract" and out["units"] == "mm"
    assert out["plane"] == {"principal": "xz"}
    assert match_option("top", ("xy", "yz", "xz")) is None  # aliases only where declared
    assert match_option("ab", ("a_b", "a-b")) is None  # ambiguous: no guess


def test_a_wrong_option_lists_the_options_with_a_did_you_mean():
    error = refused(params(m=Literal["symmetric", "two_offsets"]), {"m": "symetric"})
    assert error.field == "/m"
    assert "expected one of 'symmetric', 'two_offsets'" in error.message
    assert "did you mean 'symmetric'?" in error.message


def test_unknown_fields_suggest_the_field_and_list_the_accepted_ones():
    spec = params(query=Annotated[str, T.Text(64)], kind=Literal["a"])
    error = refused(spec, {"name_contains": "steel", "kind": "a"})
    assert error.field == "/name_contains" and "did you mean 'query'?" in error.message
    assert error.details["accepted"] == ["query", "kind"]
    assert error.hint == "Send query instead of name_contains"

    class Target(TypedDict):
        bodies: Annotated[list[T.BodyRef], T.Items(1, 5)]

    nested = refused(params(t=Target), {"t": {"body": "b:1"}})
    assert nested.field == "/t/body" and "did you mean 'bodies'?" in nested.message
    assert suggest_field("sufrace", ["surface", "faces"]) == "surface"
    assert "takes no arguments" in refused({}, {"mode": "full"}).message


def test_natural_scalars_and_one_ref_for_a_list():
    spec = params(
        flag=bool,
        n=Annotated[int, T.Range(ge=1)],
        x=float,
        refs=Annotated[list[T.BodyRef], T.Items(1, 5)],
    )
    out = validate(spec, {"flag": "True", "n": "3", "x": "+0.1", "refs": "b:7"}).args
    assert out == {"flag": True, "n": 3, "x": 0.1, "refs": ["b:7"]}
    unit = refused(params(x=float), {"x": "40 mm"})
    assert "unit strings" in unit.message and "part units" in unit.message
    points = refused(params(p=Annotated[list[T.Point3], T.Items(1, 5)]), {"p": "0,0,0"})
    assert points.field == "/p"  # a string is no point: no wrapping
    assert "e.g. [0, 0, 0]" in refused(params(p=T.Point3), {"p": [0, 0]}).message


def test_refs_and_patterns_say_what_to_send():
    error = refused(params(f=T.FeatureRef), {"f": "EXTRUDE(2)"})
    assert "expected a feature ref like 'f:12'" in error.message
    assert "display names are not refs" in error.message
    word = Annotated[str, T.Text(8, r"^[a-z]+$"), T.Doc("a lower-case word")]
    message = refused(params(w=word), {"w": "ABC1"}).message
    assert "expected a lower-case word" in message and "^[a-z]+$" in message


def test_union_errors_list_the_accepted_forms():
    error = refused(params(axis=T.Axis), {"axis": "q"})
    assert error.field == "/axis"
    assert "{principal}" in error.message and "{direction, origin}" in error.message
    depth = refused(params(d=T.Length | Literal["through_all"]), {"d": "deep end"})
    assert "'through_all'" in depth.message and "a length" in depth.message
    principal = refused(params(p=T.Plane), {"p": {"principal": "foo", "origin": [0, 0, 0]}})
    assert "principal 'foo' is not accepted here; expected one of 'xy', 'yz', 'xz'" in (
        principal.message
    )


@pytest.mark.parametrize(
    "hint", [Literal["through_all"] | T.Length, T.Length | Literal["through_all"]]
)
def test_an_option_beats_an_expression_of_the_same_name(hint):
    spec = params(d=hint)
    exact = validate(spec, {"d": "through_all"})
    assert exact.args["d"] == "through_all" and exact.quantities == []
    natural = validate(spec, {"d": "Through All"})
    assert natural.args["d"] == "through_all" and natural.quantities == []
    formula = validate(spec, {"d": "thickness/2"})
    assert formula.args["d"] == "thickness/2" and formula.quantities == [(("d",), "length")]


def test_quantities_take_expression_names_and_formulas():
    spec = params(d=T.Length, a=T.Angle, n=Annotated[T.CountValue, T.Range(ge=2, le=9)])
    ok = validate(spec, {"d": "thickness", "a": "ang * 2", "n": "n_holes"})
    assert ok.quantities == [(("d",), "length"), (("a",), "angle"), (("n",), "count")]
    assert validate(spec, {"d": "12", "a": "90 deg", "n": 3.0}).args == {
        "d": 12.0,
        "a": "90 deg",
        "n": 3,
    }
    for bad in ({"d": "10 cm"}, {"d": "a b"}, {"d": "mm"}, {"a": "10 mm"}, {"n": 1}, {"n": 2.5}):
        args = {"d": 1, "a": 1, "n": 2, **bad}
        assert refused(spec, args).field == "/" + next(iter(bad)), bad


def test_gate_9_turns_an_expression_into_a_float_that_keeps_its_formula():
    seen = []

    def evaluate(formula, dim, unit, field=None):
        seen.append((formula, dim, unit, field))
        return 5.0

    value = T.convert_quantity("thickness", "length", "mm", evaluate, "/end")
    assert value == 5.0 and value.formula == "thickness" and isinstance(value, float)
    assert seen == [("thickness", "length", "mm", "/end")]
    assert (-value).formula == "-(thickness)" and -value == -5.0
    assert copy.deepcopy(value).formula == "thickness"
    count = T.convert_quantity("n", "count", "unitless", evaluate)
    assert count == 5 and isinstance(count, int) and count.formula == "n"
    with pytest.raises(ValueError, match="not a whole number"):
        T.convert_quantity("n", "count", "unitless", lambda *a, **k: 2.5)
    with pytest.raises(ValueError, match="needs NX"):
        T.convert_quantity("thickness", "length", "mm")
    assert T.convert_quantity("2 in", "length", "mm") == pytest.approx(50.8)


def test_the_variant_field_takes_natural_spellings_in_both_validators():
    args = {"kind": "A", "length": "thickness", "count": "3", "flag": "false", "target": "b:4"}
    out = validate(SAMPLE.params, args).args
    assert out["kind"] == "a" and out["count"] == 3 and out["flag"] is False
    schema.adapter(SAMPLE).validate_python(args)  # the sidecar adapter accepts it too
    with pytest.raises(Exception, match="did you mean"):
        schema.adapter(SAMPLE).validate_python({"kind": "aa", "count": 3})


def test_the_sidecar_sends_the_canonical_arguments():
    proxy = CatalogueProxy.__new__(CatalogueProxy)
    args = {"kind": "B", "points": [[1, 2]], "target": "b:4"}
    assert proxy._invalid(SAMPLE, args) is None
    assert args["kind"] == "b" and SAMPLE.variant_of(args) == "b"
    bad = proxy._invalid(SAMPLE, {"kind": "a", "lenght": 3})
    assert bad.field == "/lenght" and "did you mean 'length'?" in bad.message
