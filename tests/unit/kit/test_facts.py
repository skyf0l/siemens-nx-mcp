"""kit.facts.interior_samples: UV grid cell centres inside a trimmed face, with normals."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import facts


def tx_for(inside, normal=(0.0, 0.0, 2.0)):
    """A tx whose face spans u 0..20, v 0..10 on z=0; ``inside(point)`` trims it."""

    def props(_tag, uv):
        return ([uv[0], uv[1], 0.0], [1, 0, 0], [0, 1, 0], [0] * 3, [0] * 3, list(normal), 0, 0)

    modeling = NS(
        AskFaceUvMinmax=lambda _tag: (0.0, 20.0, 0.0, 10.0),
        AskFaceProps=props,
        AskPointContainment=lambda p, _tag: 1 if inside(p) else 2,
    )
    return NS(uf=NS(Modeling=modeling))


def test_interior_samples_are_cell_centres_with_unit_normals():
    out = facts.interior_samples(tx_for(lambda p: True), NS(Tag=7), steps=2)
    assert [p for p, _n in out] == [[5, 2.5, 0], [5, 7.5, 0], [15, 2.5, 0], [15, 7.5, 0]]
    assert all(n == pytest.approx([0, 0, 1]) for _p, n in out)


def test_interior_samples_skip_trimmed_out_points_and_null_normals():
    out = facts.interior_samples(tx_for(lambda p: p[0] < 10), NS(Tag=7), steps=2)
    assert [p for p, _n in out] == [[5, 2.5, 0], [5, 7.5, 0]]
    assert facts.interior_samples(tx_for(lambda p: True, (0, 0, 0)), NS(Tag=7)) == []
