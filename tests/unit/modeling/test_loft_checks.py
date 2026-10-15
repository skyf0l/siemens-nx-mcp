"""nx_create_loft: open sections refused for a solid, snapped start points warned and
returned."""

from tests.kit import run
from tests.unit.modeling.test_feature_shorthands import evaluable, square
from tests.unit.modeling.test_loft_sections import _nx


def test_an_open_section_in_a_solid_loft_is_refused_before_nx():
    nx = _nx()
    bottom = evaluable(nx, square(10))
    top = evaluable(nx, square(10, z=30)[:3])  # three sides: open
    bad = run(nx, "nx_create_loft", sections=[[c.ref for c in bottom], [c.ref for c in top]])
    assert bad.error["field"] == "/sections/1" and "open" in bad.error["message"], bad
    assert "solid: false" in bad.error["hint"] and bad.mutation_outcome == "not_started"
    assert not [b for b in nx.builders if b.kind == "ThroughCurvesBuilder"]


def test_a_start_point_off_its_section_is_warned_and_the_corner_returned():
    nx = _nx()
    bottom = evaluable(nx, square(10))
    top = evaluable(nx, square(10, z=30))
    ok = run(nx, "nx_create_loft", sections=[[c.ref for c in bottom], [c.ref for c in top]],
             start_points=[[100, 100, 0], [10, 10, 30]])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["start_points"] == [[10, 10, 0], [10, 10, 30]]
    (note,) = [w for w in ok.warnings if "start_points[0]" in w["message"]]
    assert "nearest corner [10, 10, 0]" in note["message"]
