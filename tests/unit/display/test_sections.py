"""Dynamic section views: create, edit, rollback, nx_list_objects rows and the deleter."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import catalogue
from tests.kit import NXException, run
from tests.unit.display.fakes import View, display_nx, show_sheet

PLANE = {"origin": [10, 5, 0], "normal": [2, 0, 0]}


def test_create_reads_back_the_plane_and_activates_it():
    nx = display_nx()
    ok = run(nx, "nx_create_section_view", plane=PLANE, name="CUT_A")
    assert ok.ok and ok.mutation_outcome == "committed"
    assert ok.data["origin"] == [10, 5, 0] and ok.data["normal"] == [1, 0, 0]
    assert ok.data["active"] and ok.data["sectioning"] and ok.data["cap"]
    assert ok.data["section"]["kind"] == "section_view" and ok.frame == "display_part_absolute"
    section = nx.part.DynamicSections[0]
    assert section.Name == "CUT_A" and nx.work_view.ActiveDynamicSection is section


def test_a_second_active_section_is_refused_unchanged():
    nx = display_nx()
    first = run(nx, "nx_create_section_view", plane=PLANE)
    bad = run(nx, "nx_create_section_view", plane={"origin": [0, 0, 0], "normal": [0, 0, 1]})
    assert bad.error["code"] == "NX_ALREADY_EXISTS" and bad.mutation_outcome == "not_started"
    assert bad.error["details"]["section"]["id"] == first.data["section"]["id"]
    assert len(nx.part.DynamicSections) == 1


def test_zero_normal_is_an_argument_error():
    nx = display_nx()
    bad = run(nx, "nx_create_section_view", plane={"origin": [0, 0, 0], "normal": [0, 0, 0]})
    assert bad.error["field"] == "/plane/normal" and bad.mutation_outcome == "not_started"


def test_a_plane_nx_did_not_take_is_removed_again():
    nx = display_nx()
    nx.section_normal = [0.0, 1.0, 0.0]
    bad = run(nx, "nx_create_section_view", plane=PLANE)
    assert bad.error["details"]["check"] == "normal" and bad.mutation_outcome == "rolled_back"
    assert not nx.part.DynamicSections and not nx.work_view.DisplaySectioningToggle


def test_edit_moves_switches_cap_and_toggles():
    nx = display_nx()
    ref = run(nx, "nx_create_section_view", plane=PLANE).data["section"]["id"]
    moved = run(
        nx, "nx_edit_section_view", section=ref,
        plane={"origin": [0, 0, 3], "normal": [0, 0, 1]}, cap=False,
    )  # fmt: skip
    assert moved.ok and moved.data["origin"] == [0, 0, 3] and moved.data["normal"] == [0, 0, 1]
    assert moved.data["cap"] is False
    off = run(nx, "nx_edit_section_view", section=ref, enabled=False)
    assert off.ok and not off.data["sectioning"] and not nx.work_view.DisplaySectioningToggle
    on = run(nx, "nx_edit_section_view", section=ref, enabled=True)
    assert on.ok and on.data["active"] and on.data["sectioning"]


def test_a_failed_edit_puts_the_plane_back():
    nx = display_nx()
    ref = run(nx, "nx_create_section_view", plane=PLANE).data["section"]["id"]
    nx.section_error = NXException(1, "refused")
    bad = run(
        nx, "nx_edit_section_view", section=ref, plane={"origin": [1, 1, 1], "normal": [0, 1, 0]}
    )
    assert bad.mutation_outcome == "rolled_back"
    nx.section_error = None
    section = nx.part.DynamicSections[0]
    assert section.origin == [10, 5, 0] and section.normal == [1, 0, 0]


def test_edit_needs_a_change_and_a_section():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    made = run(nx, "nx_create_section_view", plane=PLANE).data["section"]["id"]
    assert run(nx, "nx_edit_section_view", section=made).error["field"] == "/plane"
    bad = run(nx, "nx_edit_section_view", section=body.ref, enabled=True)
    assert bad.error["category"] == "wrong_kind" and bad.mutation_outcome == "not_started"


def test_sections_need_the_model_view():
    nx = display_nx()
    show_sheet(nx)
    bad = run(nx, "nx_create_section_view", plane=PLANE)
    assert bad.error["code"] == "NX_WRONG_APPLICATION"


def test_listed_with_their_plane_and_state():
    nx = display_nx()
    run(nx, "nx_create_section_view", plane=PLANE)
    rows = run(nx, "nx_list_objects", kind="section_view").data["items"]
    assert len(rows) == 1 and rows[0]["type"] == "one_plane"
    assert rows[0]["details"] == {
        "origin": [10, 5, 0], "normal": [1, 0, 0], "cap": True, "active": True,
        "sectioning": True,
    }  # fmt: skip


def test_deleting_the_last_section_switches_sectioning_off():
    """NX 2406: DeleteSections leaves DisplaySectioningToggle on with no section left."""
    nx = display_nx()
    run(nx, "nx_create_section_view", plane=PLANE)
    view = nx.work_view
    deleter = catalogue.hooks("deleter")["section_view"]
    deleter(nx.tx, list(nx.part.DynamicSections))
    assert not nx.part.DynamicSections and not view.DisplaySectioningToggle


@pytest.mark.parametrize("keep_active", [True, False])
def test_deleting_one_of_two_keeps_sectioning_only_with_an_active_one(keep_active):
    nx = display_nx()
    run(nx, "nx_create_section_view", plane=PLANE)
    view = nx.work_view
    first = nx.part.DynamicSections[0]
    view.ActiveDynamicSection = None
    run(nx, "nx_create_section_view", plane={"origin": [0, 0, 0], "normal": [0, 0, 1]})
    second = nx.part.DynamicSections[1]
    target = first if keep_active else second
    catalogue.hooks("deleter")["section_view"](nx.tx, [target])
    assert view.DisplaySectioningToggle is keep_active


def test_create_says_which_side_is_hidden_and_how_to_remove_it():
    nx = display_nx()
    ok = run(nx, "nx_create_section_view", plane={"origin": [0, 0, 0], "normal": [0, -3, 4]})
    data = ok.data
    assert data["hidden_side"].startswith("where the normal points")
    assert data["cut_face_view_from"] == pytest.approx([0, -0.6, 0.8])
    assert data["remove_with"] == {
        "tool": "nx_delete_objects",
        "arguments": {"objects": [data["section"]["id"]]},
    }
    removed = run(nx, data["remove_with"]["tool"], **data["remove_with"]["arguments"])
    assert removed.ok and not nx.part.DynamicSections
    assert nx.work_view.DisplaySectioningToggle is False


@pytest.mark.parametrize(
    ("plane", "offset", "origin", "normal", "hidden"),
    [
        ("XZ", "30 mm", [0, 30, 0], [0, 1, 0], "y > 30"),
        ("xz plane", 30, [0, 30, 0], [0, 1, 0], "y > 30"),
        ("-yz", 50, [50, 0, 0], [-1, 0, 0], "x < 50"),
        ("top", None, [0, 0, 0], [0, 0, 1], "z > 0"),
        ({"normal": [0, 0, 2]}, 5, [0, 0, 5], [0, 0, 1], "z > 5"),
        ({"origin": [1, 2, 3], "normal": [1, 0, 0]}, -1, [0, 2, 3], [1, 0, 0], "x > 0"),
    ],
)
def test_create_takes_a_principal_plane_and_an_offset(plane, offset, origin, normal, hidden):
    nx = display_nx()
    args = {"plane": plane} if offset is None else {"plane": plane, "offset": offset}
    ok = run(nx, "nx_create_section_view", **args)
    assert ok.ok, ok.error
    assert ok.data["origin"] == pytest.approx(origin) and ok.data["normal"] == normal
    assert ok.data["hidden_side"] == hidden and ok.data["enabled"] is True


def test_create_offset_is_in_part_units():
    nx = display_nx("in")
    ok = run(nx, "nx_create_section_view", plane="xy", offset="25.4 mm")
    assert ok.ok and ok.data["origin"] == pytest.approx([0, 0, 1])


def test_create_errors_say_what_to_send():
    nx = display_nx()
    bad = run(nx, "nx_create_section_view", plane="xq")
    assert bad.error["field"] == "/plane" and "'xy'" in bad.error["hint"]
    assert "'-xz'" in bad.error["hint"] and "normal: [x, y, z]" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started"
    near = run(nx, "nx_create_section_view", plane="xzz")
    assert "did you mean 'xz'" in near.error["message"]
    origin_only = run(nx, "nx_create_section_view", plane={"origin": [0, 1, 0]})
    assert origin_only.error["field"] == "/plane/normal" and "offset" in origin_only.error["hint"]
    second = run(nx, "nx_create_section_view", plane=PLANE)
    again = run(nx, "nx_create_section_view", plane="yz")
    assert again.error["code"] == "NX_ALREADY_EXISTS"
    assert second.data["section"]["id"] in str(again.error["details"])


def test_edit_finds_a_section_by_name_and_keeps_the_normal():
    nx = display_nx()
    run(nx, "nx_create_section_view", plane=PLANE, name="mid cut")
    moved = run(nx, "nx_edit_section_view", section="MID CUT", plane={"origin": [0, 40, 0]})
    assert moved.ok, moved.error
    assert moved.data["origin"] == [0, 40, 0] and moved.data["normal"] == [1, 0, 0]
    turned = run(nx, "nx_edit_section_view", section="mid cut", plane={"normal": [0, 0, -1]})
    assert turned.data["origin"] == [0, 40, 0] and turned.data["normal"] == [0, 0, -1]
    assert turned.data["hidden_side"] == "z < 0"
    shifted = run(nx, "nx_edit_section_view", section="mid cut", offset=-5)
    assert shifted.data["origin"] == [0, 40, 5]
    principal = run(nx, "nx_edit_section_view", section="mid cut", plane="xz", offset=30)
    assert principal.data["origin"] == [0, 30, 0] and principal.data["normal"] == [0, 1, 0]
    spaced = run(nx, "nx_edit_section_view", section="Mid_Cut", offset=1)
    assert spaced.ok, spaced.error  # spaces and underscores name the same section


def test_edit_by_unknown_or_repeated_name_lists_the_sections():
    nx = display_nx()
    ref = run(nx, "nx_create_section_view", plane=PLANE, name="A").data["section"]["id"]
    missing = run(nx, "nx_edit_section_view", section="B", cap=False)
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/section"
    assert [r["id"] for r in missing.error["details"]["sections"]] == [ref]
    nx.work_view.ActiveDynamicSection = None
    run(nx, "nx_create_section_view", plane={"origin": [0, 0, 0], "normal": [0, 0, 1]}, name="a")
    two = run(nx, "nx_edit_section_view", section="a", cap=False)
    assert two.error["code"] == "NX_AMBIGUOUS_REFERENCE"
    assert len(two.error["details"]["sections"]) == 2


def test_disabled_section_reports_enabled_false():
    nx = display_nx()
    ref = run(nx, "nx_create_section_view", plane=PLANE).data["section"]["id"]
    off = run(nx, "nx_edit_section_view", section=ref, enabled=False)
    assert off.ok and off.data["enabled"] is False and off.data["sectioning"] is False
    on = run(nx, "nx_edit_section_view", section=ref, enabled=True)
    assert on.data["enabled"] is True


class BackgroundViews(list):
    """A part that is not displayed: NX 2406 refuses its WorkView, its views iterate."""

    @property
    def WorkView(self):
        raise RuntimeError("This operation can only be performed on a displayed part")


def test_listed_in_a_background_part_through_its_section_view():
    """A read bound to a part in the background (gate 4s): no WorkView; the view holding
    the active section reads the plane, and the sectioning toggle is unknown (None)."""
    nx = display_nx()
    run(nx, "nx_create_section_view", plane=PLANE)
    view = nx.work_view
    view.DisplaySectioningToggle = False  # NX reads it off while the part is not displayed
    nx.part.ModelingViews = BackgroundViews([View("Top"), view])
    nx.session.Parts.Display = NS(Tag=-1)  # another part is displayed
    rows = catalogue.hooks("lister")["section_view"](nx.tx, None, None)
    assert len(rows) == 1 and rows[0][1] == "section_view"
    assert rows[0][2]["details"] == {
        "origin": [10, 5, 0], "normal": [1, 0, 0], "cap": True, "active": True,
        "sectioning": None,
    }  # fmt: skip


def test_a_section_an_undo_reverted_or_removed_is_named_on_the_next_call():
    """Live, parallel agents: an edit came back undone and a section vanished without a
    word (NX's session-wide undo reverts sections, which no undo history lists)."""
    nx = display_nx()
    made = run(nx, "nx_create_section_view", plane=PLANE, name="SEC2")
    ref = made.data["section"]["id"]
    run(nx, "nx_edit_section_view", section=ref, plane={"origin": [25, 5, 0]}, cap=False)
    section = nx.part.DynamicSections[0]
    section.origin, section.cap = [10.0, 5.0, 0.0], True  # an undo put the old state back
    off = run(nx, "nx_edit_section_view", section=ref, enabled=False)
    assert off.ok and off.data["origin"] == [10, 5, 0]
    notes = [w["message"] for w in off.warnings if w["code"] == "NX_NOTE"]
    assert len(notes) == 1 and "SEC2 is at origin [10.0, 5.0, 0.0]" in notes[0]
    assert "left at [25.0, 5.0, 0.0]" in notes[0] and "cap False" in notes[0]
    again = run(nx, "nx_edit_section_view", section=ref, enabled=True)
    assert not [w for w in again.warnings if w["code"] == "NX_NOTE"]  # said once
    nx.part.DynamicSections.remove(section)  # an undo removed it
    listed = run(nx, "nx_list_objects", kind="section_view")
    assert listed.ok and listed.data["items"] == []
    assert any("SEC2 is gone" in w["message"] for w in listed.warnings)


def test_a_switched_off_section_lets_create_add_another():
    """Switched off, the active section no longer cuts: a new one becomes the active one,
    and enabled=true on the first switches back to it."""
    nx = display_nx()
    first = run(nx, "nx_create_section_view", plane=PLANE, name="A").data["section"]["id"]
    run(nx, "nx_edit_section_view", section=first, enabled=False)
    second = run(nx, "nx_create_section_view", plane="xy", offset=5, name="B")
    assert second.ok, second.error
    assert second.data["active"] and second.data["sectioning"]
    assert nx.work_view.ActiveDynamicSection.Name == "B" and len(nx.part.DynamicSections) == 2
    back = run(nx, "nx_edit_section_view", section=first, enabled=True)
    assert back.ok and back.data["enabled"] and nx.work_view.ActiveDynamicSection.Name == "A"


def test_deleting_a_section_raises_no_reverted_note():
    """A deleted section's tag no longer reads back: the deleter forgets it by the tags it
    read first, so the listing nx_delete_objects runs afterwards says nothing."""
    nx = display_nx()
    made = run(nx, "nx_create_section_view", plane=PLANE, name="MID_CUT")
    delete = nx.part.DynamicSections.DeleteSections

    def forget_tags(flag, targets):
        delete(flag, targets)
        for target in targets:
            target.Tag = 0  # NX: the deleted object reads another tag

    nx.part.DynamicSections.DeleteSections = forget_tags
    removed = run(nx, "nx_delete_objects", objects=[made.data["section"]["id"]])
    assert removed.ok, removed.error
    assert not [w for w in removed.warnings if w["code"] == "NX_NOTE" and "gone" in w["message"]]
