"""nx_create_scale factors given as expressions keep driving the SCALE feature."""

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx
from tests.unit.modeling.test_expression_values import expression


def scale_nx(volume, centroid):
    nx = modeling_nx()
    block = nx.block(10, 10, 10)

    def scaled(b):
        block.volume, block.centroid = volume, centroid
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    return nx, block


def test_uniform_factor_takes_an_expression_name():
    nx, block = scale_nx(8000.0, [10.0, 10.0, 10.0])
    expression(nx, "shrink", 2.0, unit=None)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factor="shrink")
    assert ok.ok, ok
    assert nx.last("ScaleBuilder").UniformFactor.RightHandSide == "shrink"


def test_per_axis_factors_keep_their_formulas():
    nx, block = scale_nx(2000.0, [10.0, 5.0, 5.0])
    expression(nx, "grow", 2.0, unit=None)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factors=["grow", 1, 1])
    assert ok.ok, ok
    b = nx.last("ScaleBuilder")
    assert b.ScaleXdirection.RightHandSide == "grow" and b.ScaleYdirection.RightHandSide == "1.0"


def test_a_stale_csys_is_refused_before_anything_changes():
    nx, block = scale_nx(2000.0, [10.0, 5.0, 5.0])
    bad = run(nx, "nx_create_scale", bodies=[block.ref], factors=[2, 1, 1], csys="d:999999")
    assert bad.error["field"] == "/csys" and bad.mutation_outcome == "not_started", bad
