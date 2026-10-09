import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import select
from tests.kit import fake_nx


def _code(call, *a, **k):
    with pytest.raises(NXError) as error:
        call(*a, **k)
    return error.value.code


def test_planar_face_and_datum_plane():
    nx = fake_nx()
    block = nx.block(40, 20, 10)
    top = block.face(normal=(0, 0, 1))
    obj, origin, normal = select.planar(nx.tx, top.ref)
    assert obj is top and origin == [0.0, 0.0, 10.0] and normal == [0.0, 0.0, 1.0]
    datum = nx.datum_plane(origin=(0, 0, 5), normal=(0, 2, 0))
    assert select.planar(nx.tx, datum.ref)[1:] == ([0.0, 0.0, 5.0], [0.0, 1.0, 0.0])


def test_planar_rejects_wrong_geometry_kind_and_owner():
    nx = fake_nx()
    cylinder = nx.face(kind="cylinder")
    assert _code(select.planar, nx.tx, cylinder.ref) == "NX_WRONG_GEOMETRY"
    axis = nx.datum_axis()
    assert _code(select.planar, nx.tx, axis.ref) == "NX_WRONG_KIND"  # resolve rejects the kind
    face = nx.face()
    face.IsOccurrence = True
    assert _code(select.planar, nx.tx, face.ref) == "NX_WRONG_OWNER"
    other = nx.face()
    other.OwningPart = object()
    assert _code(select.planar, nx.tx, other.ref) == "NX_WRONG_OWNER"
    assert _code(select.planar, nx.tx, "fa:999") == "NX_STALE_REFERENCE"


def test_plane_union():
    nx = fake_nx()
    assert select.plane(nx.tx, {"principal": "xz"}) == (None, [0, 0, 0], [0.0, -1.0, 0.0])
    _, origin, normal = select.plane(nx.tx, {"origin": [1, 2, 3], "normal": [0, 0, 2]})
    assert origin == [1.0, 2.0, 3.0] and normal == [0.0, 0.0, 1.0]
    assert _code(select.plane, nx.tx, {"principal": "xx"}) == "NX_INVALID_ARGUMENT"
    assert _code(select.plane, nx.tx, 5) == "NX_INVALID_ARGUMENT"


def test_axis_union():
    nx = fake_nx()
    edge = nx.edge((0, 0, 0), (0, 10, 0))
    assert select.axis(nx.tx, edge.ref)[1:] == ([0.0, 0.0, 0.0], [0.0, 1.0, 0.0])
    datum = nx.datum_axis(origin=(1, 1, 1), direction=(0, 0, -3))
    assert select.axis(nx.tx, datum.ref)[2] == [0.0, 0.0, -1.0]
    hole = nx.face(kind="cylinder", origin=(5, 5, 0), normal=(0, 0, 1))
    assert select.axis(nx.tx, hole.ref)[1:] == ([5.0, 5.0, 0.0], [0.0, 0.0, 1.0])
    assert select.axis(nx.tx, {"principal": "-x"})[2] == [-1.0, 0.0, 0.0]
    assert select.axis(nx.tx, {"origin": [0, 0, 0], "direction": [2, 0, 0]})[2] == [1.0, 0.0, 0.0]
    assert _code(select.axis, nx.tx, nx.face().ref) == "NX_WRONG_GEOMETRY"
    arc = nx.edge((0, 0, 0), (1, 0, 0), linear=False)
    assert _code(select.axis, nx.tx, arc.ref) == "NX_WRONG_GEOMETRY"


def test_direction_union():
    nx = fake_nx()
    assert select.direction(nx.tx, [0, 3, 4]) == [0.0, 0.6, 0.8]
    assert select.direction(nx.tx, {"principal": "z"}) == [0.0, 0.0, 1.0]
    assert select.direction(nx.tx, nx.edge((0, 0, 0), (2, 0, 0)).ref) == [1.0, 0.0, 0.0]
    assert select.direction(nx.tx, nx.face(normal=(0, -1, 0)).ref) == [0.0, -1.0, 0.0]
    assert select.direction(nx.tx, nx.datum_plane(normal=(1, 0, 0)).ref) == [1.0, 0.0, 0.0]
    assert select.direction(nx.tx, nx.datum_axis(direction=(0, 0, 9)).ref) == [0.0, 0.0, 1.0]
    assert _code(select.direction, nx.tx, None) == "NX_INVALID_ARGUMENT"


def test_lists_of_owned_objects():
    nx = fake_nx()
    block = nx.block(1, 1, 1)
    assert select.bodies(nx.tx, [block.ref]) == [block]
    assert select.faces(nx.tx, [f.ref for f in block.faces[:2]]) == block.faces[:2]
    edges = block.edges[:3]
    assert select.edges(nx.tx, [e.ref for e in edges]) == edges
    assert _code(select.edges, nx.tx, [edges[0].ref, edges[0].ref]) == "NX_INVALID_ARGUMENT"
    assert _code(select.faces, nx.tx, [block.ref]) == "NX_WRONG_KIND"
    assert select.owned_by_work_part(nx.tx, block)
