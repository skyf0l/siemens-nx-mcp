"""Gate 3t (core/target.py) through the real kernel on the fake kit: the ``part``,
``expires_at`` call keys, the target rule (a call without refs and without ``part``
is ``NX_AMBIGUOUS_PART`` while two or more of the agent's parts are loaded), its P0
binding (a ``part`` that is not the work part is refused), ``envelope.part``, the
two-parts note, the ghost guard and the per-part operation_id fingerprint with receipts
written before target parts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TypedDict

import pytest

from nx_mcp.core import catalogue, kernel, receipts, target
from nx_mcp.core.catalogue import CatalogueError, make_spec
from nx_mcp.core.errors import NXError
from nx_mcp.core.tx import Tx
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Part
from tests.kit.parts import with_parts
from tests.unit.modeling.fakes import feature, modeling_nx, new_body

BLOCK = {"length": 40, "width": 20, "height": 10}


def _block_nx():
    """A modeling fake whose block builder makes a BLOCK feature in the work part."""
    nx = modeling_nx()
    with_parts(nx, services(nx).root)
    nx.part.FullPath = str(Path(services(nx).root) / "work.prt")  # inside the workspace

    def make(b):
        corner, *sizes = b.SetOriginAndLengths.calls[0][0]
        box = [corner.X, corner.Y, corner.Z] + [
            getattr(corner, a) + float(sizes[i]) for i, a in enumerate("XYZ")
        ]
        return feature(nx, "BLOCK", [new_body(nx, box)])

    nx.on_commit("BlockFeatureBuilder", make)
    return nx


def _second(nx, rel="kitB/plate.prt"):
    """Another loaded top-level part; the work and display part stay as they were."""
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    other = Part(nx.session, Path(services(nx).root) / rel)
    parts.Work, parts.Display = work, display
    return other


def _show(nx, part):
    nx.session.Parts.Work = nx.session.Parts.Display = part


def _ref(nx, part):
    return services(nx).references.row(part, "part")["id"]


def _count(nx):
    return len(nx.part.Features)


# ---------------------------------------------------------------------------
# One top-level part: today's behaviour


def test_one_part_without_keys_runs_as_before_and_reports_no_part():
    nx = _block_nx()
    r = run(nx, "nx_create_primitive", **BLOCK)
    assert r.ok, r
    assert "part" not in r.envelope and _count(nx) == 1


def test_a_loaded_component_part_counts():
    nx = _block_nx()
    component = _second(nx, "kitA/pin.prt")
    nx.attach(nx.part, component)
    assert target.roots(services(nx)) == [nx.part]  # one top-level part, two loaded
    r = run(nx, "nx_create_primitive", **BLOCK)
    assert r.error["code"] == "NX_AMBIGUOUS_PART" and _count(nx) == 0, r
    assert {row["file"] for row in r.error["details"]["loaded_parts"]} == {"work.prt", "pin.prt"}
    r = run(nx, "nx_create_primitive", part="work", **BLOCK)
    assert r.ok and r.part["from"] == "part" and _count(nx) == 1, r


def test_part_naming_the_work_part_runs_and_reports_it():
    nx = _block_nx()
    r = run(nx, "nx_create_primitive", part="work", **BLOCK)
    assert r.ok, r
    assert r.part == {"id": _ref(nx, nx.part), "file": "work.prt", "from": "part"}


# ---------------------------------------------------------------------------
# Two or more loaded parts


def test_two_or_more_parts_make_ref_less_calls_ambiguous():
    nx = _block_nx()
    plate = _second(nx)
    _second(nx, "kitC/bolt.prt")
    r = run(nx, "nx_create_primitive", **BLOCK)  # no part key, no refs
    assert r.error["code"] == "NX_AMBIGUOUS_PART" and r.mutation_outcome == "not_started", r
    assert _count(nx) == 0 and r.error["details"]["count"] == 3
    message = "3 parts are loaded and nx_create_primitive has no refs that pick one"
    assert r.error["message"] == message
    files = [row["file"] for row in r.error["details"]["loaded_parts"]]
    assert files == ["plate.prt", "bolt.prt", "work.prt"]  # the work part last
    assert run(nx, "nx_get_model_summary").error["code"] == "NX_AMBIGUOUS_PART"  # reads too
    # A ref of a part that is not the work part needs no part and runs as before.
    body = nx.block(10, 10, 10)
    _show(nx, plate)
    r = run(nx, "nx_measure_mass", bodies=[body.ref])
    assert (r.error or {}).get("code") not in ("NX_WRONG_PART_CONTEXT", "NX_AMBIGUOUS_PART"), r
    assert r.part["file"] == "work.prt" and r.part["from"] == "refs"
    _show(nx, nx.part)
    assert run(nx, "nx_create_primitive", part="work", **BLOCK).ok
    assert run(nx, "nx_create_primitive", part="work.prt", **BLOCK).ok
    assert _count(nx) == 2


def test_the_ambiguity_hint_uses_a_neutral_example():
    nx = _block_nx()
    _second(nx)
    hint = run(nx, "nx_create_primitive", **BLOCK).error["hint"]
    assert 'e.g. {"part": "<file name>"}' in hint and "nx_list_parts" in hint
    assert "work.prt" in hint and "plate.prt" in hint
    assert '"part": "work' not in hint and '"part": "plate' not in hint  # never a real part


def test_the_create_component_hint_names_the_assembly():
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_create_component", path="kitB/plate.prt")
    assert r.error["code"] == "NX_AMBIGUOUS_PART", r
    assert "assembly's file name" in r.error["hint"] and "<assembly file name>" in r.error["hint"]
    assert "plate.prt" in r.error["hint"] and "work.prt" in r.error["hint"]


def test_engineer_parts_are_not_counted():
    nx = _block_nx()
    s = services(nx)
    theirs = _second(nx, "theirs/housing.prt")
    s.foreign.add(s.part_key(theirs))
    r = run(nx, "nx_create_primitive", **BLOCK)  # one agent part: the work part as before
    assert r.ok and _count(nx) == 1, r
    assert r.part == {"id": _ref(nx, nx.part), "file": "work.prt", "from": "work"}  # 2 loaded
    status = run(nx, "nx_get_status", include=["parts"]).data["parts"]
    assert status["loaded"] == 2 and status["agent_loaded"] == 1
    _second(nx)  # a second agent part
    r = run(nx, "nx_create_primitive", **BLOCK)
    assert r.error["code"] == "NX_AMBIGUOUS_PART" and r.error["details"]["count"] == 2, r
    files = {row["file"] for row in r.error["details"]["loaded_parts"]}
    assert files == {"work.prt", "plate.prt"} and "housing.prt" not in r.error["hint"]


def _parts_nx():
    nx = fake_nx()
    with_parts(nx, services(nx).root)
    return nx


def _notes(result) -> list:
    return [w for w in result.warnings or [] if w["code"] == "NX_NOTE"]


def test_the_load_that_brings_two_parts_carries_a_note():
    nx = _parts_nx()
    note = {"code": "NX_NOTE", "message": "2 parts are loaded: calls without refs now take part"}
    second = run(nx, "nx_create_part", path="b.prt", display=False)
    assert second.ok and _notes(second) == [note], second
    third = run(nx, "nx_create_part", path="c.prt", display=False)
    assert third.ok and not _notes(third), third
    # An engineer's part does not count towards the note.
    engineer = _parts_nx()
    services(engineer).foreign.add(services(engineer).part_key(engineer.part))
    mine = run(engineer, "nx_create_part", path="mine.prt", display=False)
    assert mine.ok and not _notes(mine), mine


def test_two_parts_and_no_hint_is_ambiguous_listing_the_files():
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_create_primitive", operation_id="amb_block_01", **BLOCK)
    assert not r.ok and r.error["code"] == "NX_AMBIGUOUS_PART", r
    assert r.mutation_outcome == "not_started" and _count(nx) == 0
    assert "work.prt" in r.error["hint"] and "plate.prt" in r.error["hint"]
    assert '"part": "' in r.error["hint"]
    files = {row["file"] for row in r.error["details"]["loaded_parts"]}
    assert files == {"work.prt", "plate.prt"}
    # A refusal before gate 7 writes no receipt: the same id runs later.
    assert services(nx).store.get("amb_block_01")["state"] == "unknown"


@pytest.mark.parametrize(
    "key", ["work", "work.prt", "WORK", "Work.PRT", "work.prt ", "./work.prt", ".\\work"]
)
def test_part_key_forms_name_the_part(key):
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_create_primitive", part=key, **BLOCK)
    assert r.ok, (key, r)
    assert r.part["file"] == "work.prt" and r.part["from"] == "part" and _count(nx) == 1


def test_part_key_as_p_ref_and_absolute_workspace_path():
    nx = _block_nx()
    _second(nx)
    assert run(nx, "nx_create_primitive", part=_ref(nx, nx.part), **BLOCK).ok
    assert run(nx, "nx_create_primitive", part=str(nx.part.FullPath), **BLOCK).ok
    assert _count(nx) == 2


def test_part_naming_another_part_is_refused_naming_both_parts():
    nx = _block_nx()
    plate = _second(nx)
    r = run(nx, "nx_create_primitive", part="plate", **BLOCK)
    assert not r.ok and r.error["code"] == "NX_WRONG_PART_CONTEXT", r
    assert r.mutation_outcome == "not_started" and _count(nx) == 0
    assert "plate.prt" in r.error["message"] and "work.prt" in r.error["message"]
    assert r.error["field"] == "/part" and _ref(nx, plate) in r.error["hint"]
    assert r.part["file"] == "plate.prt"


def test_dotted_stem_is_its_own_file_never_the_shorter_one():
    nx = _block_nx()
    _second(nx, "kitA/bracket.prt")
    v2 = _second(nx, "kitA/bracket.v2.prt")
    _show(nx, v2)
    r = run(nx, "nx_create_primitive", part="bracket.v2", **BLOCK)
    # bracket.v2.prt is the target (P0: not the work part's file -> no build anywhere else)
    assert r.part["file"] == "bracket.v2.prt", r
    _show(nx, nx.part)
    r = run(nx, "nx_create_primitive", part="bracket.v2", **BLOCK)
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.part["file"] == "bracket.v2.prt"


def test_dotted_stem_alone_is_found():
    nx = _block_nx()
    v2 = _second(nx, "kitA/bracket.v2.prt")
    found = target.part_from_text(services(nx), "bracket.v2")
    assert found is v2


def test_a_path_outside_the_workspace_is_refused_not_matched_by_stem():
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_create_primitive", part="C:/elsewhere/project/work.prt", **BLOCK)
    assert not r.ok and r.error["code"] == "NX_PATH_OUTSIDE_WORKSPACE", r
    assert r.error["field"] == "/part" and _count(nx) == 0


@pytest.mark.parametrize("bad", [5, ["work"], {"name": "work"}, "", "   ", True, "x" * 300])
def test_malformed_part_key_is_an_invalid_argument(bad):
    nx = _block_nx()
    r = run(nx, "nx_create_primitive", part=bad, **BLOCK)
    assert not r.ok and r.error["code"] == "NX_INVALID_ARGUMENT", r
    assert r.error["field"] == "/part" and r.mutation_outcome == "not_started"


@pytest.mark.parametrize("bad", [5, "", "two words", "x" * 40])
def test_agent_is_not_a_call_key(bad):
    nx = _block_nx()
    r = run(nx, "nx_create_primitive", agent=bad, **BLOCK)  # an unknown argument, as before
    assert r.error["code"] == "NX_INVALID_ARGUMENT" and r.error["field"] == "/agent", r
    assert "agent" not in target.CALL_KEYS


def test_unknown_file_name_lists_the_loaded_files():
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_create_primitive", part="plat", **BLOCK)
    assert r.error["code"] == "NX_NOT_FOUND" and r.error["field"] == "/part", r
    assert "work.prt" in r.error["hint"] and "plate.prt" in r.error["hint"]
    assert {row["file"] for row in r.error["details"]["loaded_parts"]} == {"work.prt", "plate.prt"}


def test_closed_parts_p_ref_is_stale_with_the_loaded_parts():
    nx = _block_nx()
    plate = _second(nx)
    ref = _ref(nx, plate)
    nx.session.Parts.remove(plate)
    services(nx).forget_parts([services(nx).part_key(plate)])
    r = run(nx, "nx_create_primitive", part=ref, **BLOCK)
    assert r.error["code"] == "NX_STALE_REFERENCE" and r.error["field"] == "/part", r
    assert r.error["details"]["loaded_parts"] and "work.prt" in r.error["hint"]


def test_a_ref_of_another_kind_as_part_is_wrong_kind():
    nx = _block_nx()
    body = nx.block(1, 1, 1)
    r = run(nx, "nx_create_primitive", part=body.ref, **BLOCK)
    assert r.error["code"] == "NX_WRONG_KIND" and r.error["field"] == "/part", r


# ---------------------------------------------------------------------------
# References pin the part


def test_refs_of_the_work_part_pin_it_without_part():
    nx = _block_nx()
    _second(nx)
    made = run(nx, "nx_create_primitive", part="work", **BLOCK)
    r = run(nx, "nx_get_feature", feature=made.data["feature"]["id"])
    assert r.ok, r
    assert r.part == {"id": _ref(nx, nx.part), "file": "work.prt", "from": "refs"}


def test_part_naming_the_refs_part_that_is_not_the_work_part_is_refused():
    nx = _block_nx()
    plate = _second(nx)
    body = nx.block(10, 10, 10)
    _show(nx, plate)
    r = run(nx, "nx_measure_mass", bodies=[body.ref], part="work")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.error["field"] == "/part", r
    assert r.mutation_outcome == "not_applicable"
    assert r.part == {"id": _ref(nx, nx.part), "file": "work.prt", "from": "part"}
    assert "plate.prt" in r.error["message"] and _ref(nx, nx.part) in r.error["hint"]


def test_part_contradicting_the_refs_is_wrong_part_context():
    nx = _block_nx()
    _second(nx)
    body = nx.block(10, 10, 10)
    edge = body.edges[0]
    r = run(nx, "nx_create_blend", part="plate", edges=[nx.ref_of(edge, "edge")], radius=1)
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.error["field"] == "/part", r
    assert "Drop part (the refs pick work.prt)" in r.error["hint"]


def test_a_read_whose_part_contradicts_its_refs_is_refused():
    nx = _block_nx()
    _second(nx)
    body = nx.block(10, 10, 10)
    r = run(nx, "nx_measure_mass", bodies=[body.ref], part="plate")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.error["field"] == "/part", r
    assert r.mutation_outcome == "not_applicable"
    assert "Drop part (the refs pick work.prt)" in r.error["hint"]
    made = run(nx, "nx_create_primitive", part="work", **BLOCK)
    r = run(nx, "nx_get_feature", feature=made.data["feature"]["id"], part="plate.prt")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.error["field"] == "/part", r
    r = run(nx, "nx_get_feature", feature=made.data["feature"]["id"], part="work")
    assert r.ok and r.part["from"] == "part", r


def test_part_may_name_the_assembly_that_holds_an_occurrence_ref():
    nx = _block_nx()
    pin = _second(nx, "kitA/pin.prt")
    _second(nx)
    edge = NS(Tag=9101, IsOccurrence=True, Prototype=NS(OwningPart=pin), OwningPart=nx.part)
    edge.Name = "EDGE"
    ref = nx.ref_of(edge, "edge")
    spec = catalogue.load()["nx_create_blend"]
    checked = spec.check({"edges": [ref], "radius": 1})
    s = services(nx)
    assert target.resolve(spec, checked, s).part is pin  # the refs: the prototype's part
    assert target.resolve(spec, checked, s, part="pin").part is pin
    assert target.resolve(spec, checked, s, part="work").part is nx.part  # its assembly
    with pytest.raises(NXError) as caught:
        target.resolve(spec, checked, s, part="plate")
    assert caught.value.code == "NX_WRONG_PART_CONTEXT"


def test_an_occurrence_pins_its_prototype_unless_the_assembly_decides():
    nx = _block_nx()
    pin = _second(nx, "kitA/pin.prt")
    occurrence = NS(Tag=9001, IsOccurrence=True, Prototype=NS(OwningPart=pin), OwningPart=nx.part)
    s = services(nx)
    assert target.owner_of(s, occurrence, "face", "refs") is pin
    assert target.owner_of(s, occurrence, "face", "assembly") is nx.part
    assert target.owner_of(s, occurrence, "component", "refs") is nx.part


# ---------------------------------------------------------------------------
# operation_id per part, receipts written before target parts


def test_operation_id_fingerprint_includes_the_part():
    nx = _block_nx()
    plate = _second(nx)
    first = run(nx, "nx_create_primitive", operation_id="block_part_01", part="work", **BLOCK)
    assert first.ok
    again = run(nx, "nx_create_primitive", operation_id="block_part_01", part="work", **BLOCK)
    assert again.replayed and _count(nx) == 1
    record = services(nx).store.get("block_part_01")
    assert record["part_file"] == "work.prt"
    _show(nx, plate)
    other = run(nx, "nx_create_primitive", operation_id="block_part_01", part="plate", **BLOCK)
    assert other.error["code"] == "NX_IDEMPOTENCY_CONFLICT", other
    assert "another part (work.prt)" in other.error["message"]


def test_a_receipt_without_part_still_replays():
    nx = _block_nx()
    args = dict(BLOCK)
    first = run(nx, "nx_create_primitive", operation_id="older_block_01", **args)
    assert first.ok
    store = services(nx).store
    record = store.get("older_block_01")
    record.pop("part_file")
    record["fingerprint"] = receipts.fingerprint("nx_create_primitive", args)
    store.put(record)
    again = run(nx, "nx_create_primitive", operation_id="older_block_01", **args)
    assert again.replayed and _count(nx) == 1, again


def test_a_finished_call_replays_although_its_part_is_now_ambiguous():
    nx = _block_nx()
    assert run(nx, "nx_create_primitive", operation_id="replay_ambig1", **BLOCK).ok
    _second(nx)
    assert run(nx, "nx_create_primitive", **BLOCK).error["code"] == "NX_AMBIGUOUS_PART"
    again = run(nx, "nx_create_primitive", operation_id="replay_ambig1", **BLOCK)
    assert again.ok and again.replayed and _count(nx) == 1, again


def test_receipts_same_call_rules():
    args = {"a": 1}
    new = {"fingerprint": receipts.fingerprint("t", args, "a.prt"), "part_file": "a.prt"}
    assert receipts.same_call(new, "t", args, "a.prt")
    assert not receipts.same_call(new, "t", args, "b.prt")
    assert receipts.same_call(new, "t", args, receipts.ANY_PART)
    older = {"fingerprint": receipts.fingerprint("t", args)}
    assert receipts.same_call(older, "t", args, "a.prt")
    assert not receipts.same_call(older, "t", {"a": 2}, "a.prt")
    assert receipts.fingerprint("t", args) != receipts.fingerprint("t", args, "a.prt")


# ---------------------------------------------------------------------------
# The ghost guard


def test_an_expired_call_never_runs_and_leaves_no_receipt():
    nx = _block_nx()
    r = run(
        nx, "nx_create_primitive", operation_id="ghost_call_01", expires_at=time.time() - 5, **BLOCK
    )
    assert r.error["code"] == "NX_CALL_EXPIRED" and r.mutation_outcome == "not_started", r
    assert _count(nx) == 0
    assert services(nx).store.get("ghost_call_01")["state"] == "unknown"
    again = run(nx, "nx_create_primitive", operation_id="ghost_call_01", **BLOCK)
    assert again.ok and not again.replayed and _count(nx) == 1


def test_expiry_forms_and_reads():
    nx = _block_nx()
    assert run(nx, "nx_create_primitive", expires_at=time.time() + 60, **BLOCK).ok
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    r = run(nx, "nx_create_primitive", expires_at=past, **BLOCK)
    assert r.error["code"] == "NX_CALL_EXPIRED", r
    assert run(nx, "nx_list_parts", expires_at=time.time() - 60).ok  # a read still answers
    bad = run(nx, "nx_create_primitive", expires_at="soon", **BLOCK)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/expires_at"
    bad = run(nx, "nx_create_primitive", expires_at=True, **BLOCK)
    assert bad.error["field"] == "/expires_at"


# ---------------------------------------------------------------------------
# Call keys, policies and resolvers


def test_a_tools_own_part_parameter_is_not_a_call_key():
    spec = catalogue.load()["nx_close_part"]
    args, keys = target.split_call_keys(spec, {"part": "plate", "expires_at": 1})
    assert args == {"part": "plate"} and keys == {"expires_at": 1}
    spec = catalogue.load()["nx_create_primitive"]
    args, keys = target.split_call_keys(spec, {"part": "plate", "length": 1})
    assert args == {"length": 1} and keys == {"part": "plate"}


def test_own_part_argument_is_the_target_and_never_refused():
    nx = _block_nx()
    plate = _second(nx)
    spec = catalogue.load()["nx_close_part"]
    checked = spec.check({"part": "plate"})
    found = target.resolve(spec, checked, services(nx))
    assert found.part is plate and found.source == "arg"
    target.refuse_other_part(spec, found, services(nx), explicit=False)


def test_tools_without_a_part_ignore_the_loaded_parts():
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_list_parts")
    assert r.ok and "part" not in r.envelope, r


def test_redo_finds_its_part_and_its_calls_inherit_it():
    nx = _block_nx()
    _second(nx)
    assert run(nx, "nx_create_primitive", part="work", operation_id="redo_block_1", **BLOCK).ok
    undo = run(nx, "nx_undo_operation", part="work")
    assert undo.ok and _count(nx) == 0, undo
    redo = run(nx, "nx_redo_operation")  # no part: the resolver names the undone call's part
    assert redo.ok and redo.part["from"] == "resolver" and _count(nx) == 1, redo


def test_default_policies():
    specs = catalogue.load()
    expect = {
        "nx_create_extrude": ("refs", "work", False),
        "nx_measure_mass": ("assembly", "none", False),
        "nx_capture_view": ("display", "display", True),
        "nx_get_camera": ("display", "none", False),
        "nx_create_drawing_view": ("sheet", "display", True),
        "nx_create_sheet_metal_feature": ("refs", "display", True),
        "nx_create_part": ("creates", "none", False),
        "nx_close_part": ("arg:part", "none", False),
        "nx_list_parts": ("none", "none", False),
        "nx_list_materials": ("any", "none", False),
        "nx_get_sketch": ("refs", "work", False),
        "nx_finish_preview": ("resolver", "work", False),
    }
    for name, (policy, switch, bound) in expect.items():
        spec = specs[name]
        assert (spec.target, spec.switch, spec.display_bound) == (policy, switch, bound), name
    assert specs["nx_capture_view"].switch_for({}) == "display"


class Out(TypedDict, total=False):
    x: int


def test_catalogue_rules_for_target_switch_and_display_bound():
    def nx_get_thing(tx: Tx) -> Out:
        """Get a thing."""
        return {}

    def spec(**options):
        return make_spec(
            nx_get_thing, effect="read", label="Thing", keywords=("a", "b", "c"),
            domain="inspection", requires=("work_part",), **options,
        )  # fmt: skip

    assert spec().target == "refs" and spec().switch == "none"
    assert spec(target=lambda services, args: None).target == "resolver"
    assert spec(display_bound=True).switch == "display"
    for bad in (
        {"target": "everything"},
        {"target": "arg:missing"},
        {"switch": "sideways"},
        {"display_bound": True, "switch": "work"},
        {"display_bound": ("block",)},
        {"target": "none", "switch": "work"},
    ):
        with pytest.raises(CatalogueError):
            spec(**bad)


def test_check_call_keys_and_expiry_unit():
    assert target.check_call_keys(expires_at=12.5) == 12.5
    assert target.check_call_keys(expires_at="1970-01-01T00:01:00Z") == 60.0
    spec = NS(effect="model")
    with pytest.raises(NXError) as caught:
        target.check_expiry(spec, 100.0, now=160.0)
    assert caught.value.code == "NX_CALL_EXPIRED"
    target.check_expiry(NS(effect="read"), 100.0, now=160.0)
    target.check_expiry(spec, 200.0, now=160.0)


def test_kernel_context_carries_the_keys():
    nx = _block_nx()
    _second(nx)
    spec = catalogue.load()["nx_create_primitive"]
    context = kernel.Context(services(nx), part="work")
    envelope = kernel.run(spec, dict(BLOCK), context)
    assert envelope["ok"] and envelope["part"]["from"] == "part", envelope
    record_free = json.dumps(envelope)
    assert str(services(nx).root) not in record_free  # no absolute path in the envelope


def test_status_reports_the_agent_loaded_count():
    nx = _block_nx()
    parts = run(nx, "nx_get_status", include=["parts"]).data["parts"]
    assert parts["agent_loaded"] == 1 and "targeting" not in parts
    component = _second(nx, "kitA/pin.prt")
    nx.attach(nx.part, component)
    parts = run(nx, "nx_get_status", include=["parts"]).data["parts"]
    assert parts["agent_loaded"] == 2 and parts["top_level"] == 1 and parts["loaded"] == 2


def test_no_mode_window_or_environment_switch(monkeypatch):
    monkeypatch.setenv("NX_MCP_MULTI_AGENT", "1")  # no longer read
    assert run(_block_nx(), "nx_create_primitive", **BLOCK).ok
    for name in ("MULTI_ENV", "WINDOW", "note_part", "mode_status", "multi_agent"):
        assert not hasattr(target, name), name


def test_a_call_with_an_unknown_ref_is_not_ref_less():
    nx = _block_nx()
    _second(nx)
    r = run(nx, "nx_get_feature", feature="f:999")  # the handler reports the ref
    assert r.error["code"] not in ("NX_AMBIGUOUS_PART", "NX_WRONG_PART_CONTEXT"), r
    assert r.error["code"] == "NX_STALE_REFERENCE" and r.part["from"] == "work", r
