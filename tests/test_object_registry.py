import pytest

from nx_mcp.bridge import ObjectRegistry
from nx_mcp.core.errors import NXError


def test_object_reference_resolves_only_in_its_part_session():
    registry = ObjectRegistry()
    nx_object = object()
    reference = registry.register(nx_object, kind="sketch", name="PROFILE", part_id="part-1")

    assert registry.resolve(reference.id, expected_kind="sketch") is nx_object

    registry.invalidate_part("part-1")

    with pytest.raises(NXError) as error:
        registry.resolve(reference.id, expected_kind="sketch")
    assert error.value.code == "NX_STALE_REFERENCE"


def test_object_reference_rejects_wrong_kind():
    registry = ObjectRegistry()
    reference = registry.register(object(), kind="body", name="BODY(1)", part_id="part-1")

    with pytest.raises(NXError) as error:
        registry.resolve(reference.id, expected_kind="sketch")
    assert error.value.code == "NX_WRONG_KIND"


def test_registering_same_live_object_returns_same_session_id():
    registry = ObjectRegistry()
    nx_object = object()

    first = registry.register(nx_object, kind="body", name="BODY(1)", part_id="part-1")
    second = registry.register(nx_object, kind="body", name="BODY(1)", part_id="part-1")

    assert second.id == first.id


def test_object_reference_cannot_be_used_with_another_work_part():
    registry = ObjectRegistry()
    reference = registry.register(object(), kind="sketch", name="PROFILE", part_id="part-1")

    with pytest.raises(NXError) as error:
        registry.resolve(reference.id, expected_kind="sketch", part_id="part-2")

    assert error.value.code == "NX_STALE_REFERENCE"


def test_an_identity_hit_keeps_the_live_object_when_nx_reuses_a_tag():
    class Wrapper:
        def __init__(self, tag):
            self.Tag = tag

    registry = ObjectRegistry()
    dead = Wrapper(77)  # created, then undone inside the same call
    first = registry.register(dead, kind="curve", name="LINE(1)", part_id="part-1")
    live = Wrapper(77)  # NX hands the freed tag to the next object
    second = registry.register(live, kind="curve", name="LINE(2)", part_id="part-1")
    assert second.id == first.id and second.name == "LINE(2)"
    assert registry.resolve(first.id) is live
