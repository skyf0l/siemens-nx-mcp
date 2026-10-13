"""nx_analyze_body_changes on the fake kit: matching, changed, added and removed faces,
volume and area differences, and refusals before any face is read."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.inspection import body_compare
from tests.kit import run
from tests.unit.inspection.fakes import inspection_nx


def face_box(nx, face):
    return [float(v) for v in nx.uf.ModlGeneral.AskBoundingBox(face.Tag)]


def compare_nx(monkeypatch):
    """An inspection fake whose AskFaceData reports the face box and whose face areas
    come from ``nx.areas`` (else the box's planar extent)."""
    nx = inspection_nx()
    plain = nx.uf.Modeling.AskFaceData
    nx.areas = {}

    def face_data(tag):
        data = list(plain(tag))
        data[3] = face_box(nx, nx.objects[tag])
        return tuple(data)

    def area(_tx, face):
        if int(face.Tag) in nx.areas:
            return nx.areas[int(face.Tag)]
        box = face_box(nx, face)
        sizes = sorted(box[i + 3] - box[i] for i in range(3))
        return sizes[1] * sizes[2]

    nx.uf.Modeling.AskFaceData = face_data
    monkeypatch.setattr(body_compare, "face_area", area)
    return nx


def test_identical_copies_match_face_for_face(monkeypatch):
    nx = compare_nx(monkeypatch)
    a, b = nx.block(40, 40, 40), nx.block(40, 40, 40)
    ok = run(nx, "nx_analyze_body_changes", control=a.ref, sample=b.ref)
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    d = ok.data
    assert d["identical"] is True
    assert d["counts"] == {"matching": 6, "changed": 0, "added": 0, "removed": 0}
    assert d["volume"] == {"control": 64000, "sample": 64000, "difference": 0.0}
    assert d["area"]["control"] == pytest.approx(9600) and d["area"]["difference"] == 0
    assert d["tolerance"] == pytest.approx(0.001) and "truncated" not in d


def test_a_new_hole_changes_two_faces_and_adds_its_wall(monkeypatch):
    nx = compare_nx(monkeypatch)
    a, b = nx.block(40, 40, 40), nx.block(40, 40, 40)
    top, bottom = b.face((0, 0, 1)), b.face((0, 0, -1))
    wall = nx.block(10, 10, 40, origin=(15, 15, 0)).faces[0]  # a stand-in face
    nx.face_data[wall.Tag] = ("cylinder", (20, 20, 0), (0, 0, 1))
    wall.radius = 5.0
    nx.objects[int(wall.Tag)] = wall
    b.faces.append(wall)
    for face in (top, bottom):
        nx.areas[int(face.Tag)] = 1600 - 78.54
        edges = face.GetEdges()
        face.GetEdges = lambda edges=edges: [*edges, NS(Tag=0)]
    b.volume = 64000 - 3141.6
    d = run(nx, "nx_analyze_body_changes", control=a.ref, sample=b.ref).data
    assert d["identical"] is False
    assert d["counts"] == {"matching": 4, "changed": 2, "added": 1, "removed": 0}
    assert {r["sample"]["id"] for r in d["changed"]} == {top.ref, bottom.ref}
    assert {r["control"]["id"] for r in d["changed"]} == {a.face((0, 0, 1)).ref,
                                                          a.face((0, 0, -1)).ref}  # fmt: skip
    assert d["changed"][0]["area_change"] == pytest.approx(-78.54)
    assert [r["id"] for r in d["added"]] == [wall.ref] and d["removed"] == []
    assert d["volume"]["difference"] == pytest.approx(-3141.6)


def test_a_taller_version_moves_its_top_and_stretches_the_sides(monkeypatch):
    nx = compare_nx(monkeypatch)
    a, b = nx.block(40, 40, 40), nx.block(40, 40, 50)
    d = run(nx, "nx_analyze_body_changes", control=a.ref, sample=b.ref, limit=2).data
    assert d["counts"] == {"matching": 1, "changed": 4, "added": 1, "removed": 1}
    assert d["removed"][0]["id"] == a.face((0, 0, 1)).ref
    assert d["added"][0]["id"] == b.face((0, 0, 1)).ref
    assert len(d["changed"]) == 2 and d["truncated"] is True
    assert d["volume"]["difference"] == 16000


def test_tolerance_absorbs_small_moves(monkeypatch):
    nx = compare_nx(monkeypatch)
    a, b = nx.block(40, 40, 40), nx.block(40, 40, 40.005)
    near = run(nx, "nx_analyze_body_changes", control=a.ref, sample=b.ref,
               tolerance=0.01).data  # fmt: skip
    assert near["counts"]["matching"] == 6 and near["volume"]["difference"] == pytest.approx(8)
    strict = run(nx, "nx_analyze_body_changes", control=a.ref, sample=b.ref).data
    assert strict["counts"]["matching"] == 1


def test_surface_identity_rules():
    plane = {"type": 22, "point": [0, 0, 5], "dir": [0, 0, 1], "box": [0] * 6,
             "radius": 0.0, "rad_data": 0.0}  # fmt: skip
    assert body_compare.same_surface(plane, {**plane, "point": [9, 9, 5]}, 1e-3)
    assert not body_compare.same_surface(plane, {**plane, "dir": [0, 0, -1]}, 1e-3)
    assert not body_compare.same_surface(plane, {**plane, "point": [0, 0, 5.1]}, 1e-3)
    cyl = {**plane, "type": 16, "point": [1, 2, 0], "radius": 5.0}
    assert body_compare.same_surface(cyl, {**cyl, "point": [1, 2, 30], "dir": [0, 0, -1]}, 1e-3)
    assert not body_compare.same_surface(cyl, {**cyl, "radius": 6.0}, 1e-3)
    assert not body_compare.same_surface(cyl, {**cyl, "point": [1, 3, 0]}, 1e-3)
    assert not body_compare.same_surface(cyl, plane, 1e-3)
    sphere = {**plane, "type": 18, "radius": 4.0}
    assert body_compare.same_surface(sphere, dict(sphere), 1e-3)
    assert not body_compare.same_surface(sphere, {**sphere, "point": [0, 0, 6]}, 1e-3)


@pytest.mark.parametrize(
    ("args", "field", "code"),
    [
        ({"control": "A", "sample": "A"}, "/sample", "NX_INVALID_ARGUMENT"),
        ({"control": "A", "sample": "B", "tolerance": 0}, "/tolerance", "NX_INVALID_ARGUMENT"),
        ({"control": "SHEET", "sample": "B"}, "/control", "NX_WRONG_GEOMETRY"),
    ],
)
def test_refusals_come_before_any_face_is_read(monkeypatch, args, field, code):
    nx = compare_nx(monkeypatch)
    a, b, sheet = nx.block(10, 10, 10), nx.block(10, 10, 10), nx.block(10, 10, 1)
    sheet.IsSolidBody = False
    swap = {"A": a.ref, "B": b.ref, "SHEET": sheet.ref}
    read = []
    monkeypatch.setattr(body_compare, "signature", lambda tx, f: read.append(f))
    bad = run(nx, "nx_analyze_body_changes", **{k: swap.get(v, v) for k, v in args.items()})
    assert (bad.error["code"], bad.error["field"]) == (code, field), bad
    assert read == []
