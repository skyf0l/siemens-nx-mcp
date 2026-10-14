"""kit.bodies.of for features: a pattern's copies, and a feature without bodies."""

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import bodies
from tests.kit import fake_nx
from tests.kit.objects import Feature


class Pattern(Feature):
    """NX 2406 Pattern Feature / Pattern Geometry: GetBodies is empty, the copies are its
    associated bodies."""

    def __init__(self, copies):
        super().__init__("Pattern Feature(3)", [])
        self.JournalIdentifier = "Pattern Feature(3)"
        self.copies = copies

    def GetAssociatedBodies(self):
        return list(self.copies)


def test_a_pattern_feature_gives_its_copies():
    nx = fake_nx()
    copies = [nx.block(1, 1, 1) for _ in range(3)]
    assert bodies.of(nx.tx, Pattern(copies + copies[:1])) == copies  # unique, in order


def test_a_feature_with_bodies_keeps_them_and_ignores_associated_bodies():
    nx = fake_nx()
    own, other = nx.block(1, 1, 1), nx.block(2, 2, 2)
    feature = Pattern([other])
    feature.bodies = [own]  # a pattern within its seed's body: GetBodies names that body
    assert bodies.of(nx.tx, feature) == [own]


def test_a_feature_without_bodies_is_named_in_the_error():
    nx = fake_nx()
    datum = Feature("DATUM_PLANE(4)", [])
    datum.JournalIdentifier = "DATUM_PLANE(4)"
    with pytest.raises(NXError) as error:
        bodies.of(nx.tx, datum, field="/bodies/0")
    assert error.value.code == "NX_NOT_FOUND" and error.value.field == "/bodies/0"
    assert "DATUM_PLANE(4)" in error.value.message
    with pytest.raises(NXError) as empty_pattern:
        bodies.of(nx.tx, Pattern([]))
    assert "Pattern Feature(3)" in empty_pattern.value.message
