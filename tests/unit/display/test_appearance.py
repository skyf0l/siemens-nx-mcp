"""Visibility, appearance, highlights and restore_id snapshots on the shared fake kernel."""

import pytest

from nx_mcp.kit import display
from tests.kit import run
from tests.kit.kernel import services
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


def test_restore_with_nothing_to_restore_says_so():
    nx = display_nx()
    bad = run(nx, "nx_restore_display")
    assert bad.error["code"] == "NX_NOT_FOUND" and bad.mutation_outcome == "not_started"
    assert bad.error["field"] == "/restore_id" and "presentation" in bad.error["hint"]
    (services(nx).root / "a.json").write_text("{}", encoding="utf-8")
    both = run(nx, "nx_restore_display", restore_id="last", presentation="a.json")
    assert both.error["category"] == "invalid_argument" and both.mutation_outcome == "not_started"


@pytest.mark.parametrize("how", [{}, {"restore_id": "last"}, {"restore_id": "latest"}])
def test_last_or_no_argument_walks_back_one_change_at_a_time(how):
    nx = display_nx()
    a, b = nx.block(1, 1, 1), nx.block(2, 2, 2)
    run(nx, "nx_set_visibility", mode="hide", objects=[a.ref, b.ref])
    show = run(nx, "nx_set_visibility", mode="show", objects=[a.ref])
    first = run(nx, "nx_restore_display", **how)
    assert first.ok and first.data["restored"] == show.restore_id and a.IsBlanked
    assert "restore_id" not in first.envelope  # no redo: the next 'last' goes further back
    second = run(nx, "nx_restore_display", **how)
    assert second.ok and not a.IsBlanked and not b.IsBlanked
    done = run(nx, "nx_restore_display", **how)
    assert done.error["code"] == "NX_NOT_FOUND"


def test_nothing_shown_is_refused():
    nx = display_nx()
    sketch = nx.sketch()
    bad = run(nx, "nx_set_visibility", mode="hide", objects=[sketch.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY"


@pytest.mark.parametrize(
    ("color", "index", "name"),
    [("Red", 186, "red"), (" RED ", 186, "red"), ("grey", 130, "gray"), ("211", 211, "blue")],
)
def test_colour_names_in_any_case_and_spelling(color, index, name):
    nx = display_nx()
    body = nx.block(1, 1, 1)
    ok = run(nx, "nx_set_appearance", objects=[body.ref], color=color)
    assert ok.ok, ok.error
    assert body.Color == index and ok.data["objects"][0]["color"] == index
    assert ok.data["objects"][0]["color_name"] == name


def test_unknown_colour_lists_the_names_with_a_guess():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_set_appearance", objects=[body.ref], color="bleu")
    assert bad.error["field"] == "/color" and "did you mean 'blue'" in bad.error["message"]
    assert "'magenta'" in bad.error["hint"] and bad.mutation_outcome == "not_started"
    big = run(nx, "nx_set_appearance", objects=[body.ref], color="300")
    assert big.error["field"] == "/color" and body.Color == 7


@pytest.mark.parametrize("value", ["40%", "40 %", " 40", "40.0%"])
def test_transparency_takes_percent_strings(value):
    nx = display_nx()
    body = nx.block(1, 1, 1)
    ok = run(nx, "nx_set_appearance", objects=[body.ref], transparency=value)
    assert ok.ok and body.transparency == 40


@pytest.mark.parametrize("value", ["150%", "half", 150])
def test_transparency_out_of_range_is_refused(value):
    nx = display_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_set_appearance", objects=[body.ref], transparency=value)
    assert bad.error["field"] == "/transparency" and bad.mutation_outcome == "not_started"


def test_a_body_reports_its_faces_transparency_and_colour_name():
    """The NX host asks translucency of faces only (a body's record has none)."""
    nx = display_nx()
    body = nx.block(1, 1, 1)
    kit = services(nx)
    records = kit.display_records

    def like_the_host(objects, appearance):
        pairs = records(objects, appearance)
        for obj, record in pairs:
            if obj is body:
                record.pop("transparency", None)
        return pairs

    kit.display_records = like_the_host
    run(nx, "nx_set_appearance", objects=[body.ref], color="red", transparency=40)
    rows = run(nx, "nx_get_appearance", objects=[body.ref]).data["objects"]
    assert rows[0]["object"]["kind"] == "body"
    assert rows[0]["transparency"] == 40 and rows[0]["color_name"] == "red"
    run(nx, "nx_set_appearance", objects=[body.faces[0].ref], transparency=10)
    mixed = run(nx, "nx_get_appearance", objects=[body.ref]).data["objects"]
    assert mixed[0]["transparency"] is None and mixed[1]["transparency"] == 10


def test_hide_counts_what_ends_up_blanked():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    ok = run(nx, "nx_set_visibility", mode="hide", objects=[body.ref])
    assert ok.ok and body.IsBlanked and ok.data["mode"] == "hide" and ok.data["blanked"] == 1


def test_show_alone_shows_every_hidden_body_and_component():
    nx = display_nx()
    a, b, c = nx.block(1, 1, 1), nx.block(2, 2, 2), nx.block(3, 3, 3)
    plane = nx.datum_plane()
    run(nx, "nx_set_visibility", mode="hide", objects=[a.ref, b.ref])
    run(nx, "nx_set_visibility", mode="hide", datums=True)
    ok = run(nx, "nx_set_visibility", mode="show")
    assert ok.ok and not a.IsBlanked and not b.IsBlanked and not c.IsBlanked
    assert ok.data["count"] == 2 and ok.data["blanked"] == 0
    assert plane.IsBlanked  # datums keep their own switch
    run(nx, "nx_restore_display", restore_id=ok.restore_id)
    assert a.IsBlanked and b.IsBlanked and not c.IsBlanked
    nothing = run(nx, "nx_set_visibility", mode="hide")
    assert nothing.error["field"] == "/objects" and "mode=show" in nothing.error["hint"]


def test_face_refs_are_refused_with_a_hint():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_set_visibility", mode="hide", objects=[body.faces[0].ref])
    assert bad.error["category"] == "wrong_kind" and bad.mutation_outcome == "not_started"
    assert not body.faces[0].IsBlanked
