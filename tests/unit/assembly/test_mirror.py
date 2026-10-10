import pytest

from tests.kit import run
from tests.unit.assembly.fakes import assembly_nx, place


@pytest.fixture
def nx():
    return assembly_nx()


def test_mirror_creates_one_component_per_source_at_the_reflected_origin(nx):
    a = place(nx, nx.blk, "A", (10, 5, 0))
    b = place(nx, nx.pin, "B", (30, -5, 2))
    ok = run(nx, "nx_create_component_mirror", components=[a.ref, b.ref],
             plane={"origin": [5, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    pairs = ok.data["components"]
    assert [p["source"]["name"] for p in pairs] == ["A", "B"]
    assert [p["mirrored"]["translation"] for p in pairs] == [[0, 5, 0], [-20, -5, 2]]
    assert ok.data["plane"] == {"origin": [5, 0, 0], "normal": [1, 0, 0]}
    builder = nx.assembly.reflect_builders[-1]
    assert builder.destroyed and builder.sources == [a, b]
    assert len(nx.assembly.components()) == 4


def test_mirror_refuses_duplicates_and_rolls_back_a_misplaced_copy(nx):
    a = place(nx, nx.blk, "A", (10, 0, 0))
    twice = run(nx, "nx_create_component_mirror", components=[a.ref, a.ref],
                plane={"principal": "yz"})  # fmt: skip
    assert twice.error["field"] == "/components/1" and twice.mutation_outcome == "not_started"
    nx.assembly.reflect_error = 0.5
    off = run(nx, "nx_create_component_mirror", components=[a.ref], plane={"principal": "yz"})
    assert off.error["details"]["check"] == "origin[0]" and off.mutation_outcome == "rolled_back", (
        off
    )
    assert nx.assembly.components() == [a]


def test_mirror_takes_a_bare_principal_plane_name(nx):
    a = place(nx, nx.blk, "A", (10, 5, 0))
    ok = run(nx, "nx_create_component_mirror", components=[a.ref], plane="yz")
    assert ok.ok, ok
    assert ok.data["components"][0]["mirrored"]["translation"] == [-10, 5, 0]
    assert ok.data["plane"]["normal"] == [1, 0, 0]
