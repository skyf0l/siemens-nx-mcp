import math
from pathlib import Path

import pytest

from tests.kit import run
from tests.kit.objects import Body
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


# ---- new component in context and make unique ---------------------------------------


def test_new_in_context_creates_the_part_at_the_pose_and_moves_bodies(nx):
    body = Body("SLAB")
    nx.asm.Bodies.append(body)
    body.OwningPart = nx.asm
    body.IsOccurrence = False
    ref = nx.ref_of(body, "body")
    ok = run(nx, "nx_create_component", mode="new_in_context", new_path="parts/new.prt",
             pose={"translation": [5, 0, 0], "rotation": RZ90}, objects_to_move=[ref])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    c = nx.assembly.components()[0]
    assert c.Name == "NEW" and Path(c.Prototype.FullPath).name == "new.prt"
    assert pose_of(ok.data["component"]) == ([5, 0, 0], RZ90)
    assert ok.data["part_path"] == "parts/new.prt" and ok.data["moved_bodies"] == 1
    assert ok.data["units"] == "mm" and ok.data["part"]["kind"] == "part"
    assert body not in nx.asm.Bodies and len(c.Prototype.Bodies) == 1
    builder = nx.assembly.new_builders[0]
    assert builder.destroyed and builder.OriginalObjectsDeleted and builder.ComponentOrigin == 1
    assert builder.NewFile.destroyed and builder.NewFile.TemplateFileName.endswith(
        "mm-template.prt"
    )


def test_new_in_context_argument_rules(nx):
    both = run(nx, "nx_create_component", mode="new_in_context", path="parts/blk.prt",
               new_path="parts/n.prt")  # fmt: skip
    assert both.error["field"] == "/path" and both.mutation_outcome == "not_started"
    none = run(nx, "nx_create_component", mode="new_in_context")
    assert none.error["field"] == "/new_path"
    plain = run(nx, "nx_create_component", path="parts/blk.prt", units="mm")
    assert plain.error["field"] == "/units"
    taken = run(nx, "nx_create_component", mode="new_in_context", new_path="other/blk.prt")
    assert taken.error["code"] == "NX_NAME_CONFLICT" and taken.error["field"] == "/new_path"
    exists = run(nx, "nx_create_component", mode="new_in_context", new_path="parts/blk.prt")
    assert exists.error["code"] == "NX_ALREADY_EXISTS"
    assert not nx.assembly.components()


def test_make_unique_points_one_component_at_a_new_part(nx):
    a = place(nx, nx.blk, "A")
    b = place(nx, nx.blk, "B", (20, 0, 0))
    ok = run(nx, "nx_edit_component", components=[b.ref], make_unique="parts/blk_u.prt")
    assert ok.ok, ok
    assert a.Prototype is nx.blk and Path(b.Prototype.FullPath).name == "blk_u.prt"
    row = ok.data["components"][0]
    assert row["part_path"] == "parts/blk_u.prt" and pose_of(row)[0] == [20, 0, 0]
    two = run(nx, "nx_edit_component", components=[a.ref, b.ref], make_unique="parts/x.prt")
    assert two.error["field"] == "/make_unique" and two.mutation_outcome == "not_started"
    clash = run(nx, "nx_edit_component", components=[a.ref], make_unique="parts/blk_u.prt")
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.error["field"] == "/make_unique"
    both = run(nx, "nx_edit_component", components=[a.ref], make_unique="parts/y.prt",
               replace_with="parts/pin.prt")  # fmt: skip
    assert both.error["field"] == "/make_unique"


# ---- natural values and clearer results (b05) ----------------------------------------


def test_create_component_reports_the_part_ref_name_and_reference_set(nx):
    ok = run(nx, "nx_create_component", path="parts/blk.prt", name="plate_a")
    assert ok.ok, ok
    row = ok.data["component"]
    assert row["name"] == "PLATE_A" and row["reference_set"] == "Entire Part"
    assert ok.data["part"]["kind"] == "part" and ok.data["part"]["name"]


def test_create_component_takes_a_reference_set_in_any_case(nx):
    ok = run(nx, "nx_create_component", path="parts/blk.prt", reference_set="solid")
    assert ok.ok, ok
    assert ok.data["component"]["reference_set"] == "SOLID"
    assert nx.assembly.components()[0].ReferenceSet == "SOLID"
    missing = run(nx, "nx_create_component", path="parts/pin.prt", reference_set="LIGHT")
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/reference_set"
    assert missing.error["details"]["available"] == ["Entire Part", "Empty"]
    assert "nx_activate_part" in missing.error["hint"]
    assert missing.mutation_outcome == "rolled_back" and len(nx.assembly.components()) == 1


def test_create_component_pose_takes_unit_strings_and_axis_angle(nx):
    ok = run(nx, "nx_create_component", path="parts/blk.prt",
             pose={"translation": ["1 in", 0, "5 mm"], "rotation": {"axis": "z", "angle": 90}})  # fmt: skip
    assert ok.ok, ok
    t, r = pose_of(ok.data["component"])
    assert t == pytest.approx([25.4, 0, 5]) and flat(r) == pytest.approx(flat(RZ90))


def test_set_component_pose_takes_axis_angle_and_unit_strings(nx):
    b = place(nx, nx.blk, "B", (30, 10, 0))
    ok = run(nx, "nx_set_component_pose", component=b.ref, mode="relative",
             translation=["10 mm", 0, 0], rotation={"axis": "z", "angle": "90 deg"})  # fmt: skip
    assert ok.ok, ok
    assert b.t == pytest.approx([40, 10, 0]) and flat(b.r) == pytest.approx(flat(RZ90))
    rad = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
              translation=[0, 0, 0], rotation={"axis": "x", "angle": f"{math.pi / 2} rad"})  # fmt: skip
    assert rad.ok, rad
    assert flat(b.r) == pytest.approx([1, 0, 0, 0, 0, -1, 0, 1, 0], abs=1e-12)
    bad_axis = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
               translation=[0, 0, 0], rotation={"axis": "w", "angle": 90})  # fmt: skip
    assert (
        bad_axis.error["category"] == "invalid_argument"
        and bad_axis.mutation_outcome == "not_started"
    )
    extra = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
                translation=[0, 0, 0], rotation={"z": 90})  # fmt: skip
    assert extra.error["category"] == "invalid_argument" and extra.mutation_outcome == "not_started"


def test_edit_component_matches_reference_sets_loosely(nx):
    b = place(nx, nx.blk, "B")
    ok = run(nx, "nx_edit_component", components=[b.ref], reference_set="solid")
    assert ok.ok and b.ReferenceSet == "SOLID", ok
    whole = run(nx, "nx_edit_component", components=[b.ref], reference_set="entire_part")
    assert whole.ok and whole.data["components"][0]["reference_set"] == "Entire Part"
    nope = run(nx, "nx_edit_component", components=[b.ref], reference_set="LIGHT")
    assert nope.error["code"] == "NX_NOT_FOUND" and nope.mutation_outcome == "not_started"
    assert nope.error["details"]["available"] == ["SOLID", "Entire Part", "Empty"]
    assert "own part" in nope.error["hint"]


def test_edit_component_unsuppresses_before_the_reference_set(nx):
    b = place(nx, nx.blk, "B", (5, 0, 0))
    b.IsSuppressed = True
    replace = nx.asm.ComponentAssembly.ReplaceReferenceSet

    def strict(c, name):  # NX 2406: 720173 on a suppressed component
        if c.IsSuppressed:
            raise RuntimeError("Cannot change reference set on suppressed component.")
        replace(c, name)

    nx.asm.ComponentAssembly.ReplaceReferenceSet = strict
    refused = run(nx, "nx_edit_component", components=[b.ref], reference_set="SOLID")
    assert refused.error["code"] == "NX_INVALID_ARGUMENT"
    assert (
        refused.error["field"] == "/components/0" and "suppressed: false" in refused.error["hint"]
    )
    assert refused.mutation_outcome == "not_started"
    ok = run(nx, "nx_edit_component", components=[b.ref], suppressed=False, reference_set="SOLID")
    assert ok.ok, ok
    assert not b.IsSuppressed and b.ReferenceSet == "SOLID"
    off = run(nx, "nx_edit_component", components=[b.ref], suppressed=True, reference_set="Empty")
    assert off.ok and b.IsSuppressed and b.ReferenceSet == "Empty", off


def test_list_components_orders_siblings_by_name(nx):
    z = place(nx, nx.blk, "Z")
    a = place(nx, nx.blk, "A")
    nested = nx.assembly.add(nx.pin, "B", ([0, 0, 0], RZ90), parent=z)
    nested.ref = nx.ref_of(nested, "component")
    m = place(nx, nx.pin, "M")
    ok = run(nx, "nx_list_components")
    assert [r["id"] for r in ok.data["items"]] == [a.ref, m.ref, z.ref, nested.ref]
    assert ok.data["items"][3]["path"] == "Z/B"


def test_reference_set_names_get_a_suggestion(nx):
    a = place(nx, nx.blk, "A")
    bad = run(nx, "nx_create_reference_set", name="Light Set", objects=[a.ref])
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/name"
    assert "'Light_Set'" in bad.error["message"] and bad.mutation_outcome == "not_started"
