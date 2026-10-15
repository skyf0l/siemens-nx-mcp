"""Loft sections made of curves and edges, not only sketches (shared fake, real kernel)."""

import math
from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import modeling_nx
from tests.unit.modeling.test_feature_shorthands import arc, evaluable, extruded, square


def _nx(volume=10750.0):
    nx = modeling_nx()
    nx.on_commit("ThroughCurvesBuilder", extruded(nx, [-10, -10, 0, 10, 10, 30], volume))
    return nx


def test_a_closed_curve_and_a_list_of_curves_are_sections():
    nx = _nx()
    (ring,) = evaluable(nx, [arc((0, 0), 10, 0.0, 2 * math.pi)])
    sides = evaluable(nx, square(10, z=30))
    ok = run(nx, "nx_create_loft", sections=[ring.ref, [c.ref for c in sides]])
    assert ok.ok and not ok.warnings, ok
    built = nx.last("ThroughCurvesBuilder")
    first, second = (call[0][0] for call in built.SectionsList.Append.calls)
    assert first.rules == [("CurveDumb", [ring])] and second.rules == [("CurveDumb", sides)]
    # The circle starts at the square's corner angle, as for sketch sections.
    assert first.start is not None and second.start[1].X == -10.0
    assert [float(q.Expression.RightHandSide) for q in built.Alignment.points[:4]] == (
        pytest.approx([62.5, 87.5, 12.5, 37.5], abs=0.2)
    )


def test_curve_and_sketch_sections_mix_and_a_twisted_loft_still_warns():
    nx = _nx(4607.0)
    sk = nx.sketch()
    sk.geometry = evaluable(nx, square(10))
    top = evaluable(nx, square(10, z=30, reverse=True))
    ok = run(nx, "nx_create_loft", sections=[sk.ref, [c.ref for c in top]])
    assert ok.ok, ok
    (warning,) = ok.warnings
    assert "twists" in warning["message"] and "12000" in warning["message"]


def test_an_open_curve_section_lofts_a_sheet_along_the_centres():
    nx = modeling_nx()
    a, b = evaluable(nx, [lambda t: ([20 * t, 0.0, 0.0], [1.0, 0.0, 0.0]),
                          lambda t: ([20 * t, 0.0, 30.0], [1.0, 0.0, 0.0])])  # fmt: skip

    def sheet(_b):
        made = extruded(nx, [0, 0, 0, 20, 0, 30], 0.0)(_b)
        made.bodies[0].IsSolidBody = False
        return made

    nx.on_commit("ThroughCurvesBuilder", sheet)
    nx.part.MeasureManager.NewFaceProperties = lambda *a: NS(Area=600.0, Dispose=lambda: None)
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref], solid=False)
    assert ok.ok and ok.data["area"] == 600, ok
    assert len(nx.last("ThroughCurvesBuilder").SectionsList.Append.calls) == 2


@pytest.mark.parametrize(
    ("sections", "field"),
    [
        (lambda r, s, blk: [r, [s[0], s[0]]], "/sections/1/1"),
        (lambda r, s, blk: [r, r], "/sections/1"),
        (lambda r, s, blk: [r, blk], "/sections/1"),
        (lambda r, s, blk: [r, [blk]], "/sections/1/0"),
    ],
)
def test_section_errors_are_not_started(sections, field):
    nx = _nx()
    (ring,) = evaluable(nx, [arc((0, 0), 10, 0.0, 2 * math.pi)])
    sides = [c.ref for c in evaluable(nx, square(10, z=30))]
    block = nx.block(10, 10, 10)
    bad = run(nx, "nx_create_loft", sections=sections(ring.ref, sides, block.ref))
    assert bad.error["category"] == "invalid_argument" or bad.error["code"] == "NX_WRONG_KIND"
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not any(b.commits for b in nx.builders)
