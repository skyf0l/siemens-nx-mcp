"""nx_find_geometry, nx_find_holes, nx_create_anchor, nx_resolve_reference on the fake kit."""

import json
import math

from tests.kit import run
from tests.kit.kernel import services
from tests.unit.inspection.fakes import assembly, inspection_nx


def _top(nx, body):
    return next(f for f in body.faces if list(f.normal) == [0, 0, 1])


def test_planar_faces_ranked_highest_with_normal_filter_and_selector():
    nx = inspection_nx()
    low = nx.block(10, 10, 10)
    high = nx.block(10, 10, 5, origin=(20, 0, 10))
    ok = run(
        nx, "nx_find_geometry", kind="face", geometry_type="plane", normal=[0, 0, 1],
        order="highest", axis="z", limit=5,
    )  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    items = ok.data["items"]
    assert [i["id"] for i in items] == [_top(nx, high).ref, _top(nx, low).ref]
    assert items[0]["type"] == "plane" and items[0]["normal"] == [0, 0, 1]
    assert items[0]["center"] == [25, 5, 15] and ok.page["total"] == 2
    sel = json.loads(ok.data["selector"])
    assert sel["kind"] == "face" and sel["normal"] == [0.0, 0.0, 1.0] and "owner" not in sel
    lowest = run(nx, "nx_find_geometry", owner=low.ref, kind="face", order="lowest", axis="z")
    assert lowest.data["items"][0]["center"][2] == 0 and lowest.page["total"] == 6


def test_nearest_edge_uses_native_distance_and_pages():
    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    ok = run(
        nx, "nx_find_geometry", owner=body.ref, kind="edge", geometry_type="line",
        near=[20, 0, 10], limit=1,
    )  # fmt: skip
    row = ok.data["items"][0]
    assert row["distance"] == 0 and row["point"] == [20, 0, 10] and row["type"] == "line"
    assert ok.page == {"offset": 0, "limit": 1, "returned": 1, "total": 12, "next_offset": 1}


def test_rule_errors_are_invalid_arguments_before_nx():
    nx = inspection_nx()
    nx.block(1, 1, 1)
    bad = run(nx, "nx_find_geometry", kind="face", geometry_type="circle")
    assert bad.error["field"] == "/geometry_type" and bad.mutation_outcome == "not_applicable"
    near = run(nx, "nx_find_geometry", kind="face", order="nearest")
    assert near.error["field"] == "/near"
    normal = run(nx, "nx_find_geometry", kind="edge", normal=[0, 0, 1], order="lowest")
    assert normal.error["field"] == "/normal"
    upper = run(nx, "nx_find_geometry", kind="face", order="lowest", axis="Z")
    assert upper.error["category"] == "invalid_argument"


def test_component_owner_reaches_occurrence_faces():
    nx = inspection_nx()
    a, b = assembly(nx, ("A", [0, 0, 0, 10, 10, 10]), ("B", [20, 0, 0, 30, 10, 10]))
    for comp in (a, b):
        for occ in comp.occurrences.values():
            occ.faces = [nx.face(normal=(0, 0, 1), origin=(0, 0, 10), body=occ)]
            nx.remember(occ.faces[0]).sense = 1
    ref = nx.ref_of(b, "component")
    ok = run(nx, "nx_find_geometry", owner=ref, kind="face", order="lowest", axis="x")
    selector = json.loads(ok.data["selector"])
    assert ok.page["total"] == 1 and selector["owner_kind"] == "component"
    assert selector["owner"].endswith(":COMPONENT B 1")


def test_holes_group_coaxial_bores():
    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    for x, radius in ((10, 3.0), (10, 5.0), (30, 3.0)):
        f = nx.remember(nx.face(normal=(0, 0, 1), origin=(x, 10, 0), kind="cylinder", body=body))
        f.radius, f.sense, f.uv_box = radius, -1, (0.0, 2 * 3.141592653589793, 0.0, 1.0)
        body.faces.append(f)
    boss = nx.remember(nx.face(normal=(0, 0, 1), origin=(0, 0, 0), kind="cylinder", body=body))
    boss.radius, boss.sense = 2.0, 1
    body.faces.append(boss)
    ok = run(nx, "nx_find_holes", owner=body.ref)
    assert ok.ok, ok
    assert ok.page["total"] == 3 and len(ok.data["groups"]) == 2
    first = ok.data["groups"][0]
    assert first["radii"] == [3.0, 5.0] and len(first["faces"]) == 2
    assert all(row["full"] for row in ok.data["items"])


def test_handle_anchor_resolves_and_goes_stale():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    face = _top(nx, body)
    made = run(nx, "nx_create_anchor", object=face.ref)
    assert made.ok and made.data["anchor"] == f"@work.prt:face=RM{face.Tag} R{face.Tag}"
    back = run(nx, "nx_resolve_reference", anchor=made.data["anchor"])
    assert back.data["object"]["id"] == face.ref and back.data["via"] == "anchor"
    body.faces.remove(face)
    gone = run(nx, "nx_resolve_reference", anchor=made.data["anchor"])
    assert gone.error["code"] == "NX_STALE_REFERENCE" and gone.error["field"] == "/anchor"
    other = run(nx, "nx_resolve_reference", anchor="@other.prt:face=RM1 R1")
    assert other.error["code"] == "NX_WRONG_OWNER"
    both = run(nx, "nx_resolve_reference")
    assert both.error["category"] == "invalid_argument"


def test_selector_reruns_rule_and_reports_ties():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    found = run(nx, "nx_find_geometry", kind="face", near=[5, 5, 13])
    text = found.data["selector"]
    selector = json.loads(text)
    assert selector["part"] == "work.prt" and selector["near"] == [5.0, 5.0, 13.0]
    ok = run(nx, "nx_resolve_reference", selector=text)
    assert ok.ok, ok
    assert ok.data["object"]["id"] == _top(nx, body).ref and ok.data["rank"] == 3
    tied = run(nx, "nx_resolve_reference", selector=json.dumps(dict(selector, near=[5, 5, 5])))
    assert tied.error["code"] == "NX_AMBIGUOUS_REFERENCE"
    assert len(tied.error["details"]["candidates"]) == 2
    foreign = run(nx, "nx_resolve_reference", selector=json.dumps(dict(selector, part="x.prt")))
    assert foreign.error["code"] == "NX_WRONG_OWNER" and foreign.error["field"] == "/selector"
    junk = run(nx, "nx_resolve_reference", selector='{"kind": "face"}')
    assert junk.error["code"] == "NX_INVALID_ARGUMENT" and junk.error["field"] == "/selector"


def test_owner_selector_uses_the_owner_anchor():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    nx.block(10, 10, 10, origin=(0, 0, 20))
    refs = services(nx).references
    plain = refs.row
    anchor = "@work.prt:BLOCK#0"
    refs.row = lambda obj, kind, **k: (
        {**plain(obj, kind), "anchor": anchor} if obj is body else plain(obj, kind)
    )
    refs.find_anchor = lambda text, kind: (
        plain(body, "body") if (text, kind) == (anchor, "body") else None
    )
    found = run(nx, "nx_find_geometry", owner=body.ref, kind="face", order="highest")
    selector = json.loads(found.data["selector"])
    assert selector["owner"] == anchor and selector["owner_kind"] == "body"
    ok = run(nx, "nx_resolve_reference", selector=found.data["selector"])
    assert ok.data["object"]["id"] == _top(nx, body).ref
    refs.row = plain
    lost = run(nx, "nx_find_geometry", owner=body.ref, kind="face", order="highest")
    assert lost.ok and lost.data["selector"] is None


def test_order_defaults_to_nearest_with_near_else_lowest():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    assert run(nx, "nx_find_geometry").error is None
    plain = run(nx, "nx_find_geometry", kind="face", geometry_type="plane", axis="z")
    assert plain.ok and plain.page["total"] == 6, plain
    assert plain.data["items"][0]["center"][2] == 0 and plain.data["items"][-1]["center"][2] == 10
    assert json.loads(plain.data["selector"])["order"] == "lowest"
    near = run(nx, "nx_find_geometry", kind="face", near=[5, 5, 13], limit=1)
    assert near.data["items"][0]["id"] == _top(nx, body).ref
    assert near.data["items"][0]["distance"] == 3
    assert json.loads(near.data["selector"])["order"] == "nearest"
    bad = run(nx, "nx_find_geometry", kind="face", order="nearest")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/near"
    assert "highest or lowest" in bad.error["message"]


def test_feature_owner_gives_only_the_features_own_faces_and_edges():
    from tests.kit.objects import Feature

    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    nx.block(5, 5, 5, origin=(100, 0, 0))
    bore = nx.remember(nx.face(normal=(0, 0, 1), origin=(10, 10, 0), kind="cylinder", body=body))
    bore.radius, bore.sense, bore.uv_box = 3.0, -1, (0.0, 2 * math.pi, 0.0, 1.0)
    body.faces.append(bore)
    hole = Feature("HOLE(2)", bodies=[body])
    hole.own_faces, hole.own_edges = [bore], body.edges[:2]
    ref = nx.ref_of(hole, "feature")
    faces = run(nx, "nx_find_geometry", owner=ref, kind="face")
    assert faces.ok and faces.page["total"] == 1, faces
    assert faces.data["items"][0]["id"] == bore.ref and faces.data["items"][0]["role"] == "bore"
    edges = run(nx, "nx_find_geometry", owner=ref, kind="edge", order="highest")
    assert edges.page["total"] == 2
    whole = run(nx, "nx_find_geometry", owner=body.ref, kind="face")
    assert whole.page["total"] == 7
    holes = run(nx, "nx_find_holes", owner=ref)
    assert holes.page["total"] == 1 and holes.data["items"][0]["id"] == bore.ref
    hole.own_faces = []
    empty = run(nx, "nx_find_geometry", owner=ref, kind="face")
    assert empty.ok and empty.page["total"] == 0
