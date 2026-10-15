"""nx_edit_faces replace with a replacement face that faces the other way."""

from tests.kit import run
from tests.unit.modeling.test_action_readbacks import _moves, _with_hole


def test_an_opposed_replacement_face_reverses_the_builder():
    nx, block, _hole = _with_hole()
    other = nx.block(10, 10, 10, origin=(100, 0, 20))
    top, below = block.face((0, 0, 1)), other.face((0, 0, -1))
    _moves(nx, "ReplaceFaceBuilder", block, top, (0, 0, 20))
    ok = run(nx, "nx_edit_faces", faces=[top.ref], action="replace", replacement=below.ref)
    assert ok.ok, ok
    assert nx.last("ReplaceFaceBuilder").ReverseDirection is True


def test_a_parallel_replacement_face_keeps_the_builder_direction():
    nx, block, _hole = _with_hole()
    other = nx.block(10, 10, 15, origin=(100, 0, 0))
    top, target = block.face((0, 0, 1)), other.face((0, 0, 1))
    _moves(nx, "ReplaceFaceBuilder", block, top, (0, 0, 15))
    ok = run(nx, "nx_edit_faces", faces=[top.ref], action="replace", replacement=target.ref)
    assert ok.ok, ok
    assert nx.last("ReplaceFaceBuilder").ReverseDirection is not True
