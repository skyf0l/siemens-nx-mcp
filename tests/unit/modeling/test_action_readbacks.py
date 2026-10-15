"""Per-action read-backs and early refusals of the modeling tools (draft, draft body, patch,
face move and replace, face relations, trim, threads, materials, measurements, datums,
face blend, holes, sweeps, emboss body, blend order, unsew, blend corner) on the shared
fake, through the real kernel."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.modeling import features as modeling_features
from nx_mcp.domains.modeling import measure_feature
from tests.kit import run
from tests.kit.objects import NXException, Object, point
from tests.unit.modeling.fakes import call, feature, modeling_nx, new_body
from tests.unit.modeling.test_details_bodies import tilts
from tests.unit.modeling.test_holes_threads import table  # noqa: F401 (thread table fixture)

# ---- draft ------------------------------------------------------------------------------


def _draft_block():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    side, bottom = block.face((1, 0, 0)), block.face((0, 0, -1))
    return nx, block, side, bottom


def test_draft_refuses_a_face_that_is_no_wall_of_the_pull_direction():
    nx, _block, side, bottom = _draft_block()
    bad = run(nx, "nx_create_draft", faces=[side.ref], stationary_face=bottom.ref,
              direction=[1, 0, 0], angle=3)  # fmt: skip
    assert bad.error["field"] == "/direction" and bad.mutation_outcome == "not_started"
    assert "pull" in bad.error["hint"] and nx.builders == []
    same = run(nx, "nx_create_draft", faces=[bottom.ref], stationary_face=bottom.ref,
               direction="z", angle=3)  # fmt: skip
    assert same.error["field"] == "/faces/0" and same.mutation_outcome == "not_started"


def test_draft_reads_each_wall_angle_back():
    nx, block, side, bottom = _draft_block()
    tilts(nx, block, side, 2, 7950.0)  # NX tilted the wall by 2, not 5
    bad = run(nx, "nx_create_draft", faces=[side.ref], stationary_face=bottom.ref,
              direction="z", angle=5)  # fmt: skip
    assert bad.error["details"]["check"] == "angle" and bad.mutation_outcome == "rolled_back"


def test_draft_warns_when_the_volume_changes_a_lot():
    nx, block, side, bottom = _draft_block()
    tilts(nx, block, side, 5, 3000.0)
    ok = run(nx, "nx_create_draft", faces=[side.ref], stationary_face=bottom.ref,
             direction="z", angle=5)  # fmt: skip
    assert ok.ok, ok
    assert any("volume" in w["message"] for w in ok.warnings)


# ---- draft body -------------------------------------------------------------------------


def _taper(nx, body, angles):
    def commit(_b):
        body.volume += 1000.0
        f = feature(nx, "BODY_TAPER", [body])
        f.own_faces = []
        for a in angles:
            s, c = math.sin(math.radians(a)), math.cos(math.radians(a))
            f.own_faces.append(nx.remember(nx.face(normal=(c, 0.0, s), body=body)))
        return f

    nx.on_commit("DraftBodyBuilder", commit)


def test_draft_body_refuses_faces_wholly_on_one_side_of_the_parting():
    nx = modeling_nx()
    body = nx.block(100, 60, 40)
    walls = [f for f in body.faces if f.normal[2] == 0]
    for f in walls:
        f.box = [0, 0, 0, 100, 60, 40]
    bad = run(nx, "nx_create_draft_body", faces=[f.ref for f in walls],
              parting={"origin": [0, 0, 50], "normal": [0, 0, 1]}, direction="z", angle=3,
              match="none")  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/faces/0"
    assert bad.mutation_outcome == "not_started" and "parting" in bad.error["hint"]


def test_draft_body_takes_faces_on_one_side_beside_faces_that_cross():
    """A sloped wall drafted again about a lower parting: its upper faces lie above the
    plane, its lower faces cross it; NX matches the halves."""
    nx = modeling_nx()
    body = nx.block(100, 60, 40)
    _taper(nx, body, [8, 8])
    walls = [f for f in body.faces if f.normal[2] == 0]
    walls[0].box = [0, 0, 20, 100, 0, 40]  # above the parting at z 10
    walls[1].box = [0, 0, 0, 100, 0, 20]  # crosses it
    ok = run(nx, "nx_create_draft_body", faces=[walls[0].ref, walls[1].ref],
             parting={"origin": [0, 0, 10], "normal": [0, 0, 1]}, direction="z", angle=8)  # fmt: skip
    assert ok.ok, ok


def test_draft_body_lists_off_angle_faces_and_warns():
    nx = modeling_nx()
    body = nx.block(20, 20, 30)
    _taper(nx, body, [2, 2, 3.99, 3.99])
    ok = run(nx, "nx_create_draft_body", faces=[f.ref for f in body.faces if f.normal[2] == 0],
             parting={"origin": [0, 0, 5], "normal": [0, 0, 1]}, direction="z", angle=2,
             method="true_draft")  # fmt: skip
    assert ok.ok, ok
    flags = sorted((round(r["angle"], 2), r["at_angle"]) for r in ok.data["faces"])
    assert flags == [(2.0, True), (2.0, True), (3.99, False), (3.99, False)]
    assert any(w["code"] == "NX_APPROXIMATED" and "3.99" in w["message"] for w in ok.warnings)


def test_draft_body_warns_when_the_body_grows_along_the_draw_direction():
    nx = modeling_nx()
    body = nx.block(20, 20, 20)
    _taper(nx, body, [3, 3])
    commit = nx.hooks["DraftBodyBuilder"]

    def grown(b):
        body.box = [0, 0, 0, 20, 20, 50]
        return commit(b)

    nx.on_commit("DraftBodyBuilder", grown)
    ok = run(nx, "nx_create_draft_body", faces=[f.ref for f in body.faces if f.normal[2] == 0],
             parting={"origin": [0, 0, 10], "normal": [0, 0, 1]}, direction="z", angle=3,
             match="none")  # fmt: skip
    assert ok.ok and any("far edges" in w["message"] for w in ok.warnings), ok


# ---- patch hole --------------------------------------------------------------------------


def _sheet(nx, loops):
    """A sheet whose free edges form ``loops`` closed boundary loops."""
    body = new_body(nx, [0, 0, 0, 10, 10, 10], volume=0.0)
    body.IsSolidBody, body.IsSheetBody = False, True
    body.faces = [nx.face(body=body)]
    face = body.faces[0]
    edges = []
    for k in range(loops):
        p = (0.0, 0.0, 10.0 * k)
        edges.append(NS(GetFaces=lambda f=face: [f], GetVertices=lambda p=p: [point(*p)] * 2))
    body.edges = edges
    body.ref = nx.ref_of(body, "body")
    return body


def _patches(nx, target, volume):
    def hook(_b):
        target.volume = volume
        return feature(nx, "PATCH", [target])

    nx.on_commit("PatchBuilder", hook)


def test_hole_patch_needs_a_tube_sheet_and_must_remove_material():
    nx = modeling_nx()
    block = nx.block(40, 30, 20)
    cup = _sheet(nx, 1)
    bad = run(nx, "nx_create_patch_body", body=block.ref, sheet=cup.ref, hole=True)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/sheet"
    assert bad.error["details"]["open_loops"] == 1 and bad.mutation_outcome == "not_started"
    tube = _sheet(nx, 2)
    _patches(nx, block, 48000.0)  # NX added material: not a hole
    grew = run(nx, "nx_create_patch_body", body=nx.ref_of(block, "body"), sheet=tube.ref,
               hole=True)  # fmt: skip
    assert grew.error["details"]["check"] == "volume" and grew.mutation_outcome == "rolled_back"
    _patches(nx, block, 22429.2)
    ok = run(nx, "nx_create_patch_body", body=nx.ref_of(block, "body"),
             sheet=nx.ref_of(tube, "body"), hole=True)  # fmt: skip
    assert ok.ok and ok.data["volume"] == pytest.approx(22429.2), ok


# ---- edit_faces move and replace -----------------------------------------------------------


def _with_hole():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    hole = nx.remember(nx.face(normal=(0, 0, 1), origin=(10, 10, 0), kind="cylinder",
                               body=block))  # fmt: skip
    hole.radius = 4.0
    block.faces.append(hole)
    return nx, block, hole


def _moves(nx, kind, block, face, origin):
    def hook(_b):
        if origin is not None:
            kind_, _o, direction = nx.face_data[face.Tag]
            nx.face_data[face.Tag] = (kind_, origin, direction)
        return feature(nx, kind, [block])

    nx.on_commit(kind, hook)


def test_a_volume_neutral_move_reads_the_face_position_back():
    nx, block, hole = _with_hole()
    _moves(nx, "AdmMoveFaceBuilder", block, hole, (10, 18, 0))
    ok = run(nx, "nx_edit_faces", faces=[hole.ref], action="move", distance=8, direction="y")
    assert ok.ok and ok.data["volume_change"] == 0, ok
    _moves(nx, "AdmMoveFaceBuilder", block, hole, None)  # NX left the hole where it was
    bad = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="move", distance=8,
              direction="x")  # fmt: skip
    assert bad.error["details"]["check"] == "moved[0]" and bad.mutation_outcome == "rolled_back"


def test_replace_reads_the_face_onto_the_replacement_surface():
    nx, block, _hole = _with_hole()
    other = nx.block(10, 10, 15, origin=(100, 0, 0))
    top, target = block.face((0, 0, 1)), other.face((0, 0, 1))
    _moves(nx, "ReplaceFaceBuilder", block, top, (0, 0, 15))
    ok = run(nx, "nx_edit_faces", faces=[top.ref], action="replace", replacement=target.ref)
    assert ok.ok, ok
    nx.face_data[top.Tag] = ("plane", (0, 0, 10), (0, 0, 1))
    _moves(nx, "ReplaceFaceBuilder", block, top, None)
    bad = run(nx, "nx_edit_faces", faces=[nx.ref_of(top, "face")], action="replace",
              replacement=nx.ref_of(target, "face"))  # fmt: skip
    assert bad.error["details"]["check"] == "replaced[0]"


def test_delete_blend_maps_no_face_selected_and_names_delete():
    nx, _block, hole = _with_hole()
    nx.refuse("DeleteFaceBuilder", code=3190049, message="No face selected for operation.")
    bad = run(nx, "nx_edit_faces", faces=[hole.ref], action="delete_blend")
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "'delete'" in bad.error["hint"]
    assert bad.error["nx_code"] == 3190049


def test_delete_blend_refuses_faces_nx_does_not_see_as_blends():
    nx, _block, hole = _with_hole()
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "ResizeBlendBuilder":
            object.__setattr__(b, "IsBlendFace", lambda faces: [False for _ in faces])
        return b

    nx.builder = builder
    bad = run(nx, "nx_edit_faces", faces=[hole.ref], action="delete_blend")
    assert bad.error["field"] == "/faces/0" and bad.mutation_outcome == "not_started"
    assert not [b for b in nx.builders if b.kind == "DeleteFaceBuilder"]


# ---- face relations ---------------------------------------------------------------------


def test_tangent_cylinder_to_plane_expects_a_right_angle():
    nx = modeling_nx()
    a = nx.block(40, 20, 10)
    pin = nx.remember(nx.face(normal=(0, 0, 1), origin=(80, 10, 0), kind="cylinder", body=a))
    pin.radius = 5.0
    a.faces.append(pin)
    wall = nx.block(10, 20, 10, origin=(100, 0, 0)).face((-1, 0, 0))

    def hook(_b):
        nx.face_data[pin.Tag] = ("cylinder", (95, 10, 0), (0, 0, 1))
        return feature(nx, "TANGENT", [a])

    nx.on_commit("TangentBuilder", hook)
    ok = run(nx, "nx_edit_face_relation", face=nx.ref_of(pin, "face"), relation="tangent",
             to=nx.ref_of(wall, "face"))  # fmt: skip
    assert ok.ok and ok.data["angle"] == pytest.approx(90.0) and ok.data["gap"] == 0, ok


def test_coplanar_to_a_face_nx_merges_away_reads_its_plane_from_before():
    nx = modeling_nx()
    a = nx.block(40, 20, 10)
    low = a.face((0, 0, 1))
    high = nx.remember(nx.face(normal=(0, 0, 1), origin=(0, 0, 15), body=a))
    a.faces.append(high)

    def hook(_b):
        nx.face_data[low.Tag] = ("plane", (0, 0, 15), (0, 0, 1))
        del nx.face_data[high.Tag]  # NX deleted the face it merged into
        return feature(nx, "COPLANAR", [a])

    nx.on_commit("CoplanarBuilder", hook)
    ok = run(nx, "nx_edit_face_relation", face=nx.ref_of(low, "face"), relation="coplanar",
             to=nx.ref_of(high, "face"))  # fmt: skip
    assert ok.ok and ok.data["gap"] == 0, ok


# ---- trim with a face tool ------------------------------------------------------------------


def test_trim_front_with_a_face_tool_reverses_a_fixed_plane():
    nx = modeling_nx()
    made = []
    nx.part.Planes = NS(CreatePlane=lambda o, n, _u: made.append(NS(
        origin=o, normal=n, SetMethod=lambda m: made.append(("method", m)),
        SetGeometry=lambda g: made.append(("geometry", g)), Evaluate=lambda: None)) or made[-1])  # fmt: skip
    block = nx.block(40, 20, 20)
    tool = nx.block(10, 10, 10, origin=(100, 0, 0)).face((0, 0, 1))

    def trimmed(_b):
        block.box, block.volume, block.centroid = [0, 0, 10, 40, 20, 20], 8000.0, None
        return feature(nx, "TRIM_BODY", [block])

    nx.on_commit("TrimBody2Builder", trimmed)
    ok = call(nx, "nx_create_trim_body", {"body": block.ref, "tool": tool.ref, "keep": "front"})
    assert ok.ok, ok
    plane = next(m for m in made if isinstance(m, NS))
    assert plane.normal.Z == -1 and not [m for m in made if isinstance(m, tuple)]


# ---- threads ----------------------------------------------------------------------------


def test_thread_axis_refusal_is_retried_reversed(table):  # noqa: F811
    from tests.unit.modeling.test_holes_threads import thread_nx

    nx, _body, cyl, start = thread_nx()
    refusals = [NXException(671154, "Invalid thread axis direction.")]
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "ThreadBuilder" and refusals:
            object.__setattr__(b, "error", refusals.pop())
        return b

    nx.builder = builder
    ok = run(nx, "nx_create_thread", face=cyl.ref, spec={"size": "M6"}, start_face=start.ref,
             length=8)  # fmt: skip
    assert ok.ok, ok
    built = [b.ReverseThreadDirection for b in nx.builders if b.kind == "ThreadBuilder"]
    assert built == [False, True]


# ---- materials --------------------------------------------------------------------------


def test_a_custom_material_named_like_a_library_one_is_refused():
    from tests.unit.modeling.test_material_library import library_nx

    nx = library_nx()
    block = nx.block(10, 10, 10)
    bad = run(nx, "nx_set_material", bodies=[block.ref],
              material={"custom": {"name": "steel", "density": 5000}})  # fmt: skip
    assert bad.error["code"] == "NX_ALREADY_EXISTS" and bad.mutation_outcome == "not_started"
    assert bad.error["details"]["existing"]["material"] == "Steel"
    assert "{library: 'Steel'}" in bad.error["hint"]


def test_stainless_words_and_grade_numbers_find_stainless_grades():
    from nx_mcp.domains.modeling import material
    from tests.unit.modeling.test_material_library import LIBRARY, library_nx

    names = {**LIBRARY, "AISI_SS_304-Annealed": {"Category": "METAL"},
             "AISI_410_SS": {"Category": "METAL"}, "S/Steel_PH15-5": {"Category": "METAL"},
             "AISI_Steel_4340": {"Category": "METAL"}}  # fmt: skip
    nx = library_nx(names)
    found = run(nx, "nx_list_materials", query="stainless")
    assert {r["name"] for r in found.data["items"]} == {
        "AISI_SS_304-Annealed", "AISI_410_SS", "S/Steel_PH15-5"
    }  # fmt: skip
    inox = run(nx, "nx_list_materials", query="inox 304")
    assert [r["name"] for r in inox.data["items"]] == ["AISI_SS_304-Annealed"]
    steel = run(nx, "nx_list_materials", query="steel")
    assert "AISI_410_SS" in {r["name"] for r in steel.data["items"]}
    _name, candidates = material.library_match(list(names), "Stainless Steel 304")
    assert candidates[0] == "AISI_SS_304-Annealed"


# ---- measurement ------------------------------------------------------------------------


def test_measurement_values_are_read_in_their_own_units_and_weight_is_mass_times_g():
    nx = modeling_nx()
    exp = nx.expression("p9", "body_measurement3.weight", 2854.57)  # base units: mN
    exp.expression_value = 2.85457
    assert measure_feature._value(nx.tx, exp) == pytest.approx(2.85457)
    rows = [
        {"quantity": "mass", "value": 0.291085, "units": "kg"},
        {"quantity": "weight", "value": 2.85457, "units": "N"},
    ]
    measure_feature._check_weight(rows)
    rows[1]["value"] = 2854.57
    with pytest.raises(NXError) as error:
        measure_feature._check_weight(rows)
    assert error.value.details["check"] == "weight"


# ---- datums -----------------------------------------------------------------------------


def test_datum_plane_refuses_an_argument_of_another_method():
    nx = modeling_nx()
    bad = run(nx, "nx_create_datum_plane", method="offset", reference="xz", distance=10,
              points=[[0, 0, 0], [1, 0, 0], [0, 1, 0]])  # fmt: skip
    assert bad.error["field"] == "/points" and "three_points" in bad.error["message"]
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def _axis_feature(nx, origin, direction):
    f = feature(nx, "DATUM_AXIS")
    axis = Object("DATUM_AXIS")
    axis.Origin, axis.Direction = point(*origin), point(*direction)
    f.DatumAxis = axis
    return f


def test_datum_axis_on_a_cylinder_face_and_where_two_planes_meet():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    bore = nx.remember(nx.face(normal=(0, 0, 1), origin=(10, 10, 0), kind="cylinder",
                               body=block))  # fmt: skip
    nx.on_commit("DatumAxisBuilder", lambda b: _axis_feature(nx, (10, 10, 5), (0, 0, -1)))
    ok = run(nx, "nx_create_datum_axis", method="reference", reference=nx.ref_of(bore, "face"))
    assert ok.ok and ok.data["direction"] == [0, 0, -1], ok
    b = nx.last("DatumAxisBuilder")
    assert b.Type is nx.nx.Features.DatumAxisBuilder.Types.CurveOrFaceAxis
    assert b.CurveOrFace.Value is bore and b.IsAssociative is True
    top, side = block.face((0, 0, 1)), block.face((1, 0, 0))
    nx.on_commit("DatumAxisBuilder", lambda b: _axis_feature(nx, (40, 7, 10), (0, 1, 0)))
    line = run(nx, "nx_create_datum_axis", method="intersection",
               planes=[nx.ref_of(top, "face"), nx.ref_of(side, "face")])  # fmt: skip
    assert line.ok, line
    assert nx.last("DatumAxisBuilder").Type is nx.nx.Features.DatumAxisBuilder.Types.Intersection
    nx.on_commit("DatumAxisBuilder", lambda b: _axis_feature(nx, (0, 7, 10), (0, 1, 0)))
    off = run(nx, "nx_create_datum_axis", method="intersection",
              planes=[nx.ref_of(top, "face"), nx.ref_of(side, "face")])  # fmt: skip
    assert off.error["details"]["check"] == "on_line"
    parallel = run(nx, "nx_create_datum_axis", method="intersection",
                   planes=[nx.ref_of(top, "face"), nx.ref_of(block.face((0, 0, -1)), "face")])  # fmt: skip
    assert parallel.error["field"] == "/planes/1" and parallel.mutation_outcome == "not_started"
    stray = run(nx, "nx_create_datum_axis", method="reference", reference=nx.ref_of(bore, "face"),
                origin=[0, 0, 0])  # fmt: skip
    assert stray.error["field"] == "/origin" and stray.mutation_outcome == "not_started"


# ---- face blend -------------------------------------------------------------------------


def test_trimmed_face_blend_between_two_solids_is_refused_before_nx():
    nx = modeling_nx()
    a = nx.block(40, 20, 10)
    b = nx.block(10, 20, 30, origin=(40, 0, 0))
    bad = run(nx, "nx_create_face_blend", faces1=[a.face((0, 0, 1)).ref],
              faces2=[b.face((-1, 0, 0)).ref], radius=5)  # fmt: skip
    assert bad.error["field"] == "/faces2" and "unite" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started" and nx.builders == []


# ---- hole -------------------------------------------------------------------------------


def test_a_blind_hole_deeper_than_the_material_warns_and_drops_nominal(table):  # noqa: F811
    from tests.unit.modeling.test_holes_threads import hole_nx

    nx, _block, top, _B = hole_nx()
    ok = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 20]], diameter=6, depth=30)
    assert ok.ok and ok.data["nominal"] is None and ok.data["depth"] == 30, ok
    assert any("breaks through" in w["message"] for w in ok.warnings)


# ---- sweeps -----------------------------------------------------------------------------


def test_equal_wall_offsets_are_refused():
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch(origin=(0, 0, 50))
    bad = run(nx, "nx_create_sweep_along_guide", section=section.ref, guide=guide.ref,
              first_offset=2, second_offset=2)  # fmt: skip
    assert bad.error["field"] == "/second_offset" and bad.mutation_outcome == "not_started"


def test_an_open_section_is_refused_before_a_solid_sweep(monkeypatch):
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch(origin=(0, 0, 50))
    loops = {"closed": 0, "open": 1, "branched": 0, "gaps": []}
    monkeypatch.setattr(modeling_features, "section_loops", lambda tx, sk: loops)
    bad = run(nx, "nx_create_sweep", section=section.ref, guide=guide.ref)
    assert bad.error["field"] == "/section" and "sweep_along_guide" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started"
    guided = run(nx, "nx_create_sweep_along_guide", section=section.ref, guide=guide.ref)
    assert guided.error["field"] == "/section" and "offset" in guided.error["hint"]


# ---- emboss body ------------------------------------------------------------------------


def test_emboss_body_refuses_a_tool_that_misses_the_target():
    nx = modeling_nx()
    target = nx.block(40, 20, 10)
    tool = nx.block(5, 5, 5, origin=(500, 500, 500))
    bad = run(nx, "nx_create_emboss_body", target=target.ref, tool_body=tool.ref)
    assert bad.error["field"] == "/tool_body" and bad.mutation_outcome == "not_started"


def test_emboss_body_on_a_sheet_reports_its_area_and_bounds():
    nx = modeling_nx()
    areas = iter([100.0, 140.0])
    nx.part.MeasureManager.NewFaceProperties = lambda *a: NS(Area=next(areas),
                                                             Dispose=lambda: None)  # fmt: skip
    sheet = new_body(nx, [0, 0, 0, 40, 40, 0], volume=0.0)
    sheet.IsSolidBody, sheet.IsSheetBody = False, True
    sheet.faces = [nx.face(body=sheet)]
    tool = nx.block(10, 10, 10, origin=(15, 15, -5))

    def bump(_b):
        sheet.box = [0, 0, -7.5, 40, 40, 0]
        return feature(nx, "EMBOSS_BODY", [sheet])

    nx.on_commit("EmbossBodyBuilder", bump)
    ok = run(nx, "nx_create_emboss_body", target=nx.ref_of(sheet, "body"), tool_body=tool.ref)
    assert ok.ok and ok.data["sheet"] is True, ok
    assert ok.data["area"] == 140.0 and ok.data["area_before"] == 100.0
    assert ok.data["bounds"] == [[0, 0, -7.5], [40, 40, 0]]


# ---- blend order ------------------------------------------------------------------------


def test_blend_order_tries_the_swapped_pair_and_returns_the_faces_used():
    from tests.unit.modeling.test_sync_blends import BUILDER, corner, reorders

    nx, a, concave, convex, _asked = corner()
    reorders(nx, a, a.volume - 11.7)
    refusals = [NXException(3190185, "The blends cannot be reordered.")]
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == BUILDER and refusals:
            object.__setattr__(b, "error", refusals.pop())
        return b

    nx.builder = builder
    ok = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"),
             other=nx.ref_of(convex, "face"))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["face"]["id"] == nx.ref_of(convex, "face")
    assert ok.data["partner"]["id"] == nx.ref_of(concave, "face")


def test_blend_order_refusal_names_other_and_the_corner_face():
    from tests.unit.modeling.test_sync_blends import BUILDER, corner

    nx, _a, concave, convex, _asked = corner()
    nx.refuse(BUILDER, 3190185, "The blends cannot be reordered.")
    no = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"),
             other=nx.ref_of(convex, "face"))  # fmt: skip
    assert no.error["field"] == "/other" and "corner" in no.error["hint"]


# ---- unsew ------------------------------------------------------------------------------


def test_unsew_rows_say_which_sheet_holds_the_faces_given():
    from tests.unit.modeling.test_repair import _areas, _unsews

    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    side = block.face((1, 0, 0))
    _unsews(nx, block, [[side]])
    ok = run(nx, "nx_create_unsew", by="faces", faces=[side.ref])
    assert ok.ok, ok
    rows = ok.data["sheets"]
    assert len(rows) == ok.data["count"] and [r["selected"] for r in rows].count(True) == 1
    chosen = next(r for r in rows if r["selected"])
    assert chosen["faces"] == 1 and chosen["area"] == 100.0
