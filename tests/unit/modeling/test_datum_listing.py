"""nx_list_objects kind=datum: datum CSYS frames only (not the hidden frames of Scale or
Move Object), and datum axes with origin and direction."""

from tests.kit import fake_nx, run
from tests.kit.objects import Object, point
from tests.unit.modeling.fakes import feature


def frame(nx, origin):
    c = nx._own(Object("CSYS"), "datum_csys")
    c.Origin, c.Orientation = point(*origin), object()
    nx.part.CoordinateSystems.append(c)
    return c


def test_only_datum_csys_frames_are_listed():
    nx = fake_nx()
    datum = frame(nx, (1, 2, 3))
    frame(nx, (0, 0, 0))  # a scale's hidden frame: no datum CSYS feature owns it
    owner = feature(nx, "DATUM_CSYS")
    owner.GetEntities = lambda: [datum]
    nx.datum_axis(origin=(5, 0, 0), direction=(0, 0, 2))
    rows = run(nx, "nx_list_objects", kind="datum").data["items"]
    assert sorted(r["type"] for r in rows) == ["axis", "csys"]
    csys = next(r for r in rows if r["type"] == "csys")
    assert csys["id"] == datum.ref and csys["details"]["origin"] == [1, 2, 3]
    axis = next(r for r in rows if r["type"] == "axis")
    assert axis["details"]["origin"] == [5, 0, 0] and axis["details"]["direction"] == [0, 0, 1]
