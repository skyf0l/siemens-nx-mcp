"""nx_create_shell: zone expressions in part units; a wall too thick is explained."""

from tests.kit import run
from tests.unit.modeling.fakes import modeling_nx
from tests.unit.modeling.test_shell_alt import _shelled


def test_zone_expression_takes_the_part_length_unit():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    top, side = block.face((0, 0, 1)), block.face((1, 0, 0))
    _shelled(nx, block, 2500.0)
    ok = run(nx, "nx_create_shell", body=block.ref, thickness=2, remove_faces=[top.ref],
             alternate=[{"faces": [side.ref], "thickness": 4}])  # fmt: skip
    assert ok.ok, ok
    (call,) = nx.last("ShellBuilder").FaceThicknesses.Append.calls
    assert call[0][0].args[2] == "MilliMeter"


def test_a_wall_thicker_than_half_the_body_is_explained():
    nx = modeling_nx()
    block = nx.block(60, 40, 30)
    top = block.face((0, 0, 1))
    nx.refuse("ShellBuilder", code=671638, message="Cannot apply Shell.")
    bad = run(nx, "nx_create_shell", body=block.ref, thickness=25, remove_faces=[top.ref])
    assert bad.error["field"] == "/thickness" and "less than 15" in bad.error["hint"], bad
    assert bad.error["details"]["smallest_extent"] == 30
