"""nx_create_transform refuses a body listed twice before anything changes."""

from tests.kit import run
from tests.unit.modeling.fakes import modeling_nx


def test_a_body_listed_twice_is_refused_not_started():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    bad = run(nx, "nx_create_transform", bodies=[block.ref, block.ref],
              transform={"translation": [0, 0, 5]}, copy=True)  # fmt: skip
    assert bad.error["field"] == "/bodies/1" and bad.mutation_outcome == "not_started", bad
    assert not nx.builders
