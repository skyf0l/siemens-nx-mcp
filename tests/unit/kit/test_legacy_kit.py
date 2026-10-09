"""Legacy names for kit helpers: their ``NXError`` is an ``NXToolError``."""

import pytest

from nx_mcp import legacy_kit
from nx_mcp.core.errors import NXError
from nx_mcp.runtime import NXToolError


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: legacy_kit.finite(0, "radius", True), "radius must be finite and positive"),
        (lambda: legacy_kit.vector([1, 2]), "vector must contain three numbers"),
        (lambda: legacy_kit.unit_normal([0, 0, 0]), "normal must be finite and nonzero"),
        (
            lambda: legacy_kit.unit_normal([1], "direction"),
            "direction requires three finite numbers",
        ),
        (
            lambda: legacy_kit.object_ids([], "edges"),
            "edges must be a non-empty list of object IDs",
        ),
        (lambda: legacy_kit.plane_through([[0, 0, 0]] * 3), "The three points are collinear"),
    ],
)
def test_adapters_raise_legacy_errors(call, message):
    with pytest.raises(NXToolError) as error:
        call()
    assert error.value.code == "NX_INVALID_ARGUMENT" and error.value.message == message
    # One hierarchy: the kit error itself reaches legacy ``except NXToolError`` blocks.
    assert isinstance(error.value, NXError) and error.value.category == "invalid_argument"


def test_adapters_return_kit_results():
    assert legacy_kit.unit_normal([0, 0, 2]) == [0, 0, 1]
    assert legacy_kit.points3([[1, 2, 3]]) == [[1.0, 2.0, 3.0]]
    assert legacy_kit.finite.__module__ == "nx_mcp.kit.args"
