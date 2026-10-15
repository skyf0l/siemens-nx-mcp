"""nx_set_material is undone by compensation: the bodies get their materials back."""

from tests.kit import run
from tests.unit.modeling.test_material_library import library_nx


def test_a_material_assignment_is_set_back_by_compensation():
    nx = library_nx()
    block = nx.block(10, 10, 10)
    steel = run(nx, "nx_set_material", bodies=[block.ref], material="Steel")
    assert steel.ok, steel
    alu = run(nx, "nx_set_material", bodies=[block.ref], material="Aluminum_6061")
    assert alu.ok, alu
    undo = run(nx, "nx_undo_operation", operation=alu.operation_id, mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert undo.data["restored"] == ["the material of 1 bodies back to Steel"]
    assert nx.materials.AskMaterialOfObject(block).Name == "Steel"


def test_a_material_changed_since_is_never_overwritten():
    nx = library_nx()
    block = nx.block(10, 10, 10)
    assert run(nx, "nx_set_material", bodies=[block.ref], material="Steel").ok
    alu = run(nx, "nx_set_material", bodies=[block.ref], material="Aluminum_6061")
    assert run(nx, "nx_set_material", bodies=[block.ref], material="ABS").ok
    refused = run(nx, "nx_undo_operation", operation=alu.operation_id, mode="compensate")
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert nx.materials.AskMaterialOfObject(block).Name == "ABS"


def test_a_body_without_a_material_before_is_refused():
    nx = library_nx()
    block = nx.block(10, 10, 10)
    first = run(nx, "nx_set_material", bodies=[block.ref], material="Steel")
    assert first.ok, first
    refused = run(nx, "nx_undo_operation", operation=first.operation_id, mode="compensate")
    assert (
        refused.error["code"] == "NX_UNSUPPORTED_OPTION"
        and "no material" in refused.error["message"]
    )
    assert nx.materials.AskMaterialOfObject(block).Name == "Steel"


def test_sheet_bodies_are_marked_and_left_out_of_the_no_material_note():
    nx = library_nx()
    nx.block(10, 10, 10)
    sheet = nx.block(10, 10, 0, origin=(50, 0, 0))
    sheet.IsSolidBody, sheet.IsSheetBody = False, True
    read = run(nx, "nx_get_material")
    assert [r["solid"] for r in read.data["items"]] == [True, False], read
    notes = [w["message"] for w in read.warnings]
    assert "1 of 1 bodies have no material" in notes[0] and "sheet bodies" in notes[1]
