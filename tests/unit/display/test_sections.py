"""Dynamic section views: create, edit, rollback, nx_list_objects rows and the deleter."""

import pytest

from nx_mcp.core import catalogue
from tests.kit import NXException, run
from tests.unit.display.fakes import display_nx, show_sheet

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
    assert run(nx, "nx_edit_section_view", section=body.ref).error["field"] == "/plane"
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
    assert data["hidden_side"] == "positive"
    assert data["cut_face_view_from"] == pytest.approx([0, -0.6, 0.8])
    assert data["remove_with"] == {
        "tool": "nx_delete_objects",
        "arguments": {"objects": [data["section"]["id"]]},
    }
    removed = run(nx, data["remove_with"]["tool"], **data["remove_with"]["arguments"])
    assert removed.ok and not nx.part.DynamicSections
    assert nx.work_view.DisplaySectioningToggle is False
