"""nx_create_hole errors carry the values an agent needs to fix the call."""

from tests.kit import run
from tests.unit.modeling.test_holes_threads import hole_nx

DEPTH = {"type": "distance", "value": 10}


def test_a_position_in_an_existing_opening_says_so():
    nx, _block, top, _ = hole_nx()
    top.box = [0, 0, 20, 60, 40, 20]
    nx.remember(top)
    top.contains = lambda p: 2
    bad = run(nx, "nx_create_hole", face=top.ref, positions=[[15, 15, 20]], diameter=6,
              depth=DEPTH)  # fmt: skip
    assert "in an opening of the face" in bad.error["message"], bad
    assert bad.mutation_outcome == "not_started"


def test_a_narrow_counterbore_names_both_diameters():
    nx, _block, top, _ = hole_nx()
    bad = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 20]], form="counterbore",
              diameter=10, depth=DEPTH, counterbore={"diameter": 8, "depth": 2})  # fmt: skip
    assert "(8) must be wider than the hole (10)" in bad.error["message"], bad
