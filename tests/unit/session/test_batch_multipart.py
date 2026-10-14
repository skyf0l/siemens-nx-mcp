"""Batch and preview children across parts (kernel.run_child, domains/session/batch.py).

Each child resolves its own target: its refs, a ``part`` call key in its arguments, else
the batch's part. Under a batch that names its ``part``, a child without a ``part`` of
its own is checked against the batch's part like a top-level call (refs of another part:
``NX_WRONG_PART_CONTEXT``); with its own ``part`` it acts there. A child in another part
is made current for its step (gate 4s, quiet work part) and the batch's part comes back
after it; one undo mark rolls back every part (NX's undo is session-wide:
tests/kit/objects.Session restores every part).
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from nx_mcp.core import busy, catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Ref
from nx_mcp.kit.result import Row
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Feature, Part


class Made(TypedDict):
    feature: Row


def nx_create_test_datum(tx: Tx) -> Made:
    """Create a datum feature (no body) in the work part."""
    part = tx.part
    feature = Feature(f"DATUM({len(part.Features) + 1})")
    feature.OwningPart, feature.IsOccurrence, feature.Suppressed = part, False, False
    part.Features.append(feature)
    return {"feature": tx.ref(feature, "feature")}


def nx_edit_test_feature(tx: Tx, *, feature: Ref, fail_after: bool = False) -> Made:
    """Rename a feature of the work part, then fail when asked."""
    obj = tx.resolve(feature, "feature", work_part=True)
    obj.SetName("EDITED")
    if fail_after:
        fail("NX_BUILD_FAILED", "the edit failed (test)")
    return {"feature": tx.ref(obj, "feature")}


DATUM = make_spec(
    nx_create_test_datum, effect="model", label="Test datum", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",), batchable=True, previewable=True,
)  # fmt: skip
EDIT = make_spec(
    nx_edit_test_feature, effect="model", label="Test edit", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",), batchable=True, previewable=True,
    example={"feature": "f:1"},
)  # fmt: skip


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, DATUM.name, DATUM)
    monkeypatch.setitem(catalogue._REGISTRY, EDIT.name, EDIT)


def _part(nx, name):
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    part = Part(nx.session, Path(services(nx).root) / name)
    parts.Work, parts.Display = work, display
    return part


def _feature(nx, part, name):
    f = nx._own(Feature(name), "feature")
    f.OwningPart, f.IsOccurrence, f.Suppressed = part, False, False
    part.Features.append(f)
    return f


def three():
    """``a.prt`` displayed and work, ``b.prt`` and ``c.prt`` in the background; NX's rule
    that only the display part or its components take ``SetWork``."""
    nx = fake_nx()
    nx.part.FullPath = str(Path(services(nx).root) / "a.prt")
    b, c = _part(nx, "b.prt"), _part(nx, "c.prt")
    nx.session.strict_work = True
    return nx, nx.part, b, c


def shown(nx):
    parts = nx.session.Parts
    return parts.Display.Name, parts.Work.Name


def rename(f, *, part=None, fail_after=False):
    arguments: dict = {"feature": f.ref}
    if part is not None:
        arguments["part"] = part
    if fail_after:
        arguments["fail_after"] = True
    return {"tool": "nx_edit_test_feature", "arguments": arguments}


def datum(part=None):
    return {"tool": "nx_create_test_datum", "arguments": {"part": part} if part else {}}


def test_children_act_in_the_parts_they_name_and_the_batch_part_comes_back():
    nx, a, b, c = three()
    fa, fb, fc = _feature(nx, a, "A1"), _feature(nx, b, "B1"), _feature(nx, c, "C1")
    marks = len(nx.session.marks)
    ok = run(
        nx, "nx_run_batch", part="b", operations=[rename(fb), rename(fc, part="c"), datum()]
    )  # fmt: skip
    assert ok.ok, ok
    assert (fa.Name, fb.Name, fc.Name) == ("A1", "EDITED", "EDITED")
    assert [f.Name for f in b.Features] == ["EDITED", "DATUM(2)"]  # step 2 inherits b.prt
    assert shown(nx) == ("work", "work")  # the engineer's display and work part
    results = ok.data["results"]
    assert "part" not in results[0] and results[1]["part"]["file"] == "c.prt"
    assert ok.part["file"] == "b.prt" and len(nx.session.marks) == marks + 1
    created = {row["name"] for row in ok.changes["modified"] + ok.changes["created"]}
    assert "DATUM(2)" in created
    stamped = busy._entries(services(nx))  # both parts the batch worked in are busy
    assert {services(nx).part_key(b), services(nx).part_key(c)} <= set(stamped)


def test_a_failing_child_rolls_back_every_part_under_the_one_mark():
    nx, a, b, c = three()
    fb, fc = _feature(nx, b, "B1"), _feature(nx, c, "C1")
    bad = run(
        nx, "nx_run_batch", part="b",
        operations=[datum(), rename(fc, part="c"), rename(fb, fail_after=True)],
    )  # fmt: skip
    assert not bad.ok and bad.mutation_outcome == "rolled_back", bad
    assert bad.error["details"]["child_index"] == 2
    assert (fb.Name, fc.Name) == ("B1", "C1") and [f.Name for f in b.Features] == ["B1"]
    assert shown(nx) == ("work", "work")


def test_a_child_in_another_part_failing_rolls_back_the_batch_part_too():
    nx, a, b, c = three()
    fb, fc = _feature(nx, b, "B1"), _feature(nx, c, "C1")
    steps = [rename(fb), rename(fc, part="c", fail_after=True)]
    bad = run(nx, "nx_run_batch", part="b", operations=steps)
    assert not bad.ok and bad.mutation_outcome == "rolled_back", bad
    assert (fb.Name, fc.Name) == ("B1", "C1") and shown(nx) == ("work", "work")


def test_a_child_part_key_is_accepted_checked_first_and_must_agree_with_its_refs():
    nx, a, b, c = three()
    fb, fc = _feature(nx, b, "B1"), _feature(nx, c, "C1")
    ok = run(nx, "nx_run_batch", part="b", operations=[rename(fb, part="b"), datum(part="c")])
    assert ok.ok, ok
    assert [f.Name for f in c.Features] == ["C1", "DATUM(2)"] and fb.Name == "EDITED"
    unknown = run(nx, "nx_run_batch", part="b", operations=[datum(), datum(part="nope")])
    assert unknown.error["code"] == "NX_NOT_FOUND", unknown
    assert unknown.mutation_outcome == "not_started"
    assert unknown.error["field"] == "/operations/1/arguments/part"
    assert unknown.error["details"]["child_index"] == 1 and len(b.Features) == 1
    clash = run(nx, "nx_run_batch", part="b", operations=[datum(), rename(fc, part="b")])
    assert clash.error["code"] == "NX_WRONG_PART_CONTEXT", clash
    assert clash.mutation_outcome == "rolled_back" and len(b.Features) == 1
    wrong = run(nx, "nx_run_batch", part="b", operations=[datum(part=5)])
    assert wrong.error["code"] == "NX_INVALID_ARGUMENT", wrong


def test_without_part_the_batch_takes_the_one_part_its_steps_name():
    nx, a, b, c = three()
    fb, fc = _feature(nx, b, "B1"), _feature(nx, c, "C1")
    ok = run(nx, "nx_run_batch", operations=[rename(fb), datum(part="b")])
    assert ok.ok and ok.part["file"] == "b.prt" and ok.part["from"] == "resolver", ok
    mixed = run(nx, "nx_run_batch", operations=[rename(fb), rename(fc)])
    assert mixed.error["code"] == "NX_AMBIGUOUS_PART", mixed  # two parts: say which
    refless = run(nx, "nx_run_batch", operations=[rename(fb), datum()])
    assert refless.error["code"] == "NX_AMBIGUOUS_PART", refless
    given = run(nx, "nx_run_batch", part="a", operations=[rename(fb, part="b"),
                                                            rename(fc, part="c")])  # fmt: skip
    assert given.ok and given.part["file"] == "a.prt", given  # each step names its part
    assert fb.Name == fc.Name == "EDITED" and shown(nx) == ("work", "work")


def test_under_the_batch_part_a_step_with_refs_of_another_part_is_refused():
    """The part-vs-refs gate runs on every child (a batch for part B must not rename a
    body of part A)."""
    nx, a, b, c = three()
    fa, fb = _feature(nx, a, "A1"), _feature(nx, b, "B1")
    del c.Features[:]
    for tool in ("nx_run_batch", "nx_preview_changes"):
        refused = run(nx, tool, part="b", operations=[datum(), rename(fa)])
        assert refused.error["code"] == "NX_WRONG_PART_CONTEXT", refused
        error = refused.error
        assert error["field"] == "/operations/1/arguments/feature", error
        assert "acts in b.prt (its part)" in error["message"] and "a.prt" in error["message"]
        assert '"part": "a.prt"' in error["hint"] and error["details"]["child_index"] == 1
        assert refused.mutation_outcome == "rolled_back"
        assert (fa.Name, fb.Name, len(b.Features)) == ("A1", "B1", 1)
    for tool in ("nx_run_batch", "nx_preview_changes"):  # refused first: nothing ran
        first = run(nx, tool, part="b", operations=[rename(fa), datum()])
        assert first.error["code"] == "NX_WRONG_PART_CONTEXT", first
        assert first.mutation_outcome == "not_started" and len(b.Features) == 1, first
    direct = run(nx, "nx_edit_test_feature", part="b", feature=fa.ref)  # the same rule
    assert direct.error["code"] == "NX_WRONG_PART_CONTEXT"
    ok = run(nx, "nx_run_batch", part="b", operations=[rename(fb), rename(fa, part="a")])
    assert ok.ok and fa.Name == fb.Name == "EDITED", ok
    # a ref an earlier step returned follows that step's part (it passed the gate there)
    chained = [datum(part="c"), {"tool": "nx_edit_test_feature",
                                 "arguments": {"feature": "$0.data.feature"}}]  # fmt: skip
    ok = run(nx, "nx_run_batch", part="b", operations=chained)
    assert ok.ok and [f.Name for f in c.Features] == ["EDITED"], ok
    assert ok.data["results"][1]["part"]["file"] == "c.prt"


def test_solo_batches_behave_as_before():
    nx = fake_nx()
    f = _feature(nx, nx.part, "A1")
    ok = run(nx, "nx_run_batch", operations=[rename(f), datum()])
    assert ok.ok and ok.part is None and "part" not in ok.data["results"][0], ok
    assert not [c for c in nx.session.calls if c[0] in ("SetWorkPartQuietly", "show")]


def test_preview_children_in_another_part_are_previewed_and_rolled_back():
    nx, a, b, c = three()
    fb, fc = _feature(nx, b, "B1"), _feature(nx, c, "C1")
    steps = [rename(fb), rename(fc, part="c")]
    seen = run(nx, "nx_preview_changes", part="b", operations=steps)
    assert seen.ok and seen.mutation_outcome == "previewed", seen
    assert (fb.Name, fc.Name) == ("B1", "C1") and shown(nx) == ("work", "work")
    assert seen.data["results"][1]["part"]["file"] == "c.prt"
    accepted = run(
        nx, "nx_finish_preview", preview_id=seen.data["preview_id"], action="accept"
    )  # fmt: skip
    assert accepted.ok and (fb.Name, fc.Name) == ("EDITED", "EDITED"), accepted
    assert accepted.part["file"] == "b.prt" and shown(nx) == ("work", "work")


def test_preview_capture_switches_the_display_to_the_target():
    spec = catalogue.get("nx_preview_changes")
    assert spec.switch_for({"operations": [], "capture": True}) == "display"
    assert spec.switch_for({"operations": [], "capture": False}) == "work"
    assert spec.switch_for({"operations": []}) == "work"


SEEN: list = []


def nx_create_test_note(tx: Tx) -> Made:
    """Create a note on the displayed part (a display-bound test tool)."""
    parts = tx.session.Parts
    SEEN.append((parts.Display.Name, parts.Work.Name))
    return nx_create_test_datum(tx)


NOTE = make_spec(
    nx_create_test_note, effect="model", label="Test note", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",), batchable=True, switch="display",
)  # fmt: skip


def test_a_display_bound_child_of_a_quiet_batch_is_displayed_then_put_back(monkeypatch):
    from tests.kit.parts import with_parts

    monkeypatch.setitem(catalogue._REGISTRY, NOTE.name, NOTE)
    SEEN.clear()
    nx, a, b, c = three()
    with_parts(nx, services(nx).root)
    fb = _feature(nx, b, "B1")
    note = {"tool": "nx_create_test_note", "arguments": {}}
    ok = run(nx, "nx_run_batch", part="b", operations=[rename(fb), note, datum()])
    assert ok.ok, ok
    assert SEEN == [("b", "b")]  # the note's part was displayed for it
    assert [f.Name for f in b.Features] == ["EDITED", "DATUM(2)", "DATUM(3)"]
    assert shown(nx) == ("work", "work")
