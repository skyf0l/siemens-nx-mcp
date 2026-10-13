"""Shell with alternate-thickness zones on the shared fake, through the real kernel."""

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def _shelled(nx, block, volume):
    def hook(_b):
        block.volume = volume
        return feature(nx, "SHELL", [block])

    nx.on_commit("ShellBuilder", hook)


def test_alternate_zones_append_one_expression_set_each():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    top, side, end = block.face((0, 0, 1)), block.face((1, 0, 0)), block.face((0, 1, 0))
    _shelled(nx, block, 2500.0)
    ok = run(nx, "nx_create_shell", body=block.ref, thickness=2, remove_faces=[top.ref],
             alternate=[{"faces": [side.ref], "thickness": 4},
                        {"faces": [end.ref], "thickness": "1 mm"}])  # fmt: skip
    assert ok.ok, ok
    calls = nx.last("ShellBuilder").FaceThicknesses.Append.calls
    assert [c[0][0].args[1] for c in calls] == ["-(4.0)", "-(1.0)"]
    collector = calls[0][0][0].args[0]
    assert collector.GetRules()[0][1] == [side]


def test_plain_shell_adds_no_zone():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    _shelled(nx, block, 2500.0)
    ok = run(nx, "nx_create_shell", body=block.ref, thickness=2)
    assert ok.ok, ok
    assert nx.last("ShellBuilder").FaceThicknesses.Append.calls == []


def test_zone_faces_are_checked_before_nx():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    other = nx.block(5, 5, 5, origin=(100, 0, 0))
    top, side = block.face((0, 0, 1)), block.face((1, 0, 0))
    removed = run(nx, "nx_create_shell", body=block.ref, thickness=2, remove_faces=[top.ref],
                  alternate=[{"faces": [top.ref], "thickness": 3}])  # fmt: skip
    assert removed.error["field"] == "/alternate/0/faces/0", removed
    assert removed.mutation_outcome == "not_started"
    twice = run(nx, "nx_create_shell", body=block.ref, thickness=2,
                alternate=[{"faces": [side.ref], "thickness": 3},
                           {"faces": [side.ref], "thickness": 4}])  # fmt: skip
    assert twice.error["field"] == "/alternate/1/faces/0", twice
    foreign = run(nx, "nx_create_shell", body=block.ref, thickness=2,
                  alternate=[{"faces": [other.face((0, 0, 1)).ref], "thickness": 3}])  # fmt: skip
    assert foreign.error["code"] == "NX_WRONG_OWNER", foreign
    thin = run(nx, "nx_create_shell", body=block.ref, thickness=2,
               alternate=[{"faces": [side.ref], "thickness": 0}])  # fmt: skip
    assert thin.error["field"] == "/alternate/0/thickness", thin
    assert not any(b.kind == "ShellBuilder" for b in nx.builders)
