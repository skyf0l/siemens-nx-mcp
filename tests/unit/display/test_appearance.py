"""Visibility, appearance, highlights and restore_id snapshots on the shared fake kernel."""

import pytest

from nx_mcp.kit import display
from tests.kit import run
from tests.kit.objects import Component, Feature
from tests.unit.display.fakes import display_nx


@pytest.fixture(autouse=True)
def fresh_snapshots():
    display.snapshots.clear()
    yield
    display.snapshots.clear()


def test_hide_and_show_read_back_and_restore_in_reverse_order():
    nx = display_nx()
    a, b = nx.block(1, 1, 1), nx.block(2, 2, 2)
    hide = run(nx, "nx_set_visibility", mode="hide", objects=[a.ref, b.ref])
    assert hide.ok and a.IsBlanked and b.IsBlanked and hide.data["count"] == 2
    assert [r["blanked"] for r in hide.data["objects"]] == [True, True]
    show = run(nx, "nx_set_visibility", mode="show", objects=[a.ref])
    assert not a.IsBlanked
    early = run(nx, "nx_restore_display", restore_id=hide.restore_id)
    assert early.error["code"] == "NX_DEPENDENCY_ORDER" and early.mutation_outcome == "not_started"
    assert early.error["details"]["restore_first"] == show.restore_id
    first = run(nx, "nx_restore_display", restore_id=show.restore_id)
    assert first.ok and a.IsBlanked and first.data["identical"] is True
    second = run(nx, "nx_restore_display", restore_id=hide.restore_id)
    assert second.ok and not a.IsBlanked and not b.IsBlanked and second.data["count"] == 2
    again = run(nx, "nx_restore_display", restore_id=hide.restore_id)
    assert again.error["category"] == "stale_reference" and again.error["field"] == "/restore_id"


def test_isolate_hides_every_other_body_and_component():
    nx = display_nx()
    a, b = nx.block(1, 1, 1), nx.block(2, 2, 2)
    proto = nx.block(3, 3, 3)
    nx.part.Bodies.remove(proto)
    component = Component("C1", [proto])
    nx.part.ComponentAssembly.RootComponent = Component("root")
    nx.part.ComponentAssembly.RootComponent.children.append(component)
    ok = run(nx, "nx_set_visibility", mode="isolate", objects=[a.ref])
    assert ok.ok and not a.IsBlanked and b.IsBlanked and component.IsBlanked
    occurrence = next(iter(component.occurrences.values()))
    assert occurrence.IsBlanked
    run(nx, "nx_restore_display", restore_id=ok.restore_id)
    assert not b.IsBlanked and not component.IsBlanked and not occurrence.IsBlanked


def test_datums_are_shown_or_hidden_together_and_refused_for_isolate():
    nx = display_nx()
    planes = [nx.datum_plane(), nx.datum_plane(normal=(1, 0, 0))]
    ok = run(nx, "nx_set_visibility", mode="hide", datums=True)
    assert ok.ok and all(p.IsBlanked for p in planes) and ok.data["count"] == 2
    bad = run(nx, "nx_set_visibility", mode="isolate", datums=True)
    assert bad.error["field"] == "/mode" and bad.mutation_outcome == "not_started"
    both = run(nx, "nx_set_visibility", mode="show", datums=True, objects=[planes[0].ref])
    assert both.error["category"] == "invalid_argument"


def test_faces_cannot_be_hidden_and_nothing_changes():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_set_visibility", mode="hide", objects=[body.faces[0].ref])
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.mutation_outcome == "not_started"
    assert not body.faces[0].IsBlanked


def test_feature_and_sketch_targets_expand_to_what_they_show():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    extrude = Feature("EXTRUDE(1)", [body])
    ref = nx.ref_of(extrude, "feature")
    sketch = nx.sketch()
    curve = nx.curve(sketch)
    ok = run(nx, "nx_set_visibility", mode="hide", objects=[ref, sketch.ref])
    assert ok.ok and body.IsBlanked and curve.IsBlanked and ok.data["count"] == 2


def test_appearance_by_name_reads_back_and_restores_identically():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    before = run(nx, "nx_get_appearance", objects=[body.ref]).data["objects"]
    ok = run(nx, "nx_set_appearance", objects=[body.ref], color="red", transparency=40)
    assert ok.ok and body.Color == 186 and body.transparency == 40
    assert all(f.Color == 186 for f in body.faces) and ok.data["marks_part_modified"] is True
    assert ok.data["objects"][0]["color"] == 186 and ok.data["objects"][0]["transparency"] == 40
    restored = run(nx, "nx_restore_display", restore_id=ok.restore_id)
    assert restored.ok and restored.data["identical"] is True
    assert run(nx, "nx_get_appearance", objects=[body.ref]).data["objects"] == before
    assert restored.data["objects"] == before


def test_transparency_needs_bodies_or_faces_and_colour_or_transparency():
    nx = display_nx()
    curve = nx.curve()
    bad = run(nx, "nx_set_appearance", objects=[curve.ref], transparency=10)
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.mutation_outcome == "not_started"
    none = run(nx, "nx_set_appearance", objects=[curve.ref])
    assert none.error["field"] == "/color"
    index = run(nx, "nx_set_appearance", objects=[curve.ref], color=12)
    assert index.ok and curve.Color == 12
    out = run(nx, "nx_set_appearance", objects=[curve.ref], color=300)
    assert out.error["category"] == "invalid_argument"


def test_a_colour_nx_does_not_apply_rolls_back():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    nx.session.DisplayManager.NewDisplayModification = lambda: _Ignoring(body)
    bad = run(nx, "nx_set_appearance", objects=[body.ref], color=5)
    assert bad.error["details"]["check"] == "color" and bad.mutation_outcome == "rolled_back"
    assert body.Color == 7


class _Ignoring:
    """A display modification that changes the colour to something else."""

    def __init__(self, body):
        self.body = body

    def Apply(self, objects):
        self.body.Color = 99

    def Dispose(self):
        pass


def test_appearance_count_only():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    ok = run(nx, "nx_get_appearance", objects=[body.ref], count_only=True)
    assert ok.data == {"count": 7}


def test_highlight_replaces_and_clears_the_agents_highlights():
    nx = display_nx()
    a, b = nx.block(1, 1, 1), nx.block(2, 2, 2)
    ok = run(nx, "nx_set_highlight", objects=[a.ref, b.faces[0].ref])
    assert ok.ok and a.highlighted and b.faces[0].highlighted and ok.data["count"] == 2
    swap = run(nx, "nx_set_highlight", objects=[b.ref])
    assert swap.ok and not a.highlighted and b.highlighted
    clear = run(nx, "nx_set_highlight", objects=[])
    assert clear.ok and not b.highlighted and nx.highlighted == [] and clear.data["count"] == 0


def test_a_stale_member_fails_the_restore_before_anything_changes():
    nx = display_nx()
    a, b = nx.block(1, 1, 1), nx.block(2, 2, 2)
    hide = run(nx, "nx_set_visibility", mode="hide", objects=[a.ref, b.ref])
    a.Unblank()
    del nx.refs[b.ref]
    bad = run(nx, "nx_restore_display", restore_id=hide.restore_id)
    assert bad.error["category"] == "stale_reference" and bad.mutation_outcome == "not_started"
    assert not a.IsBlanked and hide.restore_id in display.snapshots.items


def test_restore_needs_exactly_one_source():
    nx = display_nx()
    bad = run(nx, "nx_restore_display")
    assert bad.error["category"] == "invalid_argument" and bad.mutation_outcome == "not_started"


def test_nothing_shown_is_refused():
    nx = display_nx()
    sketch = nx.sketch()
    bad = run(nx, "nx_set_visibility", mode="hide", objects=[sketch.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY"
