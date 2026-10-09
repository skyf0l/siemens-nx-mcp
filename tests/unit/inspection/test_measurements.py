"""nx_measure_distance/angle/mass/bounds/wall_thickness on the fake kit."""

import pytest

from tests.kit import run
from tests.kit.objects import matrix, point
from tests.unit.inspection.fakes import assembly, curve, inspection_nx


def test_distance_between_bodies_and_feature_bodies():
    nx = inspection_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(25, 0, 0))
    ok = run(nx, "nx_measure_distance", first=a.ref, second=b.ref)
    assert ok.ok and ok.data["distance"] == 15 and ok.data["pairs"] == 1, ok
    assert ok.data["points"][0][0] == 10 and ok.data["points"][1][0] == 25
    assert ok.data["first"]["id"] == a.ref and ok.units["length"] == "mm"


def test_distance_of_components_takes_the_closest_occurrence_pair():
    nx = inspection_nx()
    c1, c2 = assembly(nx, ("A", [0, 0, 0, 10, 10, 10]), ("B", [13, 0, 0, 20, 10, 10]))
    ok = run(nx, "nx_measure_distance", first=nx.ref_of(c1, "component"),
             second=nx.ref_of(c2, "component"))  # fmt: skip
    assert ok.data["distance"] == 3 and ok.data["first"]["kind"] == "body"


def test_angle_between_planar_faces_edges_and_lines():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = next(f for f in body.faces if list(f.normal) == [0, 0, 1])
    side = next(f for f in body.faces if list(f.normal) == [1, 0, 0])
    ok = run(nx, "nx_measure_angle", first=top.ref, second=side.ref)
    assert ok.data["angle"] == pytest.approx(90) and ok.data["directions"][0] == [0, 0, 1]
    line = curve(nx)
    line.StartPoint, line.EndPoint = point(0, 0, 0), point(1, 1, 0)
    edge = next(e for e in body.edges if e.GetVertices()[1].X != e.GetVertices()[0].X)
    angled = run(nx, "nx_measure_angle", first=line.ref, second=edge.ref)
    assert angled.ok and angled.data["angle"] == pytest.approx(45)
    arc = curve(nx, radius=2)
    bad = run(nx, "nx_measure_angle", first=arc.ref, second=top.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/first"


@pytest.mark.parametrize("units", ["mm", "in"])
def test_mass_volume_inertia_in_part_units(units):
    nx = inspection_nx(units)
    body = nx.block(10, 20, 30)
    ok = run(nx, "nx_measure_mass", bodies=[body.ref])
    assert ok.ok, ok
    data = ok.data
    assert data["volume"] == pytest.approx(6000) and data["bodies"][0]["volume"] == pytest.approx(
        6000
    )
    assert data["mass"] == pytest.approx(6000 * nx.density)
    assert data["center_of_gravity"] == pytest.approx([5, 10, 15])
    m = data["mass"]
    assert data["inertia"][0][0] == pytest.approx(m * (20**2 + 30**2) / 12)
    assert data["principal_moments"][0] == pytest.approx(m * (10**2 + 20**2) / 12)
    assert ok.units["length"] == units


def test_mass_rotates_inertia_from_a_rotated_wcs():
    nx = inspection_nx()
    nx.block(10, 20, 30)
    nx.part.WCS.CoordinateSystem.Orientation.Element = matrix((0, 1, 0), (-1, 0, 0), (0, 0, 1))
    ok = run(nx, "nx_measure_mass", scope="part")
    i_wcs = ok.data["inertia"]
    m = ok.data["mass"]
    # WCS x is absolute y: the absolute yy moment is the WCS xx moment.
    assert i_wcs[1][1] == pytest.approx(m * (20**2 + 30**2) / 12)


def test_mass_rejects_sheets_and_empty_parts():
    nx = inspection_nx()
    empty = run(nx, "nx_measure_mass")
    assert empty.error["code"] == "NX_NOT_FOUND"
    sheet = nx.block(1, 1, 1)
    sheet.IsSolidBody = False
    bad = run(nx, "nx_measure_mass", bodies=[sheet.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY"


def test_bounds_conservative_exact_and_wcs_refusal():
    nx = inspection_nx()
    a = nx.block(10, 10, 10)
    nx.block(5, 5, 5, origin=(20, 0, -5))
    ok = run(nx, "nx_measure_bounds", precision="exact")
    assert ok.data["min"] == [0, 0, -5] and ok.data["max"] == [25, 10, 10]
    assert ok.data["size"] == [25, 10, 15] and len(ok.data["bodies"]) == 2
    one = run(nx, "nx_measure_bounds", bodies=[a.ref])
    assert one.data["max"] == [10, 10, 10] and one.data["precision"] == "conservative"
    nx.part.WCS.CoordinateSystem.Orientation.Element = matrix((0, 1, 0), (-1, 0, 0), (0, 0, 1))
    refused = run(nx, "nx_measure_bounds", precision="exact")
    assert refused.error["code"] == "NX_UNSUPPORTED_OPTION"
    assert refused.error["field"] == "/precision"


def test_bounds_of_assembly_occurrences_skip_suppressed():
    nx = inspection_nx()
    a, b = assembly(nx, ("A", [0, 0, 0, 10, 10, 10]), ("B", [50, 0, 0, 60, 10, 10]))
    b.IsSuppressed = True
    ok = run(nx, "nx_measure_bounds", scope="assembly")
    assert ok.data["max"] == [10, 10, 10] and len(ok.data["bodies"]) == 1


def test_wall_thickness_samples_inward_rays():
    nx = inspection_nx()
    body = nx.block(20, 10, 5)
    top = next(f for f in body.faces if list(f.normal) == [0, 0, 1])
    ok = run(nx, "nx_measure_wall_thickness", body=body.ref, faces=[top.ref], samples_per_axis=3)
    assert ok.ok, ok
    data = ok.data
    assert data["measured"] == 9 and data["minimum"] == pytest.approx(5)
    assert data["maximum"] == pytest.approx(5) and data["unresolved"] == 0
    sample = data["samples"][0]
    assert sample["opposite_face"]["kind"] == "face" and sample["status"] == "measured"
    every = run(nx, "nx_measure_wall_thickness", body=body.ref, samples_per_axis=1)
    assert every.data["minimum"] == pytest.approx(5) and every.data["maximum"] == pytest.approx(20)
    other = nx.block(1, 1, 1, origin=(50, 0, 0))
    foreign = run(nx, "nx_measure_wall_thickness", body=body.ref, faces=[other.faces[0].ref])
    assert foreign.error["code"] == "NX_WRONG_OWNER" and foreign.error["field"] == "/faces/0"
    many = run(nx, "nx_measure_wall_thickness", body=body.ref, samples_per_axis=50)
    assert many.error["category"] == "invalid_argument"
