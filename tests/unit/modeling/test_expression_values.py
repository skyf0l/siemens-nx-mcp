"""Expression names and formulas as values (benchmark finding 4): gate 9 evaluates them in
NX and the feature tools write the formula into the builder expression, so the feature
follows later edits of the expression. Shared fake, real kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.types import Formula
from nx_mcp.domains.modeling import holes
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body
from tests.unit.modeling.test_patterns import pattern, pattern_nx

MM = NS(Name="MilliMeter", Abbreviation="mm")
DEG = NS(Name="Degrees", Abbreviation="deg")


def expression(nx, name, value, unit=MM):
    e = nx.expression(name, repr(value), value)
    e.Units = unit
    return e


def test_extrude_end_takes_an_expression_name_and_keeps_it():
    nx = modeling_nx()
    expression(nx, "thickness", 12.0)
    sk = nx.sketch(origin=(0, 0, 0))
    nx.on_commit(
        "ExtrudeBuilder", lambda b: feature(nx, "EXTRUDE", [new_body(nx, [0, 0, 0, 4, 4, 12])])
    )
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end="thickness")
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    assert b.Limits.EndExtend.Value.RightHandSide == "thickness"  # driven, not a copy
    assert b.Limits.StartExtend.Value.RightHandSide == "0.0"
    # The scratch expression of a formula is gone again; only the user's expression stays.
    assert [e.Name for e in nx.part.Expressions] == ["thickness"]


def test_symmetric_extrude_and_formula_limits():
    nx = modeling_nx()
    expression(nx, "thickness", 8.0)
    sk = nx.sketch()
    nx.on_commit(
        "ExtrudeBuilder", lambda b: feature(nx, "EXTRUDE", [new_body(nx, [0, 0, -4, 4, 4, 4])])
    )
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end="thickness/2", symmetric=True)
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    assert b.Limits.EndExtend.Value.RightHandSide == "thickness/2"
    assert b.Limits.StartExtend.Value.RightHandSide == "-(thickness/2)"
    assert [e.Name for e in nx.part.Expressions] == ["thickness"]


@pytest.mark.parametrize(
    ("end", "code", "words"),
    [
        ("nope", "NX_INVALID_ARGUMENT", "no expression 'nope'"),
        ("ang", "NX_INVALID_ARGUMENT", "not a length"),
        ("nope*2", "NX_INVALID_ARGUMENT", "does not have"),
        ("ang*2", "NX_INVALID_ARGUMENT", "is not a length"),
    ],
)
def test_a_missing_or_wrong_expression_is_refused_before_the_builder(end, code, words):
    nx = modeling_nx()
    expression(nx, "ang", 30.0, DEG)
    sk = nx.sketch()
    bad = run(nx, "nx_create_extrude", sketch=sk.ref, end=end)
    assert bad.error["code"] == code and words in bad.error["message"], bad
    assert bad.error["field"] == "/end" and bad.mutation_outcome == "not_started"
    assert nx.builders == [] and [e.Name for e in nx.part.Expressions] == ["ang"]


def test_an_inch_expression_is_converted_for_the_checks_and_kept_for_nx():
    nx = modeling_nx()
    expression(nx, "t_in", 1.0, NS(Name="Inch", Abbreviation="in"))
    sk = nx.sketch(origin=(0, 0, 0))
    nx.on_commit(
        "ExtrudeBuilder", lambda b: feature(nx, "EXTRUDE", [new_body(nx, [0, 0, 0, 4, 4, 25.4])])
    )
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end={"type": "distance", "value": "t_in"})
    assert ok.ok, ok  # the extent check compared 25.4 mm
    assert nx.last("ExtrudeBuilder").Limits.EndExtend.Value.RightHandSide == "t_in"


def test_revolve_angles_blend_radius_and_chamfer_offsets_keep_formulas():
    nx = modeling_nx()
    expression(nx, "sweep", 90.0, DEG)
    expression(nx, "r", 2.0)
    sk = nx.sketch()
    nx.on_commit(
        "RevolveBuilder", lambda b: feature(nx, "REVOLVE", [new_body(nx, [0, 0, 0, 9, 9, 9])])
    )
    ok = run(nx, "nx_create_revolve", sketch=sk.ref, axis="z", end_angle="sweep")
    assert ok.ok, ok
    assert nx.last("RevolveBuilder").Limits.EndExtend.Value.RightHandSide == "sweep"

    block = nx.block(40, 20, 10)

    def smaller(volume):
        def hook(b):
            block.volume = volume
            return feature(nx, "DETAIL", [block])

        return hook

    nx.on_commit("EdgeBlendBuilder", smaller(7990.0))
    blend = run(nx, "nx_create_blend", edges=[block.edges[0].ref], radius="r")
    assert blend.ok, blend
    assert nx.last("EdgeBlendBuilder").AddChainset.calls[0][0][1] == "r"
    nx.on_commit("ChamferBuilder", smaller(7900.0))
    chamfer = run(nx, "nx_create_chamfer", edges=[block.edges[1].ref], offset="r", angle="sweep/3")
    assert chamfer.ok, chamfer
    b = nx.last("ChamferBuilder")
    assert b.FirstOffsetExp.RightHandSide == "r" and b.AngleExp.RightHandSide == "sweep/3"


def test_pattern_count_and_spacing_take_expressions():
    nx, block, seed = pattern_nx(count=4)
    expression(nx, "n_holes", 4.0, None)
    expression(nx, "pitch", 7.5)
    ok = pattern(nx, seed, "linear", direction=[1, 0, 0], count="n_holes", spacing="pitch")
    assert ok.ok and ok.data["count"] == 4, ok
    d = nx.last("PatternFeatureBuilder").PatternService.RectangularDefinition
    assert d.XSpacing.NCopies.RightHandSide == "n_holes"
    assert d.XSpacing.PitchDistance.RightHandSide == "pitch"
    expression(nx, "one", 1.0, None)
    few = pattern(nx, seed, "linear", direction=[1, 0, 0], count="one", spacing=5)
    assert few.error["field"] == "/parameters/count" and "2.." in few.error["message"], few
    half = pattern(nx, seed, "linear", direction=[1, 0, 0], count="pitch/3", spacing=5)
    assert half.error["field"] == "/parameters/count" and "whole number" in half.error["message"]


def test_hole_sizes_keep_their_formulas():
    spec = holes.check_spec(
        "counterbore",
        Formula(6.0, "d"),
        {"type": "distance", "value": Formula(20.0, "depth")},
        118.0,
        {"diameter": Formula(11.0, "d*2"), "depth": 4.0},
        None,
        None,
    )
    assert spec["diameter"].formula == "d" and spec["depth"].formula == "depth"
    assert spec["counterbore"]["diameter"].formula == "d*2"
    assert spec["counterbore"]["depth"] == 4.0
