"""Model ownership of measured refs, per-body mass rows, wall thickness from faces, round
faces by radius, hole steps and sources, face summaries, curve normals, continuity of
curves that do not meet, body-compare reasons, off-axis radial profiles, mass-report
inertia lines and group notes, inspection-report sections and values-only analyses."""

import csv
import io
import json
import math
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.inspection import interference
from nx_mcp.kit import capture
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.inspection.fakes import assembly, curve, inspection_nx


def _foreign(nx, origin=(0, 0, 0)):
    """A body of another loaded part (not a component of the work part)."""
    body = nx.block(10, 10, 10, origin=origin)
    body.OwningPart = NS(Tag=987654, Leaf="plate", FullPath="plate.prt")
    return body


# ---- ownership (bodies of another part give no measurement) ------------------------------


def test_interference_refuses_a_body_of_another_part():
    nx = inspection_nx()
    own = nx.block(10, 10, 10)
    other = _foreign(nx, origin=(5, 0, 0))
    bad = run(nx, "nx_check_interference", objects=[own.ref, other.ref])
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.error["field"] == "/objects/1", bad
    assert "plate" in bad.error["message"] and "part=" in bad.error["hint"]
    report = run(nx, "nx_export_inspection_report", path=str(services(nx).root / "r.zip"),
                 objects=[own.ref, other.ref], capture=False)  # fmt: skip
    assert report.error["code"] == "NX_WRONG_OWNER" and report.mutation_outcome == "not_started"


def test_component_bodies_belong_to_the_assembly():
    nx = inspection_nx()
    a, b = assembly(nx, ("A", [0, 0, 0, 10, 10, 10]), ("B", [5, 0, 0, 15, 10, 10]))
    ok = run(nx, "nx_check_interference",
             objects=[nx.ref_of(a, "component"), nx.ref_of(b, "component")])  # fmt: skip
    assert ok.ok and ok.data["counts"]["penetration"] == 1, ok


def test_mass_refuses_another_parts_body_and_names_list_items():
    nx = inspection_nx()
    own = nx.block(10, 10, 10)
    other = _foreign(nx)
    bad = run(nx, "nx_measure_mass", bodies=[own.ref, other.ref])
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.error["field"] == "/bodies/1"


# ---- mass rows, notes and scope -----------------------------------------------------------


def test_mass_rows_give_each_bodys_mass_and_note_default_density():
    nx = inspection_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 20, 10, origin=(50, 0, 0))
    ok = run(nx, "nx_measure_mass", scope="part")
    assert ok.ok, ok
    rows = ok.data["bodies"]
    assert [r["mass"] for r in rows] == pytest.approx([1000 * nx.density, 2000 * nx.density])
    assert rows[0]["density"] == pytest.approx(7850) and rows[0]["material"] is None
    (note,) = ok.warnings
    assert "2 of 2 bodies have no material" in note["message"] and a.ref in note["message"]
    assert "nx_set_material" in note["message"] and b.ref in note["message"]


def test_mass_inertia_eigenvalues_are_its_principal_moments():
    """Two offset blocks: every product of inertia is non-zero."""
    from nx_mcp.kit import massprops

    nx = inspection_nx()
    nx.block(100, 50, 20)
    nx.block(10, 10, 10, origin=(200, 30, 0))
    data = run(nx, "nx_measure_mass", scope="part").data
    tensor = data["inertia"]
    assert all(abs(tensor[i][j]) > 0 for i, j in ((0, 1), (0, 2), (1, 2)))
    assert massprops.principal_moments(tensor) == pytest.approx(
        sorted(data["principal_moments"]), rel=1e-9
    )


def test_scope_assembly_without_components_says_so():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    ok = run(nx, "nx_measure_mass", scope="assembly")
    assert ok.ok and any("no components" in w["message"] for w in ok.warnings), ok.warnings


# ---- wall thickness ----------------------------------------------------------------------


def test_wall_thickness_takes_the_body_of_its_faces_and_lists_thinnest_first():
    nx = inspection_nx()
    plate = nx.block(100, 60, 10)
    nx.block(5, 5, 5, origin=(200, 0, 0))
    side = next(f for f in plate.faces if list(f.normal) == [1, 0, 0])
    top = next(f for f in plate.faces if list(f.normal) == [0, 0, 1])
    ok = run(nx, "nx_measure_wall_thickness", faces=[side.ref, top.ref], samples_per_axis=1)
    assert ok.ok and ok.data["body"]["id"] == plate.ref, ok
    assert [s["thickness"] for s in ok.data["samples"]] == pytest.approx([10, 100])
    assert ok.data["thinnest"]["thickness"] == pytest.approx(10)
    assert ok.data["by_face"][0]["min_point"] == ok.data["thinnest"]["point"]
    two = run(nx, "nx_measure_wall_thickness", samples_per_axis=1)
    assert two.error["field"] == "/body" and "work.prt has 2 solid bodies" in two.error["message"]


# ---- find_geometry radius on round faces --------------------------------------------------


def test_radius_matches_spheres_and_cylinders_and_edges_have_length():
    nx = inspection_nx()
    body = nx.block(20, 20, 20)
    ball = nx.remember(nx.face(normal=(0, 0, 1), origin=(10, 10, 10), kind="sphere", body=body))
    ball.radius, ball.sense = 10.0, 1
    body.faces.append(ball)
    ok = run(nx, "nx_find_geometry", radius="10 mm")
    assert ok.ok and [i["id"] for i in ok.data["items"]] == [ball.ref], ok
    assert ok.data["items"][0]["type"] == "sphere" and ok.data["items"][0]["radius"] == 10
    typed = run(nx, "nx_find_geometry", geometry_type="sphere")
    assert [i["id"] for i in typed.data["items"]] == [ball.ref]
    cylinder = run(nx, "nx_find_geometry", geometry_type="cylinder", radius=10)
    assert cylinder.data["items"] == []


# ---- holes: steps, source feature, thread, diameter note ----------------------------------


def test_holes_give_steps_feature_thread_and_the_nearest_diameters():
    from tests.kit.objects import Feature
    from tests.unit.inspection.test_holes import _plate

    nx, body = _plate()
    bores = [f for f in body.faces if getattr(f, "radius", None) and f.sense == -1]
    counter = [f for f in bores if f.radius in (5.0, 2.5)]
    drill = nx._own(Feature("COUNTERBORED HOLE(4)"), "feature")
    drill.FeatureType = "HOLE PACKAGE"
    drill.GetFaces = lambda: counter
    drill.GetFeatureName = lambda: "Counterbored Hole(4)"
    tapped = next(f for f in bores if f.radius == 3.0)
    thread = nx._own(Feature("SYMBOLIC THREAD(5)"), "feature")
    thread.FeatureType = "SYMBOLIC_THREAD"
    thread.GetFaces = lambda: [tapped]
    thread.GetFeatureName = lambda: "Symbolic Thread(5)"
    body.GetFeatures = lambda: [drill, thread]
    for face in body.faces:
        face.GetBody = lambda: body
    ok = run(nx, "nx_find_holes")
    assert ok.ok, ok
    groups = {round(g["axis_point"][0]): g for g in ok.data["groups"]}
    cbore = groups[50]
    assert cbore["radii"] == [5.0, 2.5] and cbore["diameters"] == [10.0, 5.0]
    assert [s["diameter"] for s in cbore["steps"]] == [10.0, 5.0]
    assert [s["depth"] for s in cbore["steps"]] == pytest.approx([3.0, 7.0])
    assert cbore["feature"]["id"] == drill.ref and "thread" not in cbore
    assert groups[10]["thread"]["type"] == "SYMBOLIC_THREAD"
    none = run(nx, "nx_find_holes", diameter=6.79)
    assert none.data["groups"] == [] and "nearest: 6" in none.warnings[0]["message"]


# ---- face summaries and sample filters ----------------------------------------------------


def test_faces_carry_a_summary_and_samples_can_be_violations_only():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = next(f for f in body.faces if list(f.normal) == [0, 0, 1])
    side = next(f for f in body.faces if list(f.normal) == [1, 0, 0])
    side.radii = [-4.0, 3.937e8]
    ok = run(nx, "nx_analyze_faces", faces=[top.ref, side.ref], samples_per_axis=2,
             pull_direction="z", minimum_draft=3, samples="violations")  # fmt: skip
    assert ok.ok, ok
    top_row, side_row = ok.data["faces"]
    assert top_row["samples"] == [] and top_row["summary"]["below_minimum"] == 0
    assert top_row["summary"]["min_radius"] is None and top_row["summary"]["min_draft"] == 90
    assert len(side_row["samples"]) == 4 and side_row["summary"]["below_minimum"] == 4
    assert side_row["summary"]["min_radius"] == 4.0
    assert side_row["summary"]["max_curvature"] == pytest.approx(0.25)
    none = run(nx, "nx_analyze_faces", faces=[top.ref], samples="none")
    assert none.data["faces"][0]["samples"] == [] and none.data["faces"][0]["summary"]["samples"]
    bad = run(nx, "nx_analyze_faces", faces=[top.ref], samples="violations")
    assert bad.error["field"] == "/samples"


# ---- curves ------------------------------------------------------------------------------


def test_curve_analysis_gives_length_normals_and_takes_a_one_curve_feature():
    from tests.kit.objects import Feature

    nx = inspection_nx()
    circle = curve(nx, radius=5)
    circle.GetLength = lambda: 2 * math.pi * 5
    ok = run(nx, "nx_analyze_curve", curve=circle.ref, samples=5)
    assert ok.ok, ok
    assert ok.data["length"] == pytest.approx(2 * math.pi * 5, rel=1e-3)
    first = ok.data["samples"][0]
    assert first["normal"] == pytest.approx([-1, 0, 0]) and ok.data["inflections"] == []
    feature = nx._own(Feature("SPLINE(3)"), "feature")
    feature.GetEntities = lambda: [circle]
    via = run(nx, "nx_analyze_curve", curve=feature.ref, samples=3)
    assert via.ok and via.data["curve"]["id"] == circle.ref, via
    feature.GetEntities = lambda: [circle, curve(nx)]
    many = run(nx, "nx_analyze_curve", curve=feature.ref)
    assert many.error["code"] == "NX_WRONG_KIND" and "2 curves" in many.error["message"]


def test_inflections_are_where_the_normal_flips():
    from nx_mcp.domains.inspection import analysis

    rows = [{"t": 0.0, "normal": [0, 1, 0]}, {"t": 0.5, "normal": [0, 1, 0]},
            {"t": 1.0, "normal": [0, -1, 0]}]  # fmt: skip
    assert analysis._inflections(rows) == [0.75]


def test_continuity_of_curves_far_apart_leaves_g1_g2_open(monkeypatch):
    from nx_mcp.kit import surface

    nx = inspection_nx()
    a, b = nx.block(1, 1, 1), nx.block(1, 1, 1, origin=(5, 0, 0))
    e1, e2 = a.edges[0], b.edges[0]
    e1.GetFaces = lambda: [a.faces[0]]
    e2.GetFaces = lambda: [b.faces[0]]
    report = {"gap": 14.14, "angle": 90.0, "curvature": 0.0, "samples": 2, "unresolved": 0}
    monkeypatch.setattr(surface, "edge_continuity", lambda *a, **k: report)
    ok = run(nx, "nx_check_continuity", first=e1.ref, second=e2.ref)
    assert (ok.data["g0"], ok.data["g1"], ok.data["g2"]) == (False, None, None)
    assert "do not meet" in ok.warnings[0]["message"]


# ---- body compare ------------------------------------------------------------------------


def test_body_compare_says_why_faces_changed_and_notes_no_match(monkeypatch):
    from nx_mcp.domains.inspection import body_compare
    from tests.unit.inspection.test_body_compare import compare_nx

    a = {"type": 22, "point": [0, 0, 0], "dir": [0, 0, 1], "box": [0, 0, 0, 1, 1, 0],
         "radius": 0, "rad_data": 0, "edges": 4, "area": 1.0}  # fmt: skip
    b = {**a, "box": [0, 0, 0, 2, 1, 0], "edges": 5, "area": 2.0}
    assert body_compare.why(a, b, 1e-3) == ["bounds", "edges", "area"]
    nx = compare_nx(monkeypatch)
    first, second = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(100, 100, 100))
    ok = run(nx, "nx_analyze_body_changes", control=first.ref, sample=second.ref)
    assert ok.ok and ok.data["counts"]["matching"] == 0, ok
    assert any("No face of the two bodies matches" in w["message"] for w in ok.warnings)


# ---- radial profile off its axis ---------------------------------------------------------


def test_radial_profile_off_the_axis_notes_it_and_origin_moves_the_axis():
    from tests.unit.inspection.test_section import rect

    nx = inspection_nx()
    body = nx.block(20, 20, 50, origin=(190, -10, 0))
    nx.section_cuts[int(body.Tag)] = rect(190, 0, 210, 50)
    off = run(nx, "nx_measure_section", radial_axis="z", stations=2)
    assert off.ok and "not revolved about it" in off.warnings[0]["message"], off
    assert off.data["profile"][0]["spans"] == [[190, 210]]


# ---- mass report -------------------------------------------------------------------------


def test_mass_report_csv_has_inertia_lines_and_units():
    nx = inspection_nx()
    assembly(nx, ("HULL", [0, 0, 0, 10, 10, 10]), ("ENGINE", [50, 0, 0, 60, 10, 20]))
    target = services(nx).root / "w.csv"
    ok = run(nx, "nx_export_mass_report", path=str(target),
             report_args={"inertia": True, "reference": {"origin": [0, 0, 0]}})  # fmt: skip
    assert ok.ok, ok
    lines = list(csv.reader(io.StringIO(target.read_bytes().decode("utf-8-sig"))))
    units = next(line for line in lines if line and line[0] == "units")
    assert "volume mm^3" in units[1] and "inertia kg*mm^2" in units[1]
    rows = {(line[0], line[1]): line for line in lines if line and line[0] == "inertia"}
    xx = float(rows[("inertia", "cog")][2])
    assert xx == pytest.approx(ok.data["total"]["inertia"][0][0])
    assert ("inertia", "reference") in rows
    assert any(line and line[0] == "principal_moments" for line in lines)


def test_mass_report_args_errors_carry_the_report_args_prefix():
    nx = inspection_nx()
    assembly(nx, ("HULL", [0, 0, 0, 10, 10, 10]))
    bad = run(nx, "nx_export_mass_report", path=str(services(nx).root / "x.csv"),
              report_args={"group_by": "colour", "bogus": 1})  # fmt: skip
    fields = [e["field"] for e in (bad.error["details"] or {}).get("errors", [])]
    assert fields and all(f.startswith("/report_args/") for f in fields), bad.error


def test_groups_and_attributes_that_match_nothing_are_noted():
    nx = inspection_nx()
    hull, _engine = assembly(nx, ("HULL", [0, 0, 0, 10, 10, 10]),
                             ("ENGINE", [50, 0, 0, 60, 10, 20]))  # fmt: skip
    named = run(nx, "nx_measure_assembly_mass",
                groups={"hull": ["HULL"], "none": ["ZZZ*"]})  # fmt: skip
    assert named.ok and any("'none' matched no" in w["message"] for w in named.warnings)
    attr = run(nx, "nx_measure_assembly_mass", group_by="attribute", attribute="WEIGHT_GROUP")
    assert any("set on 0 of 2 components" in w["message"] for w in attr.warnings), attr


# ---- inspection report -------------------------------------------------------------------


def test_section_planes_beside_the_model_are_skipped_and_the_index_is_a_table(monkeypatch):
    nx = inspection_nx()
    assembly(nx, ("A", [0, 0, 0, 10, 10, 10]), ("C", [5, 5, 5, 15, 15, 15]))
    monkeypatch.setattr(capture, "studio_png", lambda tx, path, w, h, **_: (
        Path(path).write_bytes(b"\x89PNG"), (w, h))[1])  # fmt: skip
    root = services(nx).root
    ok = run(nx, "nx_export_inspection_report", path=str(root / "r.zip"),
             section_planes=[{"origin": [0, 0, 500], "normal": [0, 0, 1]}])  # fmt: skip
    assert ok.ok, ok
    assert any("section_planes/0 misses the model" in w["message"] for w in ok.warnings)
    with zipfile.ZipFile(root / "r.zip") as archive:
        page = archive.read("index.html").decode()
        report = json.loads(archive.read("report.json"))
    assert "<table>" in page and "penetration" in page and "<pre>" not in page
    assert "section-1.png" not in report["captures"]


def test_plane_misses_and_tilted_view():
    box = ([0, 0, 0], [10, 10, 10])
    assert interference.misses(box, [0, 0, 500], [0, 0, 1])
    assert not interference.misses(box, [5, 5, 5], [0, 0, 1])
    tilted = interference._tilted([0, 1, 0])
    assert tilted[1] == pytest.approx(math.cos(math.radians(30)))
    assert tilted[2] == pytest.approx(0.5)


# ---- values-only surface analysis --------------------------------------------------------


def test_slope_values_without_a_display_object():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = next(f for f in body.faces if list(f.normal) == [0, 0, 1])
    nx.session.Parts.Display = None  # the part is not displayed
    ok = run(nx, "nx_create_surface_analysis", type="slope", faces=[top.ref], display=False,
             samples_per_axis=1)  # fmt: skip
    assert ok.ok and ok.data["analyses"] == 0 and ok.data["values"]["min"] == 0, ok
    bad = run(nx, "nx_create_surface_analysis", type="reflection", faces=[top.ref],
              display=False)  # fmt: skip
    assert bad.error["field"] == "/display" and bad.mutation_outcome == "not_started"


def test_body_rows_say_whether_the_body_is_hidden():
    nx = inspection_nx()
    shown, hidden = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(20, 0, 0))
    hidden.IsBlanked = True
    rows = {r["id"]: r for r in run(nx, "nx_list_objects", kind="body").data["items"]}
    assert rows[shown.ref]["hidden"] is False and rows[hidden.ref]["hidden"] is True
    compact = run(nx, "nx_list_objects", kind="body", compact=True).data["items"]
    assert [r["hidden"] for r in compact] == [False, True]


def test_check_model_warns_about_deformed_values_out_of_range(monkeypatch):
    from nx_mcp.kit import deform

    nx = inspection_nx()
    nx.block(10, 10, 10)
    text = "Deformed o:7 (SPRING): length = 90 lies outside its range [40, 80]"
    monkeypatch.setattr(deform, "range_messages", lambda tx, part: [text])
    ok = run(nx, "nx_check_model")
    assert ok.ok and ok.data["healthy"] is False and ok.data["warnings"] == 1, ok
    (issue,) = ok.data["issues"]
    assert issue["kind"] == "deformation_range" and issue["message"] == text
