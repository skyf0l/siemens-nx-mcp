"""nx_delete_objects on the shared fake kernel: dispatch to @deleter hooks, dependents,
read-back, refusals and rollback under one undo mark."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.errors import fail
from nx_mcp.domains.session import delete
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Feature, Part
from tests.kit.parts import with_parts

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
        "expression",
        "explosion",
        "traceline",
        "annotation",
        "dimension",
        "drawing_table",
        "section_view",
        "drawing_sheet",
        "drawing_view",
    }


def test_a_body_is_refused_with_the_features_to_delete_instead(nx):
    extrude, _blend, body = model(nx)
    extrude.Timestamp, _blend.Timestamp = 1, 2
    body.GetFeatures = lambda: [_blend, extrude]
    failed = run(nx, "nx_delete_objects", objects=[body.ref])
    assert failed.error["code"] == "NX_WRONG_KIND", failed
    assert failed.error["details"]["features"] == [extrude.ref, _blend.ref]  # oldest first
    assert "details.features[0]" in failed.error["hint"]
    ok = run(nx, "nx_delete_objects", objects=failed.error["details"]["features"][:1])
    assert ok.ok and list(nx.part.Bodies) == [] and list(nx.part.Features) == []


def test_dependents_name_the_part_like_the_deleted_rows(nx):
    extrude, *_ = model(nx)
    ok = run(nx, "nx_delete_objects", objects=[extrude.ref])
    part = ok.data["deleted"][0]["part"]
    assert ok.data["dependents"] and {r["part"] for r in ok.data["dependents"]} == {part}


# ---------------------------------------------------------------------------
# A part in the background (gate 4s): display-bound kinds display it for the call


def _background(nx, name="asm.prt"):
    """``name`` loaded but not displayed: NX shows (and works in) the fixture's part."""
    with_parts(nx, services(nx).root)  # the display switch (tests/kit/parts.py)
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    part = Part(nx.session, Path(services(nx).root) / name)
    parts.Work, parts.Display = work, display
    nx.session.strict_work = True  # NX: SetWork only under the display part
    return part


def _owned(nx, part, obj, kind):
    obj.OwningPart, obj.IsOccurrence = part, False
    obj.ref = nx.ref_of(obj, kind)
    return obj


def test_an_explosion_of_a_background_assembly_is_deleted_on_the_displayed_assembly(nx):
    shown = nx.session.Parts.Display
    asm = _background(nx)
    explosion = _owned(nx, asm, NS(Tag=9101, Name="E1"), "explosion")
    asm.ComponentAssembly = NS(RootComponent=None, Explosions=[explosion])
    asm.DraftingViews = []  # no view shows it (its modeling views are not listable here)
    seen = []

    def remove():
        seen.append((nx.session.Parts.Display, nx.session.Parts.Work))
        asm.ComponentAssembly.Explosions.remove(explosion)

    explosion.Delete = remove
    ok = run(nx, "nx_delete_objects", objects=[explosion.ref])  # the real deleter
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert seen == [(asm, asm)]  # explosion_context: work and display part
    assert ("show", asm) in nx.session.calls
    assert nx.session.Parts.Display is shown and nx.session.Parts.Work is shown


@pytest.mark.parametrize("kind", delete.DISPLAYED_KINDS)
def test_display_bound_kinds_display_their_part(nx, kind):
    asm = _background(nx)
    obj = _owned(nx, asm, NS(Tag=9200, Name="X1"), kind)
    seen = []
    deleters = {**catalogue.hooks("deleter"), kind: lambda tx, objs: seen.append(
        (tx.part, nx.session.Parts.Display))}  # fmt: skip
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(catalogue._HOOKS, "deleter", deleters)
        mp.setitem(catalogue._HOOKS, "lister", {})
        ok = run(nx, "nx_delete_objects", objects=[obj.ref])
    assert ok.ok, ok
    assert seen == [(asm, asm)]
    assert nx.session.Parts.Display is nx.part


def test_features_of_a_background_part_are_deleted_in_the_quiet_work_part(nx):
    asm = _background(nx)
    f = _owned(nx, asm, Feature("EXTRUDE(1)"), "feature")
    f.Suppressed, f.cascade = False, []
    asm.Features.append(f)
    seen = []

    def delete_list(objects):
        seen.append((nx.session.Parts.Display, nx.session.Parts.Work))
        asm.Features.remove(f)

    nx.session.UpdateManager.AddToDeleteList = delete_list
    ok = run(nx, "nx_delete_objects", objects=[f.ref])
    assert ok.ok, ok
    assert seen == [(nx.part, asm)]  # the display stays the engineer's
    assert [name for name, _ in nx.session.calls] == ["SetWorkPartQuietly", "SetWork"]


def test_the_display_bound_kinds_of_nx_delete_objects():
    spec = catalogue.load()["nx_delete_objects"]
    assert spec.switch == "work"
    for kind in delete.DISPLAYED_KINDS:
        assert spec.switch_for({"objects": ["x:1"]}, {kind}) == "display", kind
    for kind in ("feature", "sketch", "curve", "component", "traceline"):
        assert spec.switch_for({"objects": ["x:1"]}, {kind}) == "work", kind
    assert spec.switch_for({}, {"feature", "explosion"}) == "display"  # mixed kinds


def test_a_sketch_listed_with_its_own_feature_is_deleted_once_through_the_feature(nx):
    sketch = nx.sketch()
    own = nx._own(sketch.Feature, "feature")
    own.Suppressed, own.cascade = False, [sketch]
    nx.part.Features.append(own)
    ok = run(nx, "nx_delete_objects", objects=[sketch.ref, own.ref])
    assert ok.ok, ok
    assert nx.deletes == [[own]]  # once, through the feature: NX takes the sketch with it
    assert {r["id"] for r in ok.data["deleted"]} == {sketch.ref, own.ref}
    assert list(nx.part.Sketches) == [] and list(nx.part.Features) == []


def test_an_unused_expression_is_deleted_and_a_used_one_is_refused_before_any_change(nx):
    unused = nx.expression("unused_len", "12")
    ok = run(nx, "nx_delete_objects", objects=[unused.ref])
    assert ok.ok and [e.Name for e in nx.part.Expressions] == [], ok
    used = nx.expression("wall", "2")
    other = nx.expression("depth", "wall*2")
    used.dependents = [other]
    used.GetReferencingExpressions = lambda: [other]
    refused = run(nx, "nx_delete_objects", objects=[used.ref])
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert refused.error["field"] == "/objects/0" and refused.mutation_outcome == "not_started"
    assert "depth" in refused.error["message"]
