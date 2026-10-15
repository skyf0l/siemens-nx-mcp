"""Datum tools warn about a name already in use and about absurd coordinates."""

from tests.kit import run
from tests.unit.modeling.test_datums import datum_nx, plane_feature


def test_a_second_datum_with_the_same_name_and_a_far_origin_are_noted():
    nx = datum_nx()
    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (5, 5, 5), (0, 0, 1)))
    first = run(nx, "nx_create_datum_plane", method="point_normal", origin=[5, 5, 5],
                normal=[0, 0, 1], name="MID")  # fmt: skip
    assert first.ok and first.warnings == [], first
    second = run(nx, "nx_create_datum_plane", method="point_normal", origin=[5, 5, 5],
                 normal=[0, 0, 1], name="mid")  # fmt: skip
    assert second.ok, second
    assert any("already named 'mid'" in w["message"] for w in second.warnings), second.warnings
    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (1e12, 0, 0), (0, 0, 1)))
    far = run(nx, "nx_create_datum_plane", method="point_normal", origin=[1e12, 0, 0],
              normal=[0, 0, 1])  # fmt: skip
    assert far.ok and any("from the origin" in w["message"] for w in far.warnings), far


def test_datum_csys_returns_and_names_its_planes_and_axes():
    from types import SimpleNamespace as NS

    from tests.kit.objects import Object, matrix, point
    from tests.unit.modeling.fakes import feature, modeling_nx

    nx = modeling_nx()
    children = []

    def made(_b):
        f = feature(nx, "DATUM_CSYS")
        frame = Object("CSYS")
        frame.Origin = point(0, 0, 0)
        frame.Orientation = NS(Element=matrix((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        for normal in ((0, 0, 1), (1, 0, 0), (0, 1, 0)):
            plane = Object("DATUM_PLANE")
            plane.Origin, plane.Normal = point(0, 0, 0), point(*normal)
            children.append(plane)
        for direction in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            axis = Object("DATUM_AXIS")
            axis.Origin, axis.Direction = point(0, 0, 0), point(*direction)
            children.append(axis)
        f.GetEntities = lambda: [frame, *children]
        return f

    nx.on_commit("DatumCsysBuilder", made)
    ok = run(nx, "nx_create_datum_csys", origin=[0, 0, 0], name="REF")
    assert ok.ok, ok
    assert sorted(ok.data["planes"]) == ["xy", "xz", "yz"] and sorted(ok.data["axes"]) == list(
        "xyz"
    )
    assert ok.data["planes"]["xy"]["kind"] == "datum_plane"
    assert [c.Name for c in children[:4]] == ["REF_XY", "REF_YZ", "REF_XZ", "REF_X"]
