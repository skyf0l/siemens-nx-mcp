"""Error taxonomy, references and evidence gating."""

import json

import pytest

from nx_mcp.core import errors, evidence
from nx_mcp.core.errors import NXError, NXToolError, classify, native
from nx_mcp.core.references import Counter, References
from tests.core.fakes import FakeServices


def test_taxonomy_has_22_categories_and_66_codes():
    assert len(errors.CATEGORIES) == 22 and len(errors.CODES) == 66
    assert all(c.startswith("NX_") for c in errors.CODES)
    assert set(errors.LEGACY_CODES.values()) <= set(errors.CODES)


class NativeError(Exception):
    def __init__(self, code, message="native"):
        super().__init__(message)
        self.ErrorCode = code


def test_native_maps_license_listed_and_default_codes():
    for code in (948801, 949895):
        with pytest.raises(NXError) as e, native():
            raise NativeError(code)
        assert e.value.code == "NX_LICENSE_UNAVAILABLE" and e.value.nx_code == code
        assert e.value.category == "unavailable" and e.value.retry == "never"
    with pytest.raises(NXError) as e, native({670015: "NX_BUILD_FAILED"}):
        raise NativeError(670015, "radius too large")
    assert e.value.code == "NX_BUILD_FAILED" and e.value.message == "radius too large"
    with pytest.raises(NXError) as e, native(default="NX_BUILD_FAILED"):
        raise NativeError(12)
    assert e.value.code == "NX_BUILD_FAILED" and e.value.nx_code == 12
    with pytest.raises(NXError) as e, native(default="NX_BUILD_FAILED"):
        raise KeyError("k")
    assert e.value.code == "NX_ERROR" and "KeyError" in e.value.message


def test_traceback_only_in_dev(monkeypatch):
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    assert "traceback" not in classify(ValueError("x")).details
    monkeypatch.setenv("NX_MCP_DEV", "1")
    assert "traceback" in classify(ValueError("x")).details


def test_legacy_errors_map_to_new_codes():
    mapped = classify(NXToolError("NX_OBJECT_STALE", "gone", nx_code=5))
    assert mapped.code == "NX_STALE_REFERENCE" and mapped.retry == "after_reacquire"
    assert mapped.details["legacy_code"] == "NX_OBJECT_STALE" and mapped.nx_code == 5
    unknown = NXError("NX_MADE_UP", "x")
    assert unknown.code == "NX_ERROR" and unknown.details["unlisted_code"] == "NX_MADE_UP"
    shape = NXError("NX_WRONG_KIND", "m", field="/x").error_dict()
    assert set(shape) == {
        "category",
        "code",
        "message",
        "hint",
        "field",
        "retry",
        "nx_code",
        "details",
    }


def test_counter_floor_survives_restart(tmp_path):
    path = tmp_path / "refs.json"
    first = Counter(path, block=5)
    issued = [first.next() for _ in range(7)]
    assert issued == list(range(1, 8))
    second = Counter(path, block=5)
    assert second.floor == 11 and second.next() == 11  # never reused


def test_refs_from_earlier_sessions_are_stale_and_kinds_checked(tmp_path):
    svc = FakeServices(tmp_path)
    feature = svc.part.add("feature", "EXTRUDE(1)", jid="EXTRUDE(1)")
    row = svc.references.row(feature, "feature")
    assert row["id"].startswith("f:") and row["part"].startswith("p:")
    assert row["anchor"] == "@a.prt:EXTRUDE(1)"
    assert svc.references.resolve(row["id"], ("feature",)) is feature
    assert svc.references.row(feature, "feature")["id"] == row["id"]  # stable
    with pytest.raises(NXError) as e:
        svc.references.resolve(row["id"], ("body",))
    assert e.value.code == "NX_WRONG_KIND"
    legacy = svc.references.entry(row["id"]).legacy_id
    assert svc.references.resolve(legacy, ("feature",)) is feature  # obj_ spelling
    restarted = References(svc, Counter(tmp_path / ".nx-mcp" / "refs.json"))
    with pytest.raises(NXError) as e:
        restarted.resolve(row["id"], ("feature",))
    assert e.value.code == "NX_STALE_REFERENCE"
    with pytest.raises(NXError) as e:
        restarted.resolve("f:999999", ())
    assert e.value.code == "NX_NOT_FOUND"


def test_datum_group_and_body_anchor_index(tmp_path):
    svc = FakeServices(tmp_path)
    plane = svc.part.add("datum_plane", "DATUM_PLANE(2)")
    ref = svc.references.row(plane, "datum_plane")["id"]
    assert ref.startswith("d:")
    assert svc.references.resolve(ref, ("datum",)) is plane
    a = svc.part.add("body", jid="EXTRUDE(4)")
    b = svc.part.add("body", jid="EXTRUDE(4)")
    assert svc.references.row(b, "body")["anchor"] == "@a.prt:EXTRUDE(4)#1"
    assert svc.references.row(a, "body")["anchor"] == "@a.prt:EXTRUDE(4)#0"


def test_build_keys_and_evidence_record(tmp_path, monkeypatch):
    assert evidence.build_key("v2406") == "v2406"
    assert evidence.build_key("NX 2406.1700") == "v2406"
    assert evidence.build_key("NX build unknown") is None
    path = evidence.record(
        "modeling",
        "nx_x",
        "v2406",
        variants=["b", "a"],
        scenario="acceptance/modeling/x.json",
        scenario_sha256="1",
        source_sha256="2",
        checks=3,
        recorded="2026-10-09T00:00:00Z",
        directory=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["nx_x"]["v2406"]["variants"] == ["a", "b"]
    assert evidence.load(tmp_path)["nx_x"]["v2406"]["checks"] == 3


def test_nx_error_is_a_legacy_error_with_both_shapes():
    with pytest.raises(NXToolError) as e:
        errors.fail("NX_BUILD_FAILED", "radius too large", field="/radius", nx_code=670015)
    error = e.value
    assert isinstance(error, NXError) and error.nx_code == 670015
    legacy = error.as_dict()
    assert legacy["status"] == "error" and legacy["code"] == "NX_BUILD_FAILED"
    assert legacy["nx_code"] == 670015 and legacy["category"] == "geometry_failed"
    assert error.error_dict()["nx_code"] == 670015 and error.error_dict()["field"] == "/radius"
    assert error.hint is None and error.error_dict()["hint"]  # category default hint


def test_development_tools_report_kit_errors_by_code(rig):
    # A kit NXError raised inside a development tool keeps its code.
    def handler():
        errors.fail("NX_WRONG_GEOMETRY", "Select a planar face", nx_code=12)

    rig.e.dev_tools["nx_test_read"] = handler
    with pytest.raises(NXToolError) as e:
        rig.run("nx_test_read", {})
    assert e.value.code == "NX_WRONG_GEOMETRY" and e.value.nx_code == 12
    assert e.value.details["mutation_outcome"] == "rolled_back"
