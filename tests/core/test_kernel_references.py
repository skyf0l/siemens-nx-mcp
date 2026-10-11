"""Refs survive mutations, receipts keep arguments, gate 2 completes paths (kernel)."""

import json

import pytest

from nx_mcp.core import kernel, receipts
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import NXError, classify
from nx_mcp.core.references import article
from tests.core import tools
from tests.core.fakes import FakePart, FakeServices, Obj

SPECS = tools.specs()


@pytest.fixture
def svc(tmp_path):
    return FakeServices(tmp_path)


def run(svc, tool, operation_id=None, **args):
    return kernel.run(SPECS[tool], args, kernel.Context(svc, operation_id=operation_id))


def test_a_topology_change_keeps_refs_of_faces_and_edges_that_survive(svc):
    face, edge = svc.part.add("face", "TOP"), svc.part.add("edge", "E1")
    face_ref = svc.references.row(face, "face")["id"]
    edge_ref = svc.references.row(edge, "edge")["id"]
    ok = run(svc, "nx_create_block", size=1)  # invalidates="topology"
    assert ok["ok"] and ok["changes"]["references"] == "preserved"
    assert "stale_kinds" not in ok["changes"] and "stale_refs" not in ok["changes"]
    assert svc.references.resolve(face_ref, ("face",)) is face
    assert svc.references.resolve(edge_ref, ("edge",)) is edge


def test_a_consumed_face_is_listed_stale_and_its_hint_says_find_it_again(svc):
    face, keep = svc.part.add("face", "SIDE"), svc.part.add("face", "TOP")
    face_ref = svc.references.row(face, "face")["id"]
    keep_ref = svc.references.row(keep, "face")["id"]

    def nx_delete_face(tx, *, size: tools.Length) -> tools.Count:
        """Consume one face."""
        tx.part.objects.remove(face)
        return {"count": 1}

    spec = make_spec(nx_delete_face, effect="model", label="Consume", keywords=tools.KEYWORDS,
                     domain="modeling", example={"size": 1})  # fmt: skip
    ok = kernel.run(spec, {"size": 1}, kernel.Context(svc))
    assert ok["changes"]["references"] == "listed"
    assert ok["changes"]["stale_refs"] == [face_ref]
    assert svc.references.resolve(keep_ref, ("face",)) is keep
    with pytest.raises(NXError) as error:
        svc.references.resolve(face_ref, ("face",))
    assert error.value.code == "NX_STALE_REFERENCE" and "nx_find_geometry" in error.value.hint


def test_a_refused_call_does_not_move_the_revision(svc):
    before = svc.epoch
    refused = run(svc, "nx_create_block", size=-1)
    assert refused["mutation_outcome"] == "not_started" and svc.epoch == before
    failed = run(svc, "nx_create_block", size=1, fail_with="NX_BUILD_FAILED")
    assert failed["mutation_outcome"] == "rolled_back" and svc.epoch == before + 1


def test_tx_modified_rows_join_changes_modified(svc):
    feature = svc.part.add("feature", "EDITED")

    def nx_edit_feature(tx, *, size: tools.Length) -> tools.Count:
        """Edit a feature."""
        tx.modified(feature, "feature")
        tx.modified(feature, "feature")  # reported once
        return {"count": 1}

    spec = make_spec(nx_edit_feature, effect="model", label="Edit", keywords=tools.KEYWORDS,
                     domain="modeling", invalidates="deleted", example={"size": 1})  # fmt: skip
    ok = kernel.run(spec, {"size": 1}, kernel.Context(svc))
    assert [r["name"] for r in ok["changes"]["modified"]] == ["EDITED"]


def test_units_follow_a_work_part_the_call_changed(svc, tmp_path):
    def nx_activate_part(tx, *, size: tools.Length) -> tools.Count:
        """Make an inch part the work part."""
        svc.part = FakePart(tmp_path / "inch.prt", "in")
        return {"count": 1}

    spec = make_spec(nx_activate_part, effect="session", label="Switch", keywords=tools.KEYWORDS,
                     domain="parts_files", example={"size": 1})  # fmt: skip
    ok = kernel.run(spec, {"size": 1}, kernel.Context(svc))
    assert ok["ok"] and ok["units"]["length"] == "in"


def test_receipts_keep_the_arguments_without_file_contents(svc):
    ok = run(svc, "nx_create_block", operation_id="op_with_args_1", size=2, name="PLATE")
    record = json.loads((svc.root / ".nx-mcp" / "operations" / "op_with_args_1.json").read_text())
    assert ok["ok"] and record["arguments"] == {"size": 2, "name": "PLATE"}
    copy = receipts.redact({"path": "a.txt", "content": "x" * 50, "note": "y" * 1000})
    assert copy["content"] == "<file content omitted: 50 characters>"
    assert copy["path"] == "a.txt" and len(copy["note"]) < 500
    assert "omitted" in receipts.redact({"items": ["z" * 300] * 100})


def test_native_tracebacks_stay_out_of_the_envelope(monkeypatch):
    class Native(Exception):
        ErrorCode = 671473

    monkeypatch.setenv("NX_MCP_DEV", "1")
    try:
        raise Native("preview failed")
    except Native as exc:
        error = classify(exc, default="NX_BUILD_FAILED")
    assert error.nx_code == 671473 and "traceback" not in error.details
    assert error.trace and "preview failed" in error.trace
    assert "dimensions" not in error.error_dict()["hint"]


def test_paths_without_a_suffix_get_the_markers_suffix(svc):
    ok = run(svc, "nx_export_note", path="notes/readme")
    assert ok["ok"] and (svc.root / "notes" / "readme.txt").is_file()
    again = run(svc, "nx_export_note", path="notes/readme")
    error = again["error"]
    assert error["code"] == "NX_ALREADY_EXISTS"
    assert error["details"] == {"path": "notes/readme.txt", "suggested_path": "notes/readme-2.txt"}
    assert "notes/readme-2.txt" in error["hint"]
    wrong = run(svc, "nx_export_note", path="a.csv")["error"]
    assert wrong["code"] == "NX_UNSUPPORTED_FILE_TYPE" and "End the path with .txt" in wrong["hint"]


def test_anchors_resolve_like_refs_and_stale_part_refs_list_loaded_parts(svc, tmp_path):
    feature = svc.part.add("feature", "BOSS", jid="EXTRUDE(3)")
    assert svc.references.resolve("@a.prt:EXTRUDE(3)", ("feature",)) is feature
    with pytest.raises(NXError) as missing:
        svc.references.resolve("@a.prt:EXTRUDE(9)", ("feature",))
    assert missing.value.code == "NX_NOT_FOUND"
    part_ref = svc.references.row(svc.part, "part")["id"]
    svc.registry.invalidate_part(svc.part_key(svc.part))
    other = FakePart(tmp_path / "b.prt")
    svc.extra_parts.append(other)
    with pytest.raises(NXError) as stale:
        svc.references.resolve(part_ref, ("part",))
    assert "nx_list_parts" in stale.value.hint
    assert {r["name"] for r in stale.value.details["loaded_parts"]} == {"a", "b"}


def test_generic_entity_names_become_kind_and_ref_and_anchors_follow_renumbering(svc):
    dim = svc.part.add("dimension", "", jid="ENTITY 26 3 1")
    row = svc.references.row(dim, "dimension")
    assert row["name"] == f"dimension {row['id']}"
    feature = svc.part.add("feature", "", jid="BLEND(4)")
    first = svc.references.row(feature, "feature")
    feature.JournalIdentifier = "BLEND(3)"  # a reorder renumbered it
    again = svc.references.row(feature, "feature")
    assert again["id"] == first["id"] and again["anchor"].endswith(":BLEND(3)")
    assert article("edge") == "an" and article("face") == "a"
    with pytest.raises(NXError) as wrong:
        svc.references.resolve(again["id"], ("edge",))
    assert "is a feature" in wrong.value.message
    edge = svc.references.row(Obj("edge", "E", svc.part), "edge")["id"]
    with pytest.raises(NXError) as wrong:
        svc.references.resolve(edge, ("face",))
    assert "is an edge" in wrong.value.message


def test_work_part_resolution_takes_the_work_assemblys_components_only(svc, tmp_path):
    from nx_mcp.core.tx import Tx

    tx = Tx(svc, SPECS["nx_create_block"], operation_id="op_testtest")
    component = svc.part.add("component", "BOLT")
    component.IsOccurrence = True  # NX components are occurrences
    occurrence = svc.part.add("body", "BOLT body")
    occurrence.IsOccurrence = True
    elsewhere = FakePart(tmp_path / "other.prt").add("feature", "THERE")
    assert tx.resolve(svc.references.row(component, "component")["id"], work_part=True) is component
    for obj, kind in ((occurrence, "body"), (elsewhere, "feature")):
        with pytest.raises(NXError) as refused:
            tx.resolve(svc.references.row(obj, kind)["id"], work_part=True)
        assert refused.value.code == "NX_WRONG_OWNER"


def _refused(svc, ref, kinds=()):
    with pytest.raises(NXError) as caught:
        svc.references.resolve(ref, kinds)
    return caught.value


def test_unknown_refs_say_whether_they_were_never_issued(svc, tmp_path):
    edge = svc.part.add("edge", "E1")
    edge_ref = svc.references.row(edge, "edge")["id"]
    number = int(edge_ref.split(":")[1])
    # Issued in this session as an edge: the face prefix names the edge ref instead.
    wrong = _refused(svc, f"fc:{number}")
    assert wrong.code == "NX_NOT_FOUND" and wrong.details["issued_as"] == edge_ref
    assert "never issued" in wrong.message and edge_ref in wrong.hint
    # A number not issued yet: never issued, with how to get refs.
    ahead = _refused(svc, f"fc:{number + 500}", ("face",))
    assert ahead.code == "NX_NOT_FOUND" and ahead.details["reason"] == "never_issued"
    assert "from nx_find_geometry" in ahead.hint and "gone" not in ahead.hint
    # No kind has this prefix.
    assert _refused(svc, "zz:5").code == "NX_INVALID_ARGUMENT"
    # Below a new session's floor: an earlier session's ref or an invented one, said so.
    later = FakeServices(tmp_path)
    old = _refused(later, edge_ref)
    assert old.code == "NX_STALE_REFERENCE" and old.details["reason"] == "not_in_this_session"
    assert "no tool returned never did" in old.message and "earlier NX session" not in old.message
