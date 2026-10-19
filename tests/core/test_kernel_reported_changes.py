"""What the kernel reports for a committed or refused model call (core/kernel.py gate 14 and
15): objects a call edited without saying so are ``modified``, objects the snapshots cannot
see are ``created`` through ``tx.created``, and a handler's own refusal after it read NX is
``not_started`` once the rollback left the part as it was."""

from __future__ import annotations

from typing import TypedDict

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Ref
from tests.kit import fake_nx, run
from tests.kit.objects import Body, Feature


class Done(TypedDict):
    done: bool


def nx_set_test_object_name(tx: Tx, *, object: Ref, quiet: bool = False) -> Done:  # noqa: A002
    """Rename an object without reporting it (``quiet``: say nothing changed instead)."""
    obj = tx.resolve(object)
    if quiet:
        tx.warn("NX_NO_EFFECT", "Nothing to rename")
        return {"done": False}
    obj.Name = "RENAMED"
    return {"done": True}


def nx_create_test_untracked(tx: Tx) -> Done:
    """Make an object the part snapshots do not list (an arrangement)."""
    tx.part  # noqa: B018  # touches NX
    tx.created({"id": "OPEN", "kind": "arrangement", "name": "OPEN"})
    return {"done": True}


def nx_check_test_late(tx: Tx, *, change_first: bool = False) -> Done:
    """Read NX, optionally create a body, then refuse an argument."""
    part = tx.part
    if change_first:
        body = Body("LATE")
        body.OwningPart, body.IsOccurrence = part, False
        part.Bodies.append(body)
    fail("NX_INVALID_ARGUMENT", "The value does not fit the model", field="/change_first")


SPECS = [
    make_spec(
        fn,
        effect="model",
        label=label,
        keywords=("a", "b", "c"),
        domain="modeling",
        requires=("work_part",),
        example=example,
    )  # fmt: skip
    for fn, label, example in (
        (nx_set_test_object_name, "Rename test object", {"object": "f:1"}),
        (nx_create_test_untracked, "Make test untracked", {}),
        (nx_check_test_late, "Check test late", {}),
    )
]


@pytest.fixture(autouse=True)
def _specs(monkeypatch):
    catalogue.load()
    for spec in SPECS:
        monkeypatch.setitem(catalogue._REGISTRY, spec.name, spec)


def _feature(nx):
    feature = Feature("BLOCK(1)")
    feature.OwningPart, feature.IsOccurrence, feature.Suppressed = nx.part, False, False
    nx.part.Features.append(feature)
    return feature, nx.ref_of(feature, "feature")


def test_an_edit_that_reported_nothing_lists_the_objects_it_names_as_modified():
    nx = fake_nx()
    feature, ref = _feature(nx)
    ok = run(nx, "nx_set_test_object_name", object=ref)
    assert ok.ok and feature.Name == "RENAMED", ok
    assert [r["id"] for r in ok.changes["modified"]] == [ref]
    quiet = run(nx, "nx_set_test_object_name", object=ref, quiet=True)
    assert quiet.ok and quiet.mutation_outcome == "not_applicable"
    assert quiet.changes["modified"] == []  # it said it changed nothing


def test_an_object_the_snapshots_cannot_see_is_reported_through_tx_created():
    nx = fake_nx()
    ok = run(nx, "nx_create_test_untracked")
    assert ok.ok and ok.changes["created"] == [{"id": "OPEN", "kind": "arrangement",
                                                 "name": "OPEN"}]  # fmt: skip


def test_a_handlers_own_refusal_after_reading_nx_is_not_started():
    nx = fake_nx()
    refused = run(nx, "nx_check_test_late")
    assert refused.error["code"] == "NX_INVALID_ARGUMENT"
    assert refused.mutation_outcome == "not_started", refused
    changed = run(nx, "nx_check_test_late", change_first=True)
    assert changed.mutation_outcome == "rolled_back" and len(nx.part.Bodies) == 0, changed
