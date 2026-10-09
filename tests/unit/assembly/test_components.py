import math

import pytest

from tests.kit import run
from tests.unit.assembly.fakes import assembly_nx, place

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


@pytest.fixture
def nx():
    return assembly_nx()


def flat(m):
    return [v for row in m for v in row]


def pose_of(row):
    return row["translation"], row["rotation"]


def test_create_component_places_the_saved_part_at_the_pose(nx):
    ok = run(nx, "nx_create_component", path="parts/blk.prt", name="a",
             pose={"translation": [10, 0, 5], "rotation": RZ90})  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    c = nx.assembly.components()[0]
    assert c.Prototype is nx.blk and c.Name == "A" and c.Parent is nx.assembly.root
    assert ok.data["component"]["path"] == "A" and ok.data["part_path"] == "parts/blk.prt"
    assert pose_of(ok.data["component"]) == ([10, 0, 5], RZ90)
    assert nx.undo_marks and ok.changes["references"] == "preserved"


def test_create_component_refuses_itself_missing_files_and_basename_clashes(nx):
    nx.asm.Save()
    itself = run(nx, "nx_create_component", path="asm.prt")
    assert itself.error["field"] == "/path" and itself.mutation_outcome == "not_started"
    missing = run(nx, "nx_create_component", path="parts/none.prt")
    assert missing.error["code"] == "NX_FILE_NOT_FOUND"
    other = nx.root / "other" / "blk.prt"
    other.parent.mkdir()
    other.write_bytes(b"x")
    clash = run(nx, "nx_create_component", path="other/blk.prt")
    assert clash.error["code"] == "NX_NAME_CONFLICT" and clash.mutation_outcome == "not_started"
    assert not nx.assembly.components()


def test_create_component_read_back_failure_rolls_back(nx, monkeypatch):
    add = nx.asm.ComponentAssembly.AddComponent

    def shifted(path, refset, name, origin, m, layer):
        c, status = add(path, refset, name, origin, m, layer)
        c.t = [c.t[0] + 1, c.t[1], c.t[2]]
        return c, status

    nx.asm.ComponentAssembly.AddComponent = shifted
    bad = run(nx, "nx_create_component", path="parts/blk.prt")
    assert bad.error["category"] == "verification_failed"
    assert bad.error["details"]["check"] == "pose.translation"
    assert bad.mutation_outcome == "rolled_back" and not nx.assembly.components()


def test_list_components_pages_filters_and_reports_load_state(nx):
    a = place(nx, nx.blk, "A")
    b = place(nx, nx.blk, "B", (60, 0, 0), RZ90)
    sub = place(nx, nx.pin, "SUB")
    nested = nx.assembly.add(nx.pin, "N", ([0, 0, 1], RZ90), parent=sub)
    nested.Prototype = None
    b.IsSuppressed = True
    ok = run(nx, "nx_list_components", include_poses=True, limit=2)
    assert ok.page["total"] == 4 and ok.page["next_offset"] == 2
    first, second = ok.data["items"]
    assert first["id"] == a.ref and first["depth"] == 0 and first["load_state"] == "fully_loaded"
    assert first["part_path"] == "parts/blk.prt" and first["reference_set"] == "Entire Part"
    assert second["suppressed"] and pose_of(second) == ([60, 0, 0], RZ90)
    rest = run(nx, "nx_list_components", offset=2)
    assert rest.data["items"][1]["path"] == "SUB/N" and rest.data["items"][1]["depth"] == 1
    assert rest.data["items"][1]["load_state"] == "unloaded"
    assert rest.data["items"][1]["part_path"] == "D:/missing/part.prt"
    assert "translation" not in rest.data["items"][0]
    assert run(nx, "nx_list_components", suppressed=True).page["total"] == 1
    assert run(nx, "nx_list_components", name_contains="su").page["total"] == 1


def test_absolute_pose_compensates_the_assembly_origin_pivot(nx):
    """NX 2406 MoveComponent rotates about the assembly origin, then translates."""
    b = place(nx, nx.blk, "B", (20, 0, 0))
    ok = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
             translation=[30, 10, 0], rotation=RZ90)  # fmt: skip
    assert ok.ok, ok
    assert b.t == pytest.approx([30, 10, 0]) and flat(b.r) == pytest.approx(flat(RZ90))
    assert ok.data["previous"]["translation"] == [20, 0, 0]
    again = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
                translation=[30, 10, 0], rotation=RZ90)  # fmt: skip
    assert again.ok and b.t == pytest.approx([30, 10, 0])


def test_relative_pose_turns_about_the_component_origin(nx):
    b = place(nx, nx.blk, "B", (30, 10, 0))
    ok = run(nx, "nx_set_component_pose", component=b.ref, mode="relative",
             translation=[5, 0, 0], rotation=RZ90)  # fmt: skip
    assert ok.ok, ok
    assert b.t == pytest.approx([35, 10, 0]) and flat(b.r) == pytest.approx(flat(RZ90))
    turned = run(nx, "nx_set_component_pose", component=b.ref, mode="relative",
                 translation=[0, 0, 0], rotation=RZ90)  # fmt: skip
    assert turned.ok and flat(b.r) == pytest.approx([-1, 0, 0, 0, -1, 0, 0, 0, 1])


def test_pose_mismatch_rolls_back_and_rejects_bad_input(nx):
    b = place(nx, nx.blk, "B", (20, 0, 0))
    nx.assembly.pivot_at_origin = False  # a build that pivots elsewhere
    bad = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
              translation=[20, 0, 0], rotation=RZ90)  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    assert b.t == [20, 0, 0] and b.r == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    skew = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
               translation=[0, 0, 0], rotation=[[1, 0, 0], [0, 1, 0], [0, 0, -1]])  # fmt: skip
    assert skew.error["field"] == "/rotation" and skew.mutation_outcome == "not_started"
    nested = nx.assembly.add(nx.pin, "N", ([0, 0, 0], RZ90), parent=b)
    ref = nx.ref_of(nested, "component")
    deep = run(nx, "nx_set_component_pose", component=ref, mode="relative", translation=[1, 0, 0])
    assert deep.error["code"] == "NX_WRONG_OWNER" and deep.mutation_outcome == "not_started"


def test_edit_component_renames_suppresses_and_sets_reference_set(nx):
    b = place(nx, nx.blk, "B", (20, 0, 0), RZ90)
    ok = run(nx, "nx_edit_component", components=[b.ref], name="b2", reference_set="SOLID")
    assert ok.ok, ok
    row = ok.data["components"][0]
    assert b.Name == "B2" and row["reference_set"] == "SOLID" and pose_of(row)[0] == [20, 0, 0]
    off = run(nx, "nx_edit_component", components=[b.ref], suppressed=True)
    assert off.ok and b.IsSuppressed and off.data["components"][0]["suppressed"]
    on = run(nx, "nx_edit_component", components=[b.ref], suppressed=False)
    assert on.ok and not b.IsSuppressed


def test_edit_component_replaces_the_part_and_keeps_the_placement(nx):
    b = place(nx, nx.blk, "B", (10, 0, 0))
    ok = run(nx, "nx_edit_component", components=[b.ref], replace_with="parts/pin.prt")
    assert (
        ok.ok and b.Prototype is nx.pin and ok.data["components"][0]["part_path"] == "parts/pin.prt"
    )
    builder = nx.assembly.replace_builders[-1]
    assert builder.destroyed and builder.MaintainRelationships and not builder.ReplaceAllOccurrences
    missing_set = run(nx, "nx_edit_component", components=[b.ref], reference_set="SOLID")
    assert (
        missing_set.error["code"] == "NX_NOT_FOUND"
        and missing_set.mutation_outcome == "not_started"
    )
    assert missing_set.error["details"]["available"] == ["Entire Part", "Empty"]


def test_edit_component_refuses_moves_and_bad_requests(nx):
    a = place(nx, nx.blk, "A")
    b = place(nx, nx.blk, "B", (10, 0, 0))
    nothing = run(nx, "nx_edit_component", components=[a.ref])
    assert (
        nothing.error["code"] == "NX_INVALID_ARGUMENT" and nothing.mutation_outcome == "not_started"
    )
    two = run(nx, "nx_edit_component", components=[a.ref, b.ref], name="X")
    assert two.error["field"] == "/name"
    twice = run(nx, "nx_edit_component", components=[a.ref, a.ref], suppressed=True)
    assert twice.error["field"].startswith("/components/")
    replace = nx.asm.ComponentAssembly.ReplaceReferenceSet

    def moving(c, name):
        replace(c, name)
        c.t = [c.t[0], c.t[1] + 1, c.t[2]]

    nx.asm.ComponentAssembly.ReplaceReferenceSet = moving
    moved = run(nx, "nx_edit_component", components=[b.ref], reference_set="SOLID")
    assert moved.error["details"]["check"].startswith("placement[0]")
    assert moved.mutation_outcome == "rolled_back"
    assert b.t == [10, 0, 0] and b.ReferenceSet == "Entire Part"


def test_component_tools_need_an_assembly(nx):
    nx.asm.ComponentAssembly.RootComponent = None
    ok = run(nx, "nx_edit_component", components=["o:1"], suppressed=True)
    assert ok.error["code"] == "NX_WRONG_PART_CONTEXT" and ok.mutation_outcome == "not_started"
    assert math.isfinite(1.0)
