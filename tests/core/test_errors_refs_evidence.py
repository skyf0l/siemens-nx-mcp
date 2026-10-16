"""Error taxonomy, references and evidence gating."""

import json

import pytest

from nx_mcp.core import errors, evidence
from nx_mcp.core.errors import NXError, classify, native
from nx_mcp.core.references import Counter, References
from tests.core.fakes import FakeServices


def test_taxonomy_has_22_categories_and_72_codes():
    assert len(errors.CATEGORIES) == 22 and len(errors.CODES) == 72  # + NX_BUSY, NX_ENGINEER_PART
    assert all(c.startswith("NX_") for c in errors.CODES)


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
    # A module or token checkout refused under another code is a license refusal too.
    message = "License Error, The check out of a module failed because not enough tokens"
    with pytest.raises(NXError) as e, native(default="NX_BUILD_FAILED"):
        raise NativeError(3520099, message)
    assert e.value.code == "NX_LICENSE_UNAVAILABLE" and e.value.nx_code == 3520099
    with pytest.raises(NXError) as e, native({670015: "NX_BUILD_FAILED"}):
        raise NativeError(670015, "radius too large")
    assert e.value.code == "NX_BUILD_FAILED" and e.value.message == "radius too large"
    with pytest.raises(NXError) as e, native(default="NX_BUILD_FAILED"):
        raise NativeError(12)
    assert e.value.code == "NX_BUILD_FAILED" and e.value.nx_code == 12
    with pytest.raises(NXError) as e, native(default="NX_BUILD_FAILED"):
        raise KeyError("k")
    assert e.value.code == "NX_ERROR" and "KeyError" in e.value.message


def test_an_update_failure_nx_undid_is_update_failed():
    # The host's update-failure action is Undo (host/quiet.py): NX raises 670024.
    error = classify(NativeError(670024, "Update undo happened."), default="NX_ERROR")
    assert error.code == "NX_UPDATE_FAILED" and error.nx_code == 670024
    assert error.category == "geometry_failed" and "undid" in error.message


def test_traceback_only_in_dev_and_never_in_the_envelope(monkeypatch):
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    plain = classify(ValueError("x"))
    assert "traceback" not in plain.details and plain.trace is None
    monkeypatch.setenv("NX_MCP_DEV", "1")
    dev = classify(ValueError("x"))
    assert "traceback" not in dev.details and "ValueError" in (dev.trace or "")
    assert "traceback" not in json.dumps(dev.error_dict())


def test_fail_can_override_retry_for_one_error():
    with pytest.raises(NXError) as e:
        errors.fail("NX_NOT_FOUND", "nothing to undo", retry="never")
    assert e.value.category == "not_found" and e.value.retry == "never"
    assert e.value.error_dict()["retry"] == "never" and not e.value.retryable
    assert NXError("NX_NOT_FOUND", "x").retry == "after_fix"
    with pytest.raises(ValueError):
        errors.fail("NX_NOT_FOUND", "x", retry="sometimes")


def test_advisory_warning_codes_and_not_running():
    assert {
        "NX_VIEW_OVERLAP",
        "NX_NO_EFFECT",
        "NX_NOTE",
        "NX_TARGET_UNCHANGED",
    } <= errors.WARNING_CODES
    assert NXError("NX_NOT_RUNNING", "done").retry == "never"


def test_unlisted_codes_become_nx_error():
    stale = NXError("NX_STALE_REFERENCE", "gone", nx_code=5)
    assert classify(stale) is stale and stale.retry == "after_reacquire" and stale.retryable
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
    handle = svc.references.entry(row["id"]).handle
    assert svc.references.resolve(handle, ("feature",)) is feature  # obj_ registry handle
    restarted = References(svc, Counter(tmp_path / ".nx-mcp" / "refs.json"))
    with pytest.raises(NXError) as e:
        restarted.resolve(row["id"], ("feature",))
    assert e.value.code == "NX_STALE_REFERENCE"
    with pytest.raises(NXError) as e:
        restarted.resolve("f:999999", ())
    assert e.value.code == "NX_NOT_FOUND"


def test_a_wrapper_nx_invalidated_is_stale_until_listed_again(tmp_path):
    # NX 2406: a wrapper of an object NX deleted reports tag 0; NX refuses it with "First
    # parameter is invalid. Expecting int type, found str" (seen live on a closed part).
    from tests.core.fakes import Obj

    svc = FakeServices(tmp_path)
    old = svc.part.add("feature", "EXTRUDE(1)")
    tag = old.Tag
    row = svc.references.row(old, "feature")
    handle = svc.references.entry(row["id"]).handle
    old.Tag = 0  # NX dropped the object behind this wrapper
    for ref in (row["id"], handle):
        with pytest.raises(NXError) as e:
            svc.references.resolve(ref, ("feature",))
        assert e.value.code == "NX_STALE_REFERENCE"
        assert e.value.details["reason"] == "deleted_in_nx"
    # A fresh listing hands out the live wrapper of the same NX object (same tag).
    live = Obj("feature", "EXTRUDE(1)", svc.part)
    live.Tag = tag
    assert svc.references.row(live, "feature")["id"] == row["id"]
    assert svc.references.resolve(row["id"], ("feature",)) is live
    assert svc.references.resolve(handle, ("feature",)) is live


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
    assert "nx_build" not in data["nx_x"]["v2406"]
    path = evidence.record(
        "modeling",
        "nx_x",
        "v2406",
        variants=["a"],
        scenario="acceptance/modeling/x.json",
        scenario_sha256="1",
        source_sha256="2",
        checks=3,
        recorded="2026-10-09T00:00:00Z",
        directory=tmp_path,
        nx_build="2406.9160",
    )
    assert json.loads(path.read_text())["nx_x"]["v2406"]["nx_build"] == "2406.9160"
    assert evidence.load(tmp_path)["nx_x"]["v2406"]["checks"] == 3


def test_nx_error_has_the_envelope_and_the_bridge_shape():
    with pytest.raises(NXError) as e:
        errors.fail("NX_BUILD_FAILED", "radius too large", field="/radius", nx_code=670015)
    error = e.value
    assert isinstance(error, NXError) and error.nx_code == 670015
    wire = error.as_dict()
    assert wire["status"] == "error" and wire["code"] == "NX_BUILD_FAILED"
    assert wire["nx_code"] == 670015 and wire["category"] == "geometry_failed"
    assert wire["field"] == "/radius" and wire["retryable"] is False
    assert error.error_dict()["nx_code"] == 670015 and error.error_dict()["field"] == "/radius"
    assert error.hint is None and error.error_dict()["hint"]  # category default hint


def test_development_tools_report_kit_errors_by_code(rig):
    # A kit NXError raised inside a development tool keeps its code.
    def handler():
        errors.fail("NX_WRONG_GEOMETRY", "Select a planar face", nx_code=12)

    rig.e.dev_tools["nx_test_read"] = handler
    with pytest.raises(NXError) as e:
        rig.run("nx_test_read", {})
    assert e.value.code == "NX_WRONG_GEOMETRY" and e.value.nx_code == 12
    assert e.value.details["mutation_outcome"] == "rolled_back"


def _variant_spec(effect="model"):
    from types import SimpleNamespace as NS

    return NS(
        name="nx_v",
        effect=effect,
        variants="kind",
        variant_values=lambda: ("a", "b", "c"),
        variant_of=lambda args: args.get("kind"),
    )


def test_variants_for_lists_only_tested_variants(monkeypatch):
    monkeypatch.delenv("NX_MCP_UNTESTED", raising=False)
    spec = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a", "c"]}}})
    assert evidence.variants_for(spec, "v2406") == ("a", "c")
    assert evidence.visible(spec, "v2406")
    # An untested build offers what is tested on any build (b is tested nowhere).
    assert evidence.variants_for(spec, "v2506") == ("a", "c")
    assert evidence.variants_for(spec, None) == ("a", "c")  # build unknown: untested too
    assert evidence.check(spec, "v2506", {"kind": "a"}) is None
    refused = evidence.check(spec, "v2506", {"kind": "b"})
    assert refused.code == "NX_VERSION_UNSUPPORTED" and "any NX build" in refused.message
    assert refused.details["available"] == ["a", "c"]
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": []}}})
    assert not evidence.visible(spec, "v2406")  # no variant left: the tool is hidden
    assert not evidence.visible(spec, "v2506")  # tested nowhere: hidden everywhere


def test_a_tested_build_offers_variants_tested_elsewhere(monkeypatch):
    spec = _variant_spec()
    cache = {"nx_v": {"v2406": {"variants": ["a"]}, "v2412": {"variants": ["b"]}}}
    monkeypatch.setattr(evidence, "_CACHE", cache)
    assert evidence.variants_for(spec, "v2406") == ("a", "b")
    assert evidence.available(spec, "v2406", "a") and not evidence.available(spec, "v2406", "b")
    assert evidence.tested_on(spec, "b") == ["v2412"]


def test_read_only_mode_keeps_reads_on_untested_builds(monkeypatch):
    model, read = _variant_spec(), _variant_spec("read")
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a"]}}})
    monkeypatch.setenv("NX_MCP_UNTESTED", "read_only")
    assert evidence.untested_mode() == "read_only"
    assert not evidence.visible(model, "v2506") and evidence.visible(read, "v2506")
    assert evidence.visible(model, "v2406")  # a tested build is never restricted
    refused = evidence.check(model, "v2506", {"kind": "a"})
    assert "only read and view" in refused.message
    monkeypatch.setenv("NX_MCP_UNTESTED", "offer")
    assert evidence.visible(model, "v2506")
    assert not evidence.visible(model, "v2506", read_only=True)  # what NX reported
    monkeypatch.setenv("NX_MCP_UNTESTED", "nonsense")
    assert evidence.untested_mode() == "offer"


def test_a_failed_self_test_leaves_reads_only(monkeypatch):
    monkeypatch.delenv("NX_MCP_UNTESTED", raising=False)
    model = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a"]}}})
    try:
        evidence.set_self_test({"state": "passed", "reason": None})
        assert evidence.visible(model, "v2506")
        evidence.set_self_test({"state": "failed", "reason": "the undo failed"})
        assert evidence.self_test_failed() and not evidence.visible(model, "v2506")
        assert evidence.visible(model, "v2406")
    finally:
        evidence.set_self_test(None)
    assert evidence.self_test_result() is None


def test_version_memory_refuses_for_the_session(monkeypatch):
    spec = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a", "b"]}}})
    try:
        member = "NXOpen.Features.FeatureCollection.CreateX"
        assert evidence.remember_version(spec, "2406.9160", "a", member) is None  # tested
        entry = evidence.remember_version(spec, "2506.1000", "a", member)
        assert entry["build"] == "2506.1000" and entry["member"] == member
        assert [e["variant"] for e in evidence.version_failures()] == ["a"]
        assert evidence.version_refusal(spec, {"kind": "b"}) is None
        refusal = evidence.version_refusal(spec, {"kind": "a"})
        assert refusal.code == "NX_VERSION_UNSUPPORTED" and refusal.retry == "never"
        assert "NX 2506.1000" in refusal.message and refusal.details["member"] == member
        fresh = evidence.version_unsupported(spec, "2506.1000", "a", member)
        assert fresh.message.endswith("nothing changed") and "Do not retry" in fresh.hint
    finally:
        evidence.forget_versions()
    assert evidence.version_failures() == []


def test_build_labels():
    assert evidence.build_label("2406.9160") == "NX 2406.9160"
    assert evidence.build_label("v2412") == "NX 2412"
    assert evidence.build_label("NX 2506") == "NX 2506"
    assert evidence.build_label("NX build unknown") == "NX (build unknown)"
    assert evidence.build_label(None) == "NX (build unknown)"


def test_missing_nxopen_members_are_named():
    import types as pytypes

    nxopen = pytypes.ModuleType("NXOpen.Features")
    builder = type("BlockFeatureBuilder", (), {"__module__": "NXOpen.Features"})
    try:
        builder().Foo  # noqa: B018
    except AttributeError as error:
        assert errors.nxopen_member(error) == "NXOpen.Features.BlockFeatureBuilder.Foo"
        assert errors.classify(error).details["nx_member"].endswith(".Foo")
    try:
        nxopen.Bar  # noqa: B018
    except AttributeError as error:
        assert errors.nxopen_member(error) == "NXOpen.Features.Bar"
    try:
        object().foo  # noqa: B018
    except AttributeError as error:
        assert errors.nxopen_member(error) is None  # not NXOpen: never a version problem
    try:
        try:
            builder.Baz  # noqa: B018
        except AttributeError as missing:
            raise errors.classify(missing) from missing
    except NXError as wrapped:
        assert errors.missing_member(wrapped) == "NXOpen.Features.BlockFeatureBuilder.Baz"
    assert errors.missing_member(ValueError("x")) is None


def test_license_memory_keeps_tested_variants_only(monkeypatch):
    spec = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a"]}}})
    error = errors.NXError("NX_LICENSE_UNAVAILABLE", "no seat", nx_code=948801)
    assert evidence.remember_license(spec, "NX 2406", {"kind": "b"}, error) is None  # untested
    entry = evidence.remember_license(spec, "NX 2406", {"kind": "a"}, error)
    assert entry["variant"] == "a" and entry["nx_code"] == 948801
    other = errors.NXError("NX_INVALID_ARGUMENT", "bad")
    assert evidence.remember_license(spec, "NX 2406", {"kind": "a"}, other) is None
    assert evidence.license_refusal(spec, {"kind": "c"}) is None
    refusal = evidence.license_refusal(spec, {"kind": "a"})
    assert refusal.code == "NX_LICENSE_UNAVAILABLE" and refusal.nx_code == 948801
    assert "nx_v#a" in refusal.message and "remembered from" in refusal.message
    # A remembered refusal is never remembered again; memory survives a host reload.
    assert evidence.remember_license(spec, "NX 2406", {"kind": "a"}, refusal) is None
    import sys

    holder = sys.modules[evidence._MEMORY_MODULE]  # outside nx_mcp: a reload keeps it
    assert not holder.__name__.startswith("nx_mcp")
    assert [e["variant"] for e in evidence.licenses()] == ["a"]
    evidence.forget_licenses()
    assert evidence.licenses() == []


def test_license_memory_keeps_the_license_feature(monkeypatch):
    spec = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a"]}}})
    error = errors.NXError("NX_LICENSE_UNAVAILABLE", "no seat", nx_code=948801,
                           details={"license": "nx_freeform_1"})  # fmt: skip
    entry = evidence.remember_license(spec, "NX 2406", {"kind": "a"}, error)
    assert entry["license"] == "nx_freeform_1"
    refusal = evidence.license_refusal(spec, {"kind": "a"})
    assert refusal.details["license"] == "nx_freeform_1" and "(nx_freeform_1)" in refusal.message
    assert evidence.licenses()[0]["license"] == "nx_freeform_1"  # nx_get_status rows
    evidence.forget_licenses()


def test_license_memory_covers_tools_offered_on_an_untested_build(monkeypatch):
    monkeypatch.delenv("NX_MCP_UNTESTED", raising=False)
    spec = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a"]}}})
    error = errors.NXError("NX_LICENSE_UNAVAILABLE", "no seat", nx_code=948801)
    entry = evidence.remember_license(spec, "NX 2506", {"kind": "a"}, error)  # tested elsewhere
    assert entry is not None and entry["variant"] == "a"
    assert evidence.license_refusal(spec, {"kind": "a"}) is not None  # not retried forever
    assert evidence.remember_license(spec, "NX 2506", {"kind": "b"}, error) is None  # nowhere
    by_argument = errors.NXError("NX_LICENSE_UNAVAILABLE", "no seat",
                                 details={"argument_license": True})  # fmt: skip
    evidence.forget_licenses()
    assert evidence.remember_license(spec, "NX 2506", {"kind": "a"}, by_argument) is None
    evidence.forget_licenses()


def test_a_license_refusal_names_the_feature_nx_names():
    named = classify(NativeError(948801, 'License Error: feature "nx_sheet_metal" unavailable'))
    assert named.code == "NX_LICENSE_UNAVAILABLE" and named.details["license"] == "nx_sheet_metal"
    plain = classify(NativeError(948801, "License Error, its token feature does not exist."))
    assert "license" not in plain.details


def test_license_memory_and_gate_3_use_the_default_variant(monkeypatch):
    from tests.core.test_catalogue import nx_create_kind
    from tests.core.test_catalogue import spec as make_spec

    spec = make_spec(nx_create_kind, variants="kind", example={"size": 1})
    monkeypatch.setattr(evidence, "_CACHE", {spec.name: {"v2406": {"variants": ["b"]}}})
    assert evidence.check(spec, "v2406", {"size": 1}) is None  # default b is tested
    monkeypatch.setattr(evidence, "_CACHE", {spec.name: {"v2406": {"variants": ["a"]}}})
    assert evidence.check(spec, "v2406", {"size": 1}).code == "NX_VERSION_UNSUPPORTED"
    monkeypatch.setattr(evidence, "_CACHE", {spec.name: {"v2406": {"variants": ["a", "b"]}}})
    error = errors.NXError("NX_LICENSE_UNAVAILABLE", "no seat", nx_code=948801)
    try:
        entry = evidence.remember_license(spec, "NX 2406", {"size": 1}, error)
        assert entry["variant"] == "b"  # not None, which would block every variant
        assert evidence.license_refusal(spec, {"size": 1, "kind": "a"}) is None
        assert evidence.license_refusal(spec, {"size": 1}) is not None
    finally:
        evidence.forget_licenses()


def test_a_remembered_license_refusal_expires(monkeypatch):
    # A token pool used up for a moment may be free again: NX is asked again later.
    spec = _variant_spec()
    monkeypatch.setattr(evidence, "_CACHE", {"nx_v": {"v2406": {"variants": ["a"]}}})
    error = errors.NXError("NX_LICENSE_UNAVAILABLE", "no tokens", nx_code=948801)
    entry = evidence.remember_license(spec, "NX 2406", {"kind": "a"}, error)
    assert evidence.license_refusal(spec, {"kind": "a"}) is not None
    clock = entry["at"] + evidence.LICENSE_MEMORY_SECONDS + 1
    monkeypatch.setattr(evidence.time, "time", lambda: clock)
    assert evidence.license_refusal(spec, {"kind": "a"}) is None
    assert evidence.licenses() == []
