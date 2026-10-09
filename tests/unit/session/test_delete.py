"""nx_delete_objects on the shared fake kernel: dispatch to @deleter hooks, dependents,
read-back, refusals and rollback under one undo mark."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.errors import fail
from tests.kit import fake_nx, run
from tests.kit.objects import Feature

COLLECTIONS = ("Bodies", "Features", "Sketches", "Curves")


@pytest.fixture
def nx():
    nx = fake_nx()
    nx.deletes = []

    def add_to_delete_list(objects):
        """NX's Delete: an object takes its ``cascade`` (dependents) with it."""
        nx.deletes.append(list(objects))
        pending = list(objects)
        while pending:
            obj = pending.pop()
            pending.extend(getattr(obj, "cascade", []))
            for name in COLLECTIONS:
                values = getattr(nx.part, name)
                if obj in values:
                    values.remove(obj)

    nx.session.UpdateManager.AddToDeleteList = add_to_delete_list
    return nx


def feature(nx, name, *, body=None):
    f = nx._own(Feature(name, bodies=[body] if body else []), "feature")
    f.Suppressed = False
    f.cascade = [body] if body else []
    nx.part.Features.append(f)
    return f


def model(nx):
    """EXTRUDE(1) makes a block; BLEND(2) depends on it."""
    body = nx.block(10, 10, 10)
    extrude = feature(nx, "EXTRUDE(1)", body=body)
    blend = feature(nx, "BLEND(2)")
    extrude.cascade.append(blend)
    return extrude, blend, body


def test_feature_with_dependents_is_deleted_under_one_mark(nx):
    extrude, blend, body = model(nx)
    marks = nx.session.marks.copy()
    ok = run(nx, "nx_delete_objects", objects=[extrude.ref])
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert nx.deletes == [[extrude]]
    assert [r["id"] for r in ok.data["deleted"]] == [extrude.ref]
    assert {r["id"] for r in ok.data["dependents"]} == {blend.ref, body.ref}
    assert ok.data["dependent_count"] == 2
    assert sorted(r["name"] for r in ok.changes["deleted"]) == ["BLEND(2)", "BLOCK", "EXTRUDE(1)"]
    assert ok.changes["references"] == "listed"
    assert not set(nx.refs) & {extrude.ref, blend.ref, body.ref}  # invalidated
    assert list(nx.part.Features) == [] and list(nx.part.Bodies) == []
    created = [m for m in nx.session.marks if m not in marks]
    assert len(created) <= 1  # one visible mark for the call
    update = nx.session.UpdateManager.DoUpdate
    assert update.call_count == 1


def test_mixed_kinds_skip_objects_an_earlier_group_took(nx):
    sketch = nx.sketch()
    curve = nx.curve(sketch)
    nx.part.Curves.append(curve)
    sketch.cascade = [curve]
    explosion = nx._own(NS(Tag=9001, Name="E1"), "explosion")
    taken = []
    deleters = dict(catalogue.hooks("deleter"))
    deleters["explosion"] = lambda tx, objs: taken.extend(objs)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(catalogue._HOOKS, "deleter", deleters)
        mp.setitem(catalogue._HOOKS, "lister", {})  # the fake explosion has no lister
        ok = run(nx, "nx_delete_objects", objects=[sketch.ref, curve.ref, explosion.ref, curve.ref])
    assert ok.ok, ok
    assert nx.deletes == [[sketch]]  # the curve went with its sketch
    assert taken == [explosion]
    assert [r["id"] for r in ok.data["deleted"]] == [sketch.ref, curve.ref, explosion.ref]
    assert ok.data["dependents"] == []


def test_a_kind_without_deleter_is_refused_before_anything_changes(nx):
    extrude, _blend, body = model(nx)
    failed = run(nx, "nx_delete_objects", objects=[extrude.ref, body.ref])
    assert not failed.ok and failed.error["code"] == "NX_WRONG_KIND"
    assert failed.mutation_outcome == "not_started"
    assert failed.error["field"] == "/objects/1"
    assert "feature" in failed.error["details"]["deletable"]
    assert nx.deletes == [] and list(nx.part.Features)


def test_objects_of_another_part_are_refused(nx):
    extrude, _blend, _body = model(nx)
    extrude.OwningPart = NS(Tag=777, Name="other")
    failed = run(nx, "nx_delete_objects", objects=[extrude.ref])
    assert failed.error["code"] == "NX_WRONG_OWNER" and failed.mutation_outcome == "not_started"


def test_update_errors_roll_everything_back(nx):
    extrude, blend, body = model(nx)
    nx.session.UpdateManager.DoUpdate.return_value = 2
    failed = run(nx, "nx_delete_objects", objects=[extrude.ref])
    assert failed.error["code"] == "NX_UPDATE_FAILED"
    assert failed.mutation_outcome == "rolled_back"
    assert list(nx.part.Features) == [extrude, blend] and list(nx.part.Bodies) == [body]


def test_an_object_nx_kept_fails_the_read_back(nx):
    extrude, *_ = model(nx)
    nx.session.UpdateManager.AddToDeleteList = lambda objects: None
    failed = run(nx, "nx_delete_objects", objects=[extrude.ref])
    assert failed.error["code"] == "NX_VERIFICATION_FAILED"
    assert failed.error["details"]["actual"]["objects"] == [extrude.ref]
    assert failed.mutation_outcome == "rolled_back"


def test_a_later_failing_deleter_rolls_back_the_earlier_groups(nx):
    extrude, blend, body = model(nx)
    explosion = nx._own(NS(Tag=9002, Name="E2"), "explosion")

    def refuse(tx, objs):
        fail("NX_IN_USE", "Explosion E2 is shown in views")

    deleters = {**catalogue.hooks("deleter"), "explosion": refuse}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(catalogue._HOOKS, "deleter", deleters)
        failed = run(nx, "nx_delete_objects", objects=[extrude.ref, explosion.ref])
    assert failed.error["code"] == "NX_IN_USE" and failed.mutation_outcome == "rolled_back"
    assert list(nx.part.Features) == [extrude, blend] and list(nx.part.Bodies) == [body]


def test_an_active_sketch_is_refused(nx):
    extrude, *_ = model(nx)
    nx.session.ActiveSketch = nx.sketch()
    failed = run(nx, "nx_delete_objects", objects=[extrude.ref])
    assert failed.error["code"] == "NX_SKETCH_ACTIVE" and nx.deletes == []


def test_every_deletable_kind_has_a_deleter():
    catalogue.load()
    assert set(catalogue.hooks("deleter")) == {
        "feature",
        "sketch",
        "curve",
        "component",
        "explosion",
        "traceline",
        "annotation",
        "dimension",
        "drawing_table",
        "section_view",
        "drawing_sheet",
        "drawing_view",
    }
