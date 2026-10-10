"""nx_set_name (domains/session/naming.py): rename by reference, NX's stored name read
back, the part's references invalidated, a fresh reference returned."""

from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Feature


def feature(nx, name):
    f = nx._own(Feature(name), "feature")
    nx.part.Features.append(f)
    return f


def test_rename_returns_stored_name_and_fresh_reference():
    nx = fake_nx()
    f, other = feature(nx, "EXTRUDE(1)"), feature(nx, "BLEND(2)")
    old, stale = f.ref, other.ref
    ok = run(nx, "nx_set_name", object=old, name="Boss")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert f.Name == "Boss" and ok.data["name"] == "Boss" and ok.data["requested"] == "Boss"
    assert ok.data["object"]["kind"] == "feature" and ok.data["object"]["name"] == "Boss"
    assert ok.data["object"]["id"] in nx.refs and stale not in nx.refs
    assert ok.changes["references"] == "part_invalidated"
    assert services(nx).history[-1]["method"] == "nx_set_name"  # undoable


def test_normalised_name_is_read_back():
    nx = fake_nx()
    f = feature(nx, "EXTRUDE(1)")
    f.SetName = lambda name: setattr(f, "Name", name.upper())
    ok = run(nx, "nx_set_name", object=f.ref, name="Boss")
    assert ok.data["name"] == "BOSS" and ok.data["requested"] == "Boss"


def test_rename_refuses_bad_names_and_stale_refs():
    nx = fake_nx()
    f = feature(nx, "EXTRUDE(1)")
    for name in ("", "x" * 133, "a\nb"):
        bad = run(nx, "nx_set_name", object=f.ref, name=name)
        assert bad.error["code"] == "NX_INVALID_ARGUMENT", (name, bad)
    stale = run(nx, "nx_set_name", object="f:999", name="A")
    assert stale.error["code"] == "NX_STALE_REFERENCE"
    assert stale.mutation_outcome == "not_started" and f.Name == "EXTRUDE(1)"
    display_name = run(nx, "nx_set_name", object="EXTRUDE(1)", name="A")
    assert display_name.error["code"] == "NX_INVALID_ARGUMENT"  # references only
