"""Assembly occurrences: poses, direct children, moves, load state, NX error lists."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.assembly import structure
from nx_mcp.kit import geom
from tests.kit import fake_nx
from tests.kit.objects import matrix, point

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
I3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def nx_move(current, shift, delta):
    """NX 2406 MoveComponent: rotate about the assembly origin, then translate."""
    t, r = current
    return geom.add(geom.matvec(delta, t), shift), geom.matmul(delta, r)


def component(t=(0, 0, 0), r=I3, parent=None, tag=1):
    cols = [[r[0][j], r[1][j], r[2][j]] for j in range(3)]
    return NS(Tag=tag, Parent=parent, GetPosition=lambda: (point(*t), matrix(*cols)))


def test_pose_reads_row_major_rotation():
    t, r = structure.pose(component((1, 2, 3), RZ90))
    assert t == [1, 2, 3] and r == RZ90


def test_direct_child_and_paths():
    root = NS(Tag=9)
    part = NS(ComponentAssembly=NS(RootComponent=root))
    child = component(parent=root, tag=2)
    nested = component(parent=child, tag=3)
    assert structure.is_direct_child(part, child) and not structure.is_direct_child(part, nested)
    assert structure.root(NS()) is None and structure.direct_children(NS()) == []


def test_move_reads_back_and_fails_on_a_different_pose():
    nx = fake_nx()
    state = {"t": [20.0, 0.0, 0.0], "r": I3}
    c = NS(Tag=1, GetPosition=lambda: (point(*state["t"]),
                                       matrix(*([state["r"][0][j], state["r"][1][j],
                                                 state["r"][2][j]] for j in range(3)))))  # fmt: skip

    def move(_c, shift, m):
        state["t"], state["r"] = nx_move((state["t"], state["r"]), geom.xyz(shift), geom.rows(m))

    nx.part.ComponentAssembly = NS(MoveComponent=move)
    got = structure.move(nx.tx, c, ([30.0, 10.0, 0.0], RZ90))
    assert got[0] == pytest.approx([30, 10, 0])
    nx.part.ComponentAssembly = NS(MoveComponent=lambda *a: None)
    with pytest.raises(NXError) as error:
        structure.move(nx.tx, c, ([0.0, 0.0, 0.0], I3))
    assert error.value.code == "NX_VERIFICATION_FAILED"
    assert error.value.details["check"] == "pose.translation"


def test_load_state_without_a_loaded_prototype():
    nx = fake_nx()
    loaded = NS(Prototype=NS(FullPath="C:/w/a.prt", IsFullyLoaded=False), Tag=1)
    assert structure.load_state(nx.tx, loaded) == {
        "part_path": "C:/w/a.prt", "load_state": "partially_loaded"
    }  # fmt: skip
    nx.uf.Assem = NS(AskComponentData=Mock(side_effect=RuntimeError("Missing part")))
    missing = structure.load_state(nx.tx, NS(Prototype=None, Tag=2))
    assert missing == {"part_path": None, "load_state": "unloaded"}
    nx.uf.Assem = NS(AskComponentData=lambda tag: ("D:/m/child.prt",))
    assert structure.load_state(nx.tx, NS(Prototype=None, Tag=3))["part_path"] == "D:/m/child.prt"


def test_check_reports_and_disposes_error_lists():
    errors = NS(Length=1, GetErrorInfo=lambda i: "conflicting mate", Dispose=Mock())
    with pytest.raises(NXError, match="conflicting mate") as error:
        structure.check(errors, "NX_BUILD_FAILED")
    assert error.value.code == "NX_BUILD_FAILED"
    errors.Dispose.assert_called_once()
    empty = NS(Length=0, Dispose=Mock())
    structure.check(empty)
    empty.Dispose.assert_called_once()
    structure.check(None)


class _Info:
    """An NXOpen.ErrorInfo: its str() is only the object repr."""

    def __init__(self, description, code):
        self.Description, self.ErrorCode = description, code

    def __str__(self):
        return "<NXOpen.ErrorInfo object at 0x0000021>"


def test_check_reads_the_error_info_text_and_code():
    infos = [_Info("Part is not compatible", 641029), _Info("", None)]
    errors = NS(Length=2, GetErrorInfo=lambda i: infos[i], Dispose=Mock())
    with pytest.raises(NXError) as error:
        structure.check(errors, "NX_BUILD_FAILED", "NX did not replace the part")
    message = error.value.message
    assert "Part is not compatible (NX error 641029)" in message and "0x" not in message
    assert error.value.nx_code == 641029
    assert error.value.details["errors"][1] == "NX gave no reason"
    assert structure.error_text("plain text") == ("plain text", None)
