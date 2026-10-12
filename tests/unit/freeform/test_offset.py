"""nx_create_offset_surface on the shared fake."""

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import face, flat, freeform_nx, made, sheet


def plate(nx, z=0.0, normal=(0, 0, 1)):
    """A planar face 20 x 10 at height z whose interior points come from (u, v)."""
    f = face(nx, flat(), area=200.0, normal=normal)
    f.at = lambda u, v, z=z: [20.0 * u, 10.0 * v, z]
    sheet(nx, (0, 0, z, 20, 10, z), [f])
    return f


def offset_commit(nx, *heights):
    """The commit builds one sheet per height (z of a 20 x 10 plate)."""

    def commit(_b):
        bodies = []
        for z in heights:
            f = face(nx, flat(), area=200.0)
            bodies.append(sheet(nx, (0, 0, z, 20, 10, z), [f]))
        return made(nx, "OFFSET_SURFACE", bodies)

    nx.on_commit("OffsetSurfaceBuilder", commit)


def test_offset_faces_by_signed_distance_reads_back_samples():
    nx = freeform_nx()
    f = plate(nx)
    offset_commit(nx, -5)
    ok = run(nx, "nx_create_offset_surface", faces=[f.ref], distance="-5 mm")
    assert ok.ok, ok
    b = nx.last("OffsetSurfaceBuilder")
    assert nx.license_queries == ["nx_freeform_1"]
    assert b.Tolerance == pytest.approx(0.001)
    face_set = nx.face_sets[0]
    assert face_set.Offset.RightHandSide == "-5.0"
    assert face_set.FaceCollector.rules == [("FaceDumb", [f])]
    assert b.FaceSets.Append.calls[0][0][0] is face_set
    (row,) = ok.data["sets"]
    assert row["distance"] == -5.0 and row["faces"] == 1 and row["samples"] == 9
    assert row["measured_min"] == pytest.approx(5) == row["measured_max"]
    assert row["gap"] == pytest.approx(0)
    assert ok.data["sheet"] is True and ok.data["area"] == pytest.approx(200)
    assert nx.live_builders == 0


def test_offset_sets_have_their_own_distance():
    nx = freeform_nx()
    low, high = plate(nx, 0), plate(nx, 50)
    offset_commit(nx, 2, 53)
    ok = run(nx, "nx_create_offset_surface", sets=[
        {"faces": [low.ref], "distance": 2}, {"faces": [high.ref], "distance": 3}])  # fmt: skip
    assert ok.ok, ok
    assert [s.Offset.RightHandSide for s in nx.face_sets] == ["2.0", "3.0"]
    assert [s["measured_max"] for s in ok.data["sets"]] == pytest.approx([2, 3])
    assert len(ok.data["bodies"]) == 2


def test_offset_expression_drives_the_distance():
    nx = freeform_nx()
    nx.expression("laminate", "6", 6.0)
    f = plate(nx)
    offset_commit(nx, 6)
    ok = run(nx, "nx_create_offset_surface", faces=[f.ref], distance="laminate")
    assert ok.ok, ok
    assert nx.face_sets[0].Offset.RightHandSide == "laminate"


def test_offset_wrong_side_or_distance_rolls_back():
    nx = freeform_nx()
    f = plate(nx)
    offset_commit(nx, -5)  # NX went against the normal
    bad = run(nx, "nx_create_offset_surface", faces=[f.ref], distance=5)
    assert bad.error["details"]["check"] == "offset", bad
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"distance": 5}, "/faces"),
        ({"faces": ["@"], "distance": 0}, "/distance"),
        ({"faces": ["@", "@"], "distance": 1}, "/faces/1"),
        ({"faces": ["@"], "distance": 1, "sets": [{"faces": ["@"], "distance": 1}]}, "/sets"),
        ({"sets": [{"faces": ["@"], "distance": 1}, {"faces": ["@"], "distance": 2}]},
         "/sets/1/faces/0"),
        ({"faces": ["@"], "distance": 1, "tolerance": -1}, "/tolerance"),
    ],
)  # fmt: skip
def test_offset_rejections_before_nx(args, field):
    nx = freeform_nx()
    f = plate(nx)

    def put(value):
        if value == "@":
            return f.ref
        if isinstance(value, list):
            return [put(v) for v in value]
        if isinstance(value, dict):
            return {k: put(v) for k, v in value.items()}
        return value

    bad = run(nx, "nx_create_offset_surface", **put(args))
    assert bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_offset_without_license_refuses_before_the_builder():
    nx = freeform_nx()
    f = plate(nx)
    nx.licenses["nx_freeform_1"] = False
    bad = run(nx, "nx_create_offset_surface", faces=[f.ref], distance=1)
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE"
    assert bad.error["details"]["license"] == "nx_freeform_1"
    assert not nx.builders


def test_offset_faulty_body_rolls_back():
    nx = freeform_nx()
    f = plate(nx)

    def commit(_b):
        body = sheet(nx, (0, 0, 5, 20, 10, 5), [face(nx, flat(), area=200.0)])
        nx.faults[int(body.Tag)] = 1
        return made(nx, "OFFSET_SURFACE", [body])

    nx.on_commit("OffsetSurfaceBuilder", commit)
    bad = run(nx, "nx_create_offset_surface", faces=[f.ref], distance=5)
    assert bad.error["code"] == "NX_BUILD_FAILED"
    assert bad.error["details"]["check"] == "body_health"
    assert bad.mutation_outcome == "rolled_back"


def test_offset_collects_sheets_the_feature_does_not_list():
    """NX 2406: two disconnected face sets build two sheets, GetBodies lists one."""
    nx = freeform_nx()
    low, high = plate(nx, 0), plate(nx, 50)

    def commit(_b):
        first = sheet(nx, (0, 0, 2, 20, 10, 2), [face(nx, flat(), area=200.0)])
        sheet(nx, (0, 0, 53, 20, 10, 53), [face(nx, flat(), area=200.0)])
        return made(nx, "OFFSET_SURFACE", [first])

    nx.on_commit("OffsetSurfaceBuilder", commit)
    ok = run(nx, "nx_create_offset_surface", sets=[
        {"faces": [low.ref], "distance": 2}, {"faces": [high.ref], "distance": 3}])  # fmt: skip
    assert ok.ok, ok
    assert len(ok.data["bodies"]) == 2 and ok.data["area"] == pytest.approx(400)
    assert ok.data["bounds"]["max"][2] == pytest.approx(53)
