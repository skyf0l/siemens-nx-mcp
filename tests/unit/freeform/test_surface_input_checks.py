"""Input checks and result details of the freeform sheet tools on the shared fake: the
midsurface thinness warning, extensions that never reach their limit, law extension
checks and orientation, open N-sided loops, the ruled hint, variable offset point order
and expressions, thicken combine, partial and too-deep offsets, and bridge faces."""

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import curve, edge, face, flat, freeform_nx, made, sheet
from tests.unit.freeform.support import curves_nx
from tests.unit.freeform.test_extend import freeform_nx as extend_nx
from tests.unit.freeform.test_extend import law_commit, plate
from tests.unit.freeform.test_surfaces import bridge_nx, unlicensed
from tests.unit.freeform.test_variable_offset import surf_nx
from tests.unit.modeling.fakes import new_body


# ---- midsurface ---------------------------------------------------------------------------
def test_midsurface_of_a_thick_block_warns_that_it_is_not_thin():
    nx = curves_nx()
    nx.licenses["solid_modeling"] = True
    nx.part.ScRuleFactory.CreateRuleBodyDumb = lambda objs, *_: ("BodyDumb", list(objs))
    block = nx.block(40, 20, 30, origin=(0, 50, 0))

    def commit(_b):
        f = face(nx, flat(), area=800.0)
        f.at = lambda u, v: [40.0 * u, 50.0 + 20.0 * v, 15.0]
        return made(nx, "MIDSURFACE", [sheet(nx, (0, 50, 15, 40, 70, 15), [f])])

    nx.on_commit("MidSurfaceBuilder", commit)
    ok = run(nx, "nx_create_midsurface", body=nx.ref_of(block, "body"))
    assert ok.ok, ok
    half = ok.data["half_thickness"]
    assert half["min"] < half["max"] <= 15
    assert any("may not be thin" in w["message"] for w in ok.warnings)


# ---- sheet extension until a parallel plane -------------------------------------------------
def test_extension_until_a_plane_parallel_to_the_sheet_is_refused_before_nx():
    nx = extend_nx()
    _body, _f, e = plate(nx, z=13.0)
    level = face(nx, flat(), area=600.0, normal=(0, 0, 1))  # the plane z = 0, below the sheet
    bad = run(nx, "nx_create_sheet_extension", edges=[e.ref], method="until", until=[level.ref])
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/until/0"
    assert "parallel" in bad.error["message"] and bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_extension_that_never_reaches_its_limit_gets_a_hint():
    nx = extend_nx()
    _body, _f, e = plate(nx)
    wall = face(nx, flat(), area=600.0, normal=(-1, 0, 0))
    nx.refuse("ExtendSheetBuilder", 670033, "Unable to perform Boolean.")
    bad = run(nx, "nx_create_sheet_extension", edges=[e.ref], method="until", until=[wall.ref])
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["field"] == "/until"
    assert "across the sheet" in bad.error["hint"] and nx.live_builders == 0


# ---- law extension -------------------------------------------------------------------------
def test_law_extension_reports_its_base_ends_and_side():
    nx = extend_nx()
    nx.licenses["solid_modeling"] = True
    _body, _f, e = plate(nx)
    e.GetLength = lambda: 10.0
    law_commit(nx, 50.0, (20, 0, 0, 20, 10, 5))
    ok = run(nx, "nx_create_law_extension", base=[e.ref], length=5, angle=90)
    assert ok.ok, ok
    assert ok.data["base_start"] == [20, 0, 0] and ok.data["base_end"] == [20, 10, 0]
    assert ok.data["side"] == pytest.approx([0, 0, 1])


def test_law_extension_refuses_a_broken_base_and_a_direction_along_it():
    nx = extend_nx()
    nx.licenses["solid_modeling"] = True
    _body, f, e = plate(nx)
    far = edge(nx, [40, 0, 0], [40, 10, 0], faces=[f])
    apart = run(nx, "nx_create_law_extension", base=[e.ref, far.ref], length=5)
    assert apart.error["code"] == "NX_INVALID_ARGUMENT"
    assert apart.error["field"].startswith("/base/") and apart.mutation_outcome == "not_started"
    along = run(nx, "nx_create_law_extension", base=[e.ref], reference="vector",
                direction=[0, 1, 0], length=10)  # fmt: skip
    assert along.error["field"] == "/direction" and "across" in along.error["message"]
    assert not nx.builders


# ---- N-sided open loop --------------------------------------------------------------------
def test_n_sided_open_loop_reports_where_it_is_open():
    nx = freeform_nx()
    pts = [[0, 0, 0], [50, 0, 0], [50, 50, 0], [0, 50, 0]]
    sides = [curve(nx, pts[i], pts[i + 1]) for i in range(3)]  # 3 of 4 sides
    bad = run(nx, "nx_create_n_sided_surface", boundary=[c.ref for c in sides])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/boundary"
    details = bad.error["details"]
    assert details["gap"] == pytest.approx(50)
    assert sorted([details["open_at"], details["to"]]) == [[0, 0, 0], [0, 50, 0]]


# ---- ruled hint ----------------------------------------------------------------------------
def test_ruled_self_intersection_gets_the_ruled_hint():
    nx = freeform_nx()
    a = curve(nx, [0, 0, 0], [10, 0, 0])
    b = curve(nx, [0, 0, 10], [10, 0, 10])
    nx.refuse("RuledBuilder", 875316, "Self-intersecting geometry detected.")
    bad = run(nx, "nx_create_ruled", first=a.ref, second=b.ref, reverse_second=True)
    assert bad.error["code"] == "NX_BUILD_FAILED"
    assert "reverse_second" in bad.error["hint"] and "guide" not in bad.error["hint"]


# ---- variable offset -------------------------------------------------------------------------
def test_variable_offset_refuses_points_out_of_order_and_names_expressions():
    nx, f = surf_nx()
    bow = [[2, 2, 0], [18, 8, 0], [18, 2, 0], [2, 8, 0]]
    bad = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 1, 3, 3], points=bow)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/points/2"
    assert bad.mutation_outcome == "not_started" and not nx.builders
    make = nx.builder

    def named(kind, *args):
        b = make(kind, *args)
        if kind == "VariableOffsetBuilder":
            for i, k in enumerate("ABCD"):
                getattr(b, "OffsetAt" + k).Name = f"p{17 + i}"
        return b

    nx.builder = named
    ok = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 2, 3, 4])
    assert ok.ok, ok
    assert [p["expression"] for p in ok.data["points"]] == ["p17", "p18", "p19", "p20"]


# ---- thicken combine ---------------------------------------------------------------------------
def test_thicken_unites_with_a_target_and_checks_the_volume_change():
    nx = freeform_nx()
    top = face(nx, flat(), area=200.0)
    sheet(nx, (0, 0, 0, 20, 10, 0), [top])
    target = new_body(nx, [0, 0, -10, 20, 10, 0])  # volume 2000
    target_ref = nx.ref_of(target, "body")

    def commit(_b):
        target.volume += 400.0
        target.box = [0, 0, -10, 20, 10, 2]
        return made(nx, "THICKEN", [target])

    nx.on_commit("ThickenBuilder", commit)
    ok = run(nx, "nx_create_thicken", faces=[top.ref], thickness=2,
             combine={"operation": "unite", "targets": [target_ref]})  # fmt: skip
    assert ok.ok, ok
    b = nx.last("ThickenBuilder")
    kinds = nx.nx.GeometricUtilities.BooleanOperation.BooleanType
    assert b.BooleanOperation.Type is kinds.Unite
    assert b.BooleanOperation.SetTargetBodies.calls[0][0][0] == [target]
    assert ok.data["volume"] == pytest.approx(2400) and ok.data["volume_before"] == 2000

    nx.on_commit("ThickenBuilder", lambda _b: made(nx, "THICKEN", [target]))
    same = run(nx, "nx_create_thicken", faces=[top.ref], thickness=2,
               combine={"operation": "subtract", "targets": [target_ref]})  # fmt: skip
    assert same.error["details"]["check"] == "combine" and same.mutation_outcome == "rolled_back"
    two = run(nx, "nx_create_thicken", faces=[top.ref], thickness=2,
              combine={"operation": "unite", "targets": [target_ref, top.ref]})  # fmt: skip
    assert two.error["field"] == "/combine/targets/1" and two.mutation_outcome == "not_started"


# ---- offset surface: partial and too deep ------------------------------------------------------
def _plate(nx, z):
    f = face(nx, flat(), area=200.0)
    f.at = lambda u, v, z=z: [20.0 * u, 10.0 * v, z]
    sheet(nx, (0, 0, z, 20, 10, z), [f])
    return f


def test_partial_offset_leaves_out_the_faces_nx_skipped():
    nx = freeform_nx()
    low, high = _plate(nx, 0.0), _plate(nx, 50.0)

    def commit(_b):  # NX offset only the low face
        return made(nx, "OFFSET_SURFACE", [sheet(nx, (0, 0, -5, 20, 10, -5), [face(nx, flat())])])

    nx.on_commit("OffsetSurfaceBuilder", commit)
    ok = run(nx, "nx_create_offset_surface", faces=[low.ref, high.ref], distance=-5, partial=True)
    assert ok.ok, ok
    assert nx.last("OffsetSurfaceBuilder").PartialOption is True
    assert [r["id"] for r in ok.data["skipped"]] == [high.ref]
    assert ok.data["sets"][0]["faces"] == 1
    assert any("left out" in w["message"] for w in ok.warnings)


def test_offset_deeper_than_a_cylinder_radius_is_refused_before_nx():
    nx = freeform_nx()
    tube = face(nx, flat(), area=1256.6, normal=(1, 0, 0), kind="cylinder")
    nx.face_data[tube.Tag] = ("cylinder", [0, 0, 0], [0, 0, 1])  # axis z through the origin
    tube.at = lambda u, v: [10.0, 0.0, 20.0 * v]  # sampled on the side x = 10 (radius 10)
    sheet(nx, (-10, -10, 0, 10, 10, 20), [tube])
    bad = run(nx, "nx_create_offset_surface", faces=[tube.ref], distance=-15)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/distance"
    assert bad.error["details"]["radius"] == pytest.approx(10)
    assert bad.mutation_outcome == "not_started" and not nx.builders


# ---- bridge surface faces ------------------------------------------------------------------------
def test_bridge_from_a_solid_edge_follows_the_chosen_face():
    nx, a, b, fa, fb = bridge_nx(solid_edge=True)
    ok = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g1",
             first_face=fa.ref)  # fmt: skip
    assert ok.ok and ok.data["construction"] == "through_curves", ok
    tc = nx.last("ThroughCurvesBuilder")
    assert tc.FirstSectionContinuity.ConstraintFaces.ReplaceRules.calls[0][0][0] == [
        ("FaceDumb", [fa])
    ]
    assert not [x for x in nx.builders if x.kind == "BridgeSurfaceBuilder"]
    nx.builders.clear()
    g0 = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g0",
             first_face=fa.ref)  # fmt: skip
    assert g0.error["field"] == "/first_face"
    other = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g1",
                first_face=fb.ref)  # fmt: skip
    assert other.error["field"] == "/first_face" and not nx.builders


def test_unlicensed_bridge_still_honours_a_chosen_face():
    nx, a, b, fa, _fb = bridge_nx(solid_edge=True)
    unlicensed(nx)
    ok = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g1",
             first_face=fa.ref)  # fmt: skip
    assert ok.ok and ok.data["construction"] == "through_curves", ok
