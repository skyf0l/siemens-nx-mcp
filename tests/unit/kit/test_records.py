"""kit.records: JSON and text records in string user attributes (NX 8+ calls)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import records
from tests.kit import fake_nx
from tests.kit.attributes import OldApiAttributes, UserAttributes
from tests.kit.objects import NXException, Object


class Marked(UserAttributes, Object):
    pass


class OldMarked(OldApiAttributes, Object):
    pass


class FeatureMarked(UserAttributes, Object):
    pass


@pytest.fixture
def tx():
    nx = fake_nx()
    nx.nx.NXObject = NS(AttributeType=NS(String="string"))
    nx.nx.Features = NS(Feature=FeatureMarked)
    return nx.tx


def test_json_round_trip_through_the_user_attribute_calls(tx):
    obj = Marked()
    text = records.set_json(tx, obj, "NX_MCP_DETAIL_V1", {"b": 1, "a": [1.5, "h"]}, sort_keys=True)
    assert text == '{"a": [1.5, "h"], "b": 1}'
    assert obj.attributes == {"NX_MCP_DETAIL_V1": text}
    assert obj.attribute_updates == [("SetUserAttribute", "NX_MCP_DETAIL_V1", "now")]
    assert records.get_json(tx, obj, "NX_MCP_DETAIL_V1") == {"a": [1.5, "h"], "b": 1}
    assert records.has(tx, obj, "NX_MCP_DETAIL_V1")


def test_text_round_trip_keeps_the_exact_string_including_empty(tx):
    obj = Marked()
    records.set_text(tx, obj, "NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1", "")
    assert records.get_text(tx, obj, "NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1", "x") == ""
    records.set_text(tx, obj, "NX_MCP_DRAWING_CONSTRUCTION_V1", "hide")
    records.set_text(tx, obj, "NX_MCP_DRAWING_CONSTRUCTION_V1", "show")  # replaces
    assert records.get_text(tx, obj, "NX_MCP_DRAWING_CONSTRUCTION_V1") == "show"


def test_records_the_deprecated_calls_wrote_read_back_unchanged(tx):
    obj = Marked()
    old = json.dumps({"parent": "h1", "center": [80, 0, 20], "radius": 10})
    obj.SetAttribute("NX_MCP_DETAIL_V1", old)  # as parts saved before the change hold it
    assert records.get_text(tx, obj, "NX_MCP_DETAIL_V1") == old
    assert records.get_json(tx, obj, "NX_MCP_DETAIL_V1")["radius"] == 10
    records.set_json(tx, obj, "NX_MCP_DETAIL_V1", {"radius": 5})
    assert obj.GetStringAttribute("NX_MCP_DETAIL_V1") == '{"radius": 5}'


def test_missing_attribute_gives_the_default_and_never_reads(tx):
    obj = Marked()
    assert records.get_text(tx, obj, "NX_MCP_TRACE_V1") is None
    assert records.get_json(tx, obj, "NX_MCP_TRACE_V1") is None
    assert records.get_json(tx, obj, "NX_MCP_TRACE_V1", {}) == {}
    assert not records.has(tx, obj, "NX_MCP_TRACE_V1")
    with pytest.raises(NXException):  # NX itself raises for a missing title
        obj.GetUserAttributeAsString("NX_MCP_TRACE_V1", "string", -1)


def test_a_feature_is_written_without_an_update_like_the_deprecated_call(tx):
    feature = FeatureMarked()
    records.set_json(tx, feature, "NXMCP_DEFORM_RANGES", {"x": [0.0, 1.0]})
    records.delete(tx, feature, "NXMCP_DEFORM_RANGES")
    assert feature.attribute_updates == [
        ("SetUserAttribute", "NXMCP_DEFORM_RANGES", "later"),
        ("DeleteUserAttribute", "NXMCP_DEFORM_RANGES", "later"),
    ]


def test_delete_removes_only_a_present_record(tx):
    obj = Marked()
    assert records.delete(tx, obj, "NX_MCP_MEASURED_PMI_V1") is False
    records.set_text(tx, obj, "NX_MCP_MEASURED_PMI_V1", "disabled")
    assert records.delete(tx, obj, "NX_MCP_MEASURED_PMI_V1") is True
    assert not records.has(tx, obj, "NX_MCP_MEASURED_PMI_V1")
    assert obj.attribute_updates[-1] == ("DeleteUserAttribute", "NX_MCP_MEASURED_PMI_V1", "now")


def test_objects_without_the_user_attribute_calls_use_the_deprecated_ones(tx):
    obj = OldMarked()
    assert not hasattr(obj, "SetUserAttribute")
    records.set_json(tx, obj, "NX_MCP_TRACE_V1", [{"edge": "h2", "percent": 50.0}])
    assert obj.attributes == {"NX_MCP_TRACE_V1": '[{"edge": "h2", "percent": 50.0}]'}
    assert records.get_json(tx, obj, "NX_MCP_TRACE_V1") == [{"edge": "h2", "percent": 50.0}]
    assert records.delete(tx, obj, "NX_MCP_TRACE_V1")
    assert records.get_text(tx, obj, "NX_MCP_TRACE_V1", "none") == "none"
