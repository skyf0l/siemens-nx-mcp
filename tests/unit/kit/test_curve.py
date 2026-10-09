"""kit.curve: UF Eval points, closest points and minimum distance (fake UF)."""

import pytest

from nx_mcp.kit import curve
from tests.unit.sketch.fakes import sketch_nx


def test_ends_point_at_closest_and_min_distance():
    nx = sketch_nx()
    sk = nx.sketch()
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    b = nx.line(sk, (0, 3, 0), (10, 3, 0))
    assert curve.ends(nx.tx, a) == ([0, 0, 0], [10, 0, 0])
    assert curve.point_at(nx.tx, a, 0.25) == [2.5, 0, 0]
    foot, tangent = curve.closest(nx.tx, a, [4, 7, 0])
    assert foot == pytest.approx([4, 0, 0], abs=1e-6) and tangent == pytest.approx([1, 0, 0])
    gap, on_a, on_b = curve.min_distance(nx.tx, a, b)
    assert gap == pytest.approx(3) and on_a[1] == 0 and on_b[1] == 3


def test_evaluator_is_freed_even_when_evaluation_fails():
    nx = sketch_nx()
    freed = []
    nx.uf.Eval.Free = freed.append
    nx.uf.Eval.EvaluateUnitVectors = lambda h, t: 1 / 0
    sk = nx.sketch()
    line = nx.line(sk, (0, 0, 0), (1, 0, 0))
    with pytest.raises(Exception):  # noqa: B017 - mapped by native() to NX_ERROR
        curve.ends(nx.tx, line)
    assert freed == [line.Tag]
