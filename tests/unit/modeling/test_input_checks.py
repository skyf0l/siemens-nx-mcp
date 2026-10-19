"""Modeling refusals and read-backs for implausible or mistaken input: offset emboss walls,
rib placement, sweep wall offsets, custom thread sizes, no-op face relations, a face NX
rebuilt, loft guides and start corners, unsew and untrim results, far-off distances."""

import math

from nx_mcp.domains.modeling import threads
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body
from tests.unit.modeling.test_emboss import raises, sheet_nx
from tests.unit.modeling.test_feature_shorthands import evaluable, seg, square
from tests.unit.modeling.test_holes_threads import end_faces, table, thread_nx  # noqa: F401
from tests.unit.modeling.test_loft_sections import _nx as loft_nx
from tests.unit.modeling.test_repair import _areas, _sheet
from tests.unit.modeling.test_shapes import rib_nx
from tests.unit.modeling.test_sync_relate import extra, relate
from tests.unit.modeling.test_sync_relate import setup as relate_setup

# ---- offset emboss ------------------------------------------------------------------


def test_offset_emboss_side_offset_past_half_the_footprint_is_refused():
    nx, sheet, path = sheet_nx()
    bad = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[path.ref], height=3,
              left_width=5, side_offset=6)  # fmt: skip
    assert bad.error["field"] == "/side_offset" and bad.mutation_outcome == "not_started", bad
    assert "below 5" in bad.error["hint"] and bad.error["details"]["top_width"] == -2
    assert not nx.builders
    raises(nx, sheet)
    ok = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[path.ref], height=3,
             left_width=5, right_width=8, side_offset=6)  # fmt: skip
    assert ok.ok, ok


def test_a_path_on_an_embossed_sheet_names_the_emboss():
    from tests.unit.modeling.test_features import _lines

    nx, sheet, _path = sheet_nx()
    done = feature(nx, "OFFSET_EMBOSS", [sheet])
    sheet.GetFeatures = lambda: [feature(nx, "EXTRUDE", [sheet]), done]
    (line,) = _lines(nx, [((10, 20, 3), (50, 20, 3))])
    line.OwningPart, line.IsOccurrence = nx.part, False
    nx.uf.Modeling.AskMinimumDist = lambda a, b, c, p, d, q: (3.0, list(q), list(q))
    bad = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[nx.ref_of(line, "curve")],
              height=2, left_width=2)  # fmt: skip
    assert bad.error["field"] == "/path/0" and "already carries an emboss" in bad.error["message"]
    assert bad.error["details"]["emboss"] == nx.ref_of(done, "feature")
    assert "undo the earlier emboss" in bad.error["hint"] and not nx.builders


# ---- rib --------------------------------------------------------------------------


def test_rib_curves_lying_on_material_say_there_is_nothing_to_cap():
    nx = rib_nx()
    plate = nx.block(40, 20, 3)
    sk = nx.sketch(origin=(0, 0, 3))
    sk.geometry = evaluable(nx, [seg((5, 10, 3), (35, 10, 3))])
    plate.contains = lambda p: 1 if p[2] < 3 else 2  # material below the plate's top face
    nx.refuse("RibBuilder", code=4095004, message="cannot cap")
    bad = run(nx, "nx_create_rib", sketch=sk.ref, body=plate.ref, thickness=2)
    assert bad.error["details"]["cause"] == "on_material", bad.error
    assert "flat plate" in bad.error["hint"] and "flip" not in bad.error["hint"].split(".")[0]
    above = nx.sketch(origin=(0, 0, 13))
    bad = run(nx, "nx_create_rib", sketch=above.ref, body=plate.ref, thickness=2)
    assert bad.error["details"]["cause"] == "plane_off_body" and "lies 10" in bad.error["message"]


# ---- sweep along guide --------------------------------------------------------------


def _square_section(nx, side=10.0):
    """A sketch in the YZ plane holding a side x side square from the origin, and a guide
    sketch with a line along x from ``start``."""
    sk = nx.sketch(origin=(0, 0, 0), x_axis=(0, 1, 0), y_axis=(0, 0, 1))
    corners = [(0, 0, 0), (0, side, 0), (0, side, side), (0, 0, side)]
    sk.geometry = evaluable(nx, [seg(a, b) for a, b in zip(corners, corners[1:] + corners[:1],
                                                            strict=True)])  # fmt: skip
    return sk


def _guide(nx, start=(0, 0, 0), length=100.0):
    guide = nx.sketch()
    (c,) = evaluable(nx, [seg(start, (start[0] + length, start[1], start[2]))])
    c.GetLength = lambda: length
    guide.geometry = [c]
    return guide


def test_a_wall_offset_that_turns_the_section_inside_out_is_refused():
    nx = modeling_nx()
    sk, guide = _square_section(nx), _guide(nx)
    bad = run(nx, "nx_create_sweep_along_guide", section=sk.ref, guide=guide.ref,
              first_offset=20, second_offset=-20)  # fmt: skip
    assert bad.error["field"] == "/second_offset" and bad.mutation_outcome == "not_started", bad
    assert bad.error["details"]["narrowest"] == 10 and "under 5" in bad.error["message"]
    nx.on_commit(
        "SweepAlongGuideBuilder",
        lambda b: feature(nx, "SWEEP", [new_body(nx, [0, -1, -1, 100, 11, 11], 4000)]),
    )
    ok = run(nx, "nx_create_sweep_along_guide", section=sk.ref, guide=guide.ref,
             first_offset=1, second_offset=-4)  # fmt: skip
    assert ok.ok, ok


def test_a_section_far_from_its_guide_is_refused_before_nx():
    """NX may commit a body far off the request for such a section (3000 for 18000)."""
    nx = modeling_nx()
    sk, guide = _square_section(nx), _guide(nx, start=(0, 100, 0))

    def gap(_tag, _a, _b, _zero, _c, q):  # to the guide line y=100, z=0, x 0..100
        x = min(100.0, max(0.0, q[0]))
        return (math.dist(q, (x, 100.0, 0.0)), [x, 100.0, 0.0])

    nx.uf.Modeling.AskMinimumDist = gap
    bad = run(nx, "nx_create_sweep_along_guide", section=sk.ref, guide=guide.ref)
    assert bad.error["field"] == "/section" and bad.mutation_outcome == "not_started", bad
    assert bad.error["details"]["section_gap"] > 80 and not nx.builders


# ---- thread ----------------------------------------------------------------------------


def test_a_custom_external_thread_larger_than_its_shaft_is_refused(table):  # noqa: F811
    nx, _body, cyl, start = thread_nx()
    cyl.radius = 6.0  # a 12 shaft
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"pitch": 1.75, "major_diameter": 20, "minor_diameter": 10.1})  # fmt: skip
    assert bad.error["field"] == "/spec/major_diameter", bad
    assert bad.mutation_outcome == "not_started" and "major_diameter 12" in bad.error["hint"]
    cyl.radius = 3.0
    ok = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"),
             start_face=nx.ref_of(start, "face"), length=8, hand="left",
             spec={"pitch": 1, "major_diameter": 6, "minor_diameter": 4.917, "starts": 2})  # fmt: skip
    assert ok.ok and ok.data["hand"] == "left" and ok.data["starts"] == 2, ok


def test_a_custom_internal_thread_far_deeper_than_its_bore_is_refused(table):  # noqa: F811
    nx, _body, cyl, start = thread_nx()
    ask = nx.uf.Modeling.AskFaceData
    nx.uf.Modeling.AskFaceData = lambda tag: (
        (*list(ask(tag))[:6], -1) if tag == cyl.Tag else ask(tag)
    )
    cyl.radius = 4.25  # an 8.5 bore
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"pitch": 1.75, "major_diameter": 20, "minor_diameter": 17.8})  # fmt: skip
    assert bad.error["field"] == "/spec/major_diameter" and "8.5 bore" in bad.error["message"]


def test_unknown_metric_sizes_suggest_their_numeric_neighbours():
    rows = [
        {"Standard": "Metric Coarse", "Size": s, "MajorDiameter": str(d), "Unit": "Metric"}
        for s, d in (("M7 x 1.0", 7), ("M8 x 1.25", 8), ("M27 x 3.0", 27), ("M170 x 3.0", 170))
    ]
    try:
        threads.resolve_size(rows, "Metric Coarse", "M7.3")
    except Exception as error:  # NXError
        assert error.details["close_matches"] == ["M7 x 1.0", "M8 x 1.25"]
    else:
        raise AssertionError("M7.3 resolved")


# ---- face relations -------------------------------------------------------------------


def test_a_relation_already_met_is_refused_before_nx():
    nx, a, b = relate_setup()
    top = a.face((0, 0, 1))
    other = b.face((0, 0, 1))
    nx.face_data[other.Tag] = ("plane", (110, 10, 10), (0, 0, 1))  # flush with a's top
    bad = relate(nx, top, "coplanar", other)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and "already coplanar" in bad.error["message"]
    assert bad.mutation_outcome == "not_started" and bad.error["retry"] == "never"
    assert not [x for x in nx.builders if x.kind == "CoplanarBuilder"]


def test_a_face_nx_rebuilt_is_found_by_the_relation():
    """A plane made tangent to a boss of its own body comes back under a new tag."""
    nx, a, _b = relate_setup()
    side = a.face((0, -1, 0))
    boss = extra(nx, a, "cylinder", (20, 20, 10), (0, 0, 1), 10.0)

    def hook(_builder):
        del nx.face_data[side.Tag]  # NX rebuilt the face
        extra(nx, a, "plane", (20, 10, 5), (0, -1, 0))
        return feature(nx, "TangentBuilder", [a])

    nx.on_commit("TangentBuilder", hook)
    ok = relate(nx, side, "tangent", boss)
    assert ok.ok and ok.data["gap"] == 0.0 and ok.data["angle"] == 90.0, ok
    assert ok.data["face"]["id"] != nx.ref_of(side, "face")


def test_a_rebuilt_face_that_misses_the_relation_still_fails():
    nx, a, _b = relate_setup()
    side = a.face((0, -1, 0))
    boss = extra(nx, a, "cylinder", (20, 20, 10), (0, 0, 1), 10.0)

    def hook(_builder):
        del nx.face_data[side.Tag]
        return feature(nx, "TangentBuilder", [a])

    nx.on_commit("TangentBuilder", hook)
    bad = relate(nx, side, "tangent", boss)
    assert bad.error["details"]["check"] == "face" and bad.mutation_outcome == "rolled_back"


# ---- loft ----------------------------------------------------------------------------


def test_a_guide_that_misses_a_section_is_refused_before_nx():
    nx = loft_nx()
    nx.on_commit("SweptBuilder", lambda b: feature(nx, "SWEPT", [new_body(nx, [0] * 6, 1.0)]))
    bottom = evaluable(nx, square(10))
    top = evaluable(nx, square(10, z=30))
    (guide,) = evaluable(nx, [seg((10, 10, 0), (10, 10, 20))])  # stops short of the top

    def gap(a, b, *_rest):
        far = int(a) == int(guide.Tag) and int(b) in {int(c.Tag) for c in top}
        return (10.0 if far else 0.0, [0, 0, 0], [0, 0, 0])

    nx.uf.Modeling.AskMinimumDist = gap
    bad = run(nx, "nx_create_loft", sections=[[c.ref for c in bottom], [c.ref for c in top]],
              guides=[guide.ref])  # fmt: skip
    assert bad.error["field"] == "/guides/0" and bad.error["details"]["section"] == 1, bad
    assert bad.mutation_outcome == "not_started" and "every section" in bad.error["hint"]


def test_start_corners_half_a_turn_apart_are_named_when_nx_fails():
    nx = loft_nx()
    nx.refuse("ThroughCurvesBuilder", message="Cannot create B-surface by through curves.")
    bottom = evaluable(nx, square(10))
    top = evaluable(nx, square(10, z=30))
    bad = run(nx, "nx_create_loft", sections=[[c.ref for c in bottom], [c.ref for c in top]],
              start_points=[[10, 10, 0], [-10, -10, 30]])  # fmt: skip
    assert bad.error["field"] == "/start_points/1", bad
    assert bad.error["details"]["twist_degrees"] == 180 and "twists" in bad.error["hint"]


def test_a_loft_onto_an_identical_body_warns_of_the_duplicate():
    nx = loft_nx()
    new_body(nx, [-10, -10, 0, 10, 10, 30], 10750.0)  # an earlier identical loft
    bottom = evaluable(nx, square(10))
    top = evaluable(nx, square(10, z=30))
    ok = run(nx, "nx_create_loft", sections=[[c.ref for c in bottom], [c.ref for c in top]])
    assert ok.ok, ok
    assert any("coincides with" in w["message"] for w in ok.warnings), ok.warnings


# ---- unsew and untrim ---------------------------------------------------------------


def test_unsew_keeping_the_original_marks_the_sheet_with_the_faces_given():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top = block.face((0, 0, 1))
    top.area = 1200.0

    def hook(_b):  # NX unsews a copy: the sheets hold new faces
        copy = nx.remember(nx.face(normal=(0, 0, 1), origin=nx.face_data[top.Tag][1], body=None))
        copy.area = 1200.0
        rest = nx.remember(nx.face(normal=(1, 0, 0), origin=(40, 15, 10), body=None))
        rest.area = 4000.0
        sheets = []
        for face in (copy, rest):
            sheet = _sheet(nx, [0, 0, 0, 1, 1, 1])
            sheet.faces = [face]
            sheets.append(sheet)
        return feature(nx, "UNSEW", sheets)

    nx.on_commit("UnsewBuilder", hook)
    block.faces = [top, block.face((1, 0, 0))]
    block.faces[1].area = 4000.0
    ok = run(nx, "nx_create_unsew", faces=[top.ref], keep_original=True)
    assert ok.ok, ok
    assert [s["selected"] for s in ok.data["sheets"]] == [True, False]


def test_untrim_hide_original_hides_the_source_body():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top = block.face((0, 0, 1))
    top.area = 1200.0

    def hook(_b):
        sheet = _sheet(nx, [-20, -15, 20, 60, 45, 20])
        sheet.faces[0].area = 4800.0
        return feature(nx, "UNTRIM", [sheet])

    nx.on_commit("UntrimBuilder", hook)
    ok = run(nx, "nx_create_untrim", faces=[top.ref], hide_original=True)
    assert ok.ok, ok
    assert block.IsBlanked and [r["id"] for r in ok.data["hidden"]] == [block.ref]


# ---- far-off values --------------------------------------------------------------


def test_an_edge_moved_far_beyond_its_body_is_refused():
    from tests.unit.modeling.test_sync_edges import edit
    from tests.unit.modeling.test_sync_edges import setup as edges_setup

    nx, _t, edge, _top, _back = edges_setup()
    bad = edit(nx, edge, action="move", distance=5000, direction="z")
    assert bad.error["field"] == "/distance" and bad.mutation_outcome == "not_started", bad
    assert bad.error["details"]["times"] == 100


def test_a_pull_along_the_face_plane_is_refused():
    nx = modeling_nx()
    block = nx.block(40, 30, 20)
    side = block.face((1, 0, 0))
    bad = run(nx, "nx_edit_face_region", faces=[side.ref], action="pull", distance=10,
              direction="z")  # fmt: skip
    assert bad.error["field"] == "/direction" and bad.mutation_outcome == "not_started", bad


def test_a_datum_point_outside_the_modeling_box_is_refused():
    nx = modeling_nx()
    bad = run(nx, "nx_create_datum_point", position=[1e30, 0, 0])
    assert bad.error["field"] == "/position" and bad.mutation_outcome == "not_started", bad
    assert not nx.builders


# ---- smaller read-backs and refusals ----------------------------------------------------


def test_rib_flipped_into_a_plate_names_the_material_too():
    nx = rib_nx()
    plate = nx.block(40, 20, 3)
    sk = nx.sketch(origin=(0, 0, 3))
    sk.geometry = evaluable(nx, [seg((5, 10, 3), (35, 10, 3))])
    plate.contains = lambda p: 1 if p[2] < 3 else 2
    nx.refuse("RibBuilder", code=4095004, message="cannot cap")
    bad = run(nx, "nx_create_rib", sketch=sk.ref, body=plate.ref, thickness=2, flip=True)
    assert bad.error["details"]["cause"] == "on_material", bad.error


def test_draft_returns_each_wall_angle():
    from tests.unit.modeling.test_action_readbacks import _draft_block

    nx, block, side, bottom = _draft_block()
    c, s = 0.9961946980917455, 0.08715574274765817  # 5 degrees

    def hook(_b):
        nx.face_data[side.Tag] = ("plane", (40, 10, 5), (c, 0.0, s))
        block.volume -= 100.0
        return feature(nx, "DRAFT", [block])

    nx.on_commit("DraftBuilder", hook)
    ok = run(nx, "nx_create_draft", faces=[side.ref], stationary_face=bottom.ref, direction="z",
             angle=5)  # fmt: skip
    assert ok.ok and abs(ok.data["angles"][0] - 5) < 1e-6, ok


def test_a_shell_on_a_shelled_body_names_the_shell():
    nx = modeling_nx()
    block = nx.block(60, 40, 30)
    shell = feature(nx, "SHELL", [block])
    block.GetFeatures = lambda: [feature(nx, "BLOCK", [block]), shell]
    nx.refuse("ShellBuilder", message="Cannot shell an existing Shell.")
    bad = run(nx, "nx_create_shell", body=block.ref, thickness=20)
    assert bad.error["field"] == "/body" and "shelled already" in bad.error["hint"], bad
    assert bad.error["details"]["shell"] == nx.ref_of(shell, "feature")


def test_emboss_body_refuses_a_sheet_thickness_on_a_solid():
    nx = modeling_nx()
    target, tool = nx.block(40, 20, 10), nx.block(5, 5, 5)
    bad = run(nx, "nx_create_emboss_body", target=target.ref, tool_body=tool.ref, thickness=2)
    assert bad.error["field"] == "/thickness" and bad.mutation_outcome == "not_started", bad


def test_face_copy_turned_about_its_own_axis_is_refused():
    from tests.unit.modeling.test_sync_faces import copy
    from tests.unit.modeling.test_sync_faces import setup as faces_setup

    nx, _a, _t, side, cap = faces_setup()
    turn = {"rotation": {"axis": "z", "angle": 90, "origin": [15, 20, 0]}}
    bad = copy(nx, [side, cap], mode="copy", transform=turn, paste=False)
    assert bad.error["field"] == "/transform" and bad.mutation_outcome == "not_started", bad
    assert "onto themselves" in bad.error["message"] and not nx.builders


def test_text_running_past_its_target_warns(monkeypatch):
    from nx_mcp.domains.modeling import text as mtext
    from tests.unit.modeling.test_text import _texts

    monkeypatch.setattr(mtext.measure, "length", lambda tx, curves: 40.0)
    nx = modeling_nx()
    _texts(nx, n=8)  # curves spanning x 0..8
    block = nx.block(5, 10, 10)
    top = block.face((0, 0, 1))

    def cut(_b):
        block.volume -= 10.0
        return feature(nx, "EXTRUDE", [block])

    nx.on_commit("ExtrudeBuilder", cut)
    ok = run(nx, "nx_create_text", text="ABCDEFGH", plane=top.ref, origin=[0, 2, 10], height=5,
             relief={"kind": "engrave", "depth": 1, "target": block.ref})  # fmt: skip
    assert ok.ok, ok
    assert any("past the target body" in w["message"] for w in ok.warnings), ok.warnings


def test_material_suggestions_need_a_close_spelling():
    from nx_mcp.domains.modeling.material import library_match

    names = ["Steel", "Titanium_Alloy", "Aluminum_6061"]
    assert library_match(names, "Unobtainium") == (None, [])
    assert library_match(names, "stell")[1][0] == "Steel"


def test_points_are_deleted_with_the_feature_that_owns_them():
    from types import SimpleNamespace as NS

    from nx_mcp.core.catalogue import hooks

    owned, loose = NS(Tag=7001), NS(Tag=7002)
    point_feature = NS(Tag=7000, FeatureType="POINT", GetEntities=lambda: [owned])
    other = NS(Tag=7003, FeatureType="EXTRUDE", GetEntities=lambda: [])
    deleted, updates = [], []
    tx = NS(
        mark=1,
        part=NS(Features=[other, point_feature]),
        session=NS(
            ActiveSketch=None,
            UpdateManager=NS(
                AddToDeleteList=deleted.extend, DoUpdate=lambda m: updates.append(m) or 0
            ),
        ),
    )
    hooks("deleter")["point"](tx, [owned, loose])
    assert deleted == [point_feature, loose] and updates == [1]
