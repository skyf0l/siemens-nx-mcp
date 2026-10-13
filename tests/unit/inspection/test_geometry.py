"""nx_find_geometry, nx_find_holes, nx_create_anchor, nx_resolve_reference on the fake kit."""

import json
import math

import pytest

from tests.kit import run
from tests.kit.kernel import services
from tests.unit.inspection.fakes import arc_spec, assembly, inspection_nx, spec_curve


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
    upper = run(nx, "nx_find_geometry", kind="face", order="lowest", axis="w")
    assert upper.error["category"] == "invalid_argument"
    natural = run(nx, "nx_find_geometry", kind="Faces", order="Lowest", axis="Z")
    assert natural.ok, natural  # gate 1 takes natural spellings of options


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
        f.GetEdges = lambda: []
        body.faces.append(f)
    boss = nx.remember(nx.face(normal=(0, 0, 1), origin=(0, 0, 0), kind="cylinder", body=body))
    boss.radius, boss.sense = 2.0, 1
    body.faces.append(boss)
    ok = run(nx, "nx_find_holes", owner=body.ref)
    assert ok.ok, ok
    assert ok.page["total"] == 2 and len(ok.data["groups"]) == 2  # holes, not faces
    assert ok.page["faces"] == 3 and len(ok.data["items"]) == 3
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


def _no_anchor(row):
    """A row of an object without a journal anchor (the kit rows carry one now)."""
    return {k: v for k, v in row.items() if k != "anchor"}


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
    refs.row = lambda obj, kind, **k: _no_anchor(plain(obj, kind))
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
    bore.GetEdges = lambda: []
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


def _axial_faces(nx):
    """A frustum face (r 10 at z=50 to r 5 at z=70, axis +Z) and a r=4 cylinder."""
    body = nx.block(10, 10, 10)
    cone = nx.face(normal=(0, 0, 1), origin=(0, 0, 60), kind="cone", body=body)
    cone.radius, cone.half_angle, cone.sense = 7.5, math.atan(0.25), 1
    rings = [
        spec_curve(nx, arc_spec((0, 0, 70), 5, 0, 2 * math.pi)),
        spec_curve(nx, arc_spec((0, 0, 50), 10, 0, 2 * math.pi)),
    ]
    cone.GetEdges = lambda: rings
    bore = nx.face(normal=(0, 0, 1), origin=(0, 0, 0), kind="cylinder", body=body)
    bore.radius, bore.sense = 4.0, -1
    bore.GetEdges = lambda: []
    for face in (cone, bore):
        nx.remember(face)
        body.faces.append(face)
    return cone, bore


def test_cone_faces_carry_both_end_radii_and_the_half_angle():
    nx = inspection_nx()
    cone, _bore = _axial_faces(nx)
    ok = run(nx, "nx_find_geometry", geometry_type="cone")
    assert ok.ok, ok
    (row,) = ok.data["items"]
    assert row["id"] == cone.ref and row["type"] == "cone"
    assert row["radii"] == pytest.approx([10, 5]) and row["axis"] == [0.0, 0.0, 1.0]
    assert row["half_angle"] == pytest.approx(14.0362435)
    bad = run(nx, "nx_find_geometry", kind="edge", geometry_type="cone")
    assert bad.error["field"] == "/geometry_type"


def test_listed_cylinder_and_cone_faces_carry_their_radii():
    nx = inspection_nx()
    _axial_faces(nx)
    cones = run(nx, "nx_list_objects", kind="face", type="cone").data["items"]
    assert cones[0]["details"]["radii"] == pytest.approx([10, 5])
    assert cones[0]["details"]["half_angle"] == pytest.approx(14.0362435)
    bores = run(nx, "nx_list_objects", kind="face", type="cylinder").data["items"]
    assert bores[0]["details"]["radius"] == 4.0 and bores[0]["details"]["axis"] == [0, 0, 1.0]


def test_normal_takes_axis_words_and_implies_plane_and_outermost_order():
    nx = inspection_nx()
    low = nx.block(10, 10, 10)
    high = nx.block(10, 10, 5, origin=(20, 0, 10))
    up = run(nx, "nx_find_geometry", normal="+Z")
    assert up.ok, up
    assert [i["id"] for i in up.data["items"]] == [_top(nx, high).ref, _top(nx, low).ref]
    rule = json.loads(up.data["selector"])
    assert rule["geometry_type"] == "plane" and rule["order"] == "highest"
    assert rule["axis"] == "z" and rule["normal"] == [0.0, 0.0, 1.0]
    down = run(nx, "nx_find_geometry", normal="bottom", limit=1)
    assert down.data["items"][0]["center"][2] == 0
    assert json.loads(down.data["selector"])["order"] == "lowest"
    left = run(nx, "nx_find_geometry", normal="-x", limit=1)
    assert left.data["items"][0]["normal"] == [-1, 0, 0]
    assert left.data["items"][0]["center"][0] == 0
    assert json.loads(left.data["selector"])["axis"] == "x"
    vector = run(nx, "nx_find_geometry", normal=[0, 0, 1], order="lowest", axis="z")
    assert vector.data["items"][0]["id"] == _top(nx, low).ref
    bad = run(nx, "nx_find_geometry", normal="+q")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/normal"
    assert "+z" in bad.error["message"] and "top" in bad.error["message"]
    edge = run(nx, "nx_find_geometry", kind="edge", normal="+z")
    assert edge.error["field"] == "/normal"


def _bore_and_boss(nx, body):
    faces = []
    for x, sense in ((2.0, -1), (8.0, 1)):
        f = nx.remember(nx.face(normal=(0, 0, 1), origin=(x, 5, 0), kind="cylinder", body=body))
        f.radius, f.sense, f.uv_box = 3.0, sense, (0.0, 2 * math.pi, 0.0, 1.0)
        f.GetEdges = lambda: []
        body.faces.append(f)
        faces.append(f)
    return faces


def test_role_filter_keeps_bores_or_bosses_and_reruns_from_the_selector():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    bore, boss = _bore_and_boss(nx, body)
    both = run(nx, "nx_find_geometry", radius=3)
    assert both.ok and both.page["total"] == 2, both
    assert json.loads(both.data["selector"])["geometry_type"] == "cylinder"
    bores = run(nx, "nx_find_geometry", role="bore")
    assert [i["id"] for i in bores.data["items"]] == [bore.ref]
    rule = json.loads(bores.data["selector"])
    assert rule["role"] == "bore" and rule["geometry_type"] == "cylinder"
    again = run(nx, "nx_resolve_reference", selector=bores.data["selector"])
    assert again.ok and again.data["object"]["id"] == bore.ref, again
    bosses = run(nx, "nx_find_geometry", geometry_type="cylinder", role="boss")
    assert [i["id"] for i in bosses.data["items"]] == [boss.ref]
    wrong = run(nx, "nx_find_geometry", geometry_type="plane", role="bore")
    assert wrong.error["field"] == "/role"
    edges = run(nx, "nx_find_geometry", kind="edge", role="bore")
    assert edges.error["field"] == "/role"
    circles = run(nx, "nx_find_geometry", kind="edge", radius=3)
    assert circles.ok and json.loads(circles.data["selector"])["geometry_type"] == "circle"


def test_owner_takes_a_row_anchor_and_explains_names():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    nx.block(10, 10, 10, origin=(0, 0, 20))
    anchor = run(nx, "nx_create_anchor", object=body.ref).data["anchor"]
    ok = run(nx, "nx_find_geometry", owner=anchor, normal="+z")
    assert ok.ok and [i["id"] for i in ok.data["items"]] == [_top(nx, body).ref], ok
    face = run(nx, "nx_create_anchor", object=_top(nx, body).ref).data["anchor"]
    wrong = run(nx, "nx_find_geometry", owner=face)
    assert wrong.error["code"] == "NX_WRONG_KIND" and wrong.error["field"] == "/owner"
    name = run(nx, "nx_find_geometry", owner="BLOCK(1)")
    assert name.error["code"] == "NX_INVALID_ARGUMENT" and name.error["field"] == "/owner"
    assert "not a reference" in name.error["message"]
    junk = run(nx, "nx_find_geometry", owner="@work.prt")
    assert junk.error["field"] == "/owner" and "anchor" in junk.error["message"]


def test_resolve_accepts_live_ids_selector_objects_and_swapped_fields():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = _top(nx, body)
    live = run(nx, "nx_resolve_reference", anchor=top.ref)
    assert live.ok and live.data == {"object": live.data["object"], "via": "reference"}, live
    assert live.data["object"]["id"] == top.ref
    found = run(nx, "nx_find_geometry", normal="+z")
    as_object = run(nx, "nx_resolve_reference", selector=json.loads(found.data["selector"]))
    assert as_object.ok and as_object.data["object"]["id"] == top.ref, as_object
    swapped = run(nx, "nx_resolve_reference", anchor=found.data["selector"])
    assert swapped.ok and swapped.data["via"] == "selector", swapped
    made = run(nx, "nx_create_anchor", object=top.ref).data["anchor"]
    into_selector = run(nx, "nx_resolve_reference", selector=made)
    assert into_selector.ok and into_selector.data["via"] == "anchor", into_selector
    missing = run(nx, "nx_resolve_reference", selector={"kind": "face"})
    assert missing.error["field"] == "/selector" and "missing" in missing.error["message"]
    bad = run(nx, "nx_resolve_reference", anchor="@work.prt")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and "nx_create_anchor" in bad.error["message"]


def test_create_anchor_of_a_feature_is_its_row_anchor():
    from tests.kit.objects import Feature

    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    feature = Feature("BLEND(5)", bodies=[body])
    feature.JournalIdentifier, feature.OwningPart, feature.IsOccurrence = "BLEND(5)", nx.part, False
    nx.part.Features.append(feature)
    ref = nx.ref_of(feature, "feature")
    refs = services(nx).references
    plain, anchor = refs.row, "@work.prt:BLEND(5)"
    refs.row = lambda obj, kind, **k: (
        {**plain(obj, kind), "anchor": anchor} if obj is feature else plain(obj, kind)
    )
    refs.find_anchor = lambda text, kind: (
        plain(feature, "feature") if (text, kind) == (anchor, "feature") else None
    )
    made = run(nx, "nx_create_anchor", object=ref)
    assert made.ok, made
    assert made.data["anchor"] == anchor and made.data["object"]["id"] == ref
    back = run(nx, "nx_resolve_reference", anchor=made.data["anchor"])
    assert back.ok and back.data["object"]["id"] == ref, back
    refs.row = lambda obj, kind, **k: _no_anchor(plain(obj, kind))
    bare = run(nx, "nx_create_anchor", object=ref)
    assert bare.error["code"] == "NX_UNSUPPORTED_OPTION" and bare.error["field"] == "/object"


def test_a_drawing_view_owner_explains_where_model_edges_come_from():
    from tests.unit.drawing.fakes import drawing_nx

    nx = drawing_nx()
    nx.block(10, 10, 10)
    view = nx.make_view(nx.make_sheet("SHT1"), "Top@1")
    ref = nx.ref_of(view, "drawing_view")
    bad = run(nx, "nx_find_geometry", owner=ref, kind="edge")
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.error["field"] == "/owner", bad
    assert "model body" in bad.error["hint"] and "nx_list_view_edges" in bad.error["hint"]


def test_parallel_keeps_line_edges_along_an_axis_or_a_plane_and_reruns():
    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    vertical = run(nx, "nx_find_geometry", owner=body.ref, kind="edge", parallel="z", limit=20)
    assert vertical.ok, vertical
    rows = vertical.data["items"]
    assert vertical.page["total"] == 4 and all(r["type"] == "line" for r in rows)
    for row in rows:
        edge = next(e for e in body.edges if e.ref == row["id"])
        a, b = edge.GetVertices()
        assert (a.X, a.Y) == (b.X, b.Y) and a.Z != b.Z
    sel = json.loads(vertical.data["selector"])
    assert sel["parallel"] == "z" and sel["geometry_type"] == "line"
    flat = run(nx, "nx_find_geometry", owner=body.ref, kind="edge", parallel="XY", limit=20)
    assert flat.ok and flat.page["total"] == 8, flat
    along_x = run(nx, "nx_find_geometry", owner=body.ref, kind="edge", parallel="x",
                  order="highest", axis="z", limit=1)  # fmt: skip
    assert along_x.page["total"] == 4 and along_x.data["items"][0]["center"][2] == 10
    swapped = run(nx, "nx_find_geometry", owner=body.ref, kind="edge", parallel="zx", limit=20)
    assert swapped.ok and swapped.page["total"] == 8  # plane names in any order
    again = run(nx, "nx_resolve_reference", selector=along_x.data["selector"], tie_tolerance=1e-9)
    assert again.error["code"] == "NX_AMBIGUOUS_REFERENCE"  # two top edges along x tie
    face = run(nx, "nx_find_geometry", kind="face", parallel="z")
    assert face.error["field"] == "/parallel" and "normal" in face.error["hint"]
    circle = run(nx, "nx_find_geometry", kind="edge", geometry_type="circle", parallel="z")
    assert circle.error["field"] == "/parallel"
    word = run(nx, "nx_find_geometry", kind="edge", parallel="up")
    assert word.error["category"] == "invalid_argument"


def test_face_owner_gives_the_faces_edges_and_a_handle_selector():
    # As nx_list_objects {kind: edge, owner: face}: the face's own edges, re-resolvable.
    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    top = _top(nx, body)
    rim = [e for e in body.edges if all(v.Z == 10 for v in e.GetVertices())]
    top.GetEdges = lambda: rim
    ok = run(nx, "nx_find_geometry", owner=top.ref, kind="edge", parallel="x", order="highest",
             axis="y")  # fmt: skip
    assert ok.ok and ok.page["total"] == 2, ok
    assert {i["id"] for i in ok.data["items"]} <= {e.ref for e in rim}
    selector = json.loads(ok.data["selector"])
    assert selector["owner"] == f"@work.prt:face=RM{top.Tag} R{top.Tag}"
    assert selector["owner_kind"] == "face"
    again = run(nx, "nx_resolve_reference", selector=ok.data["selector"])
    assert again.data["object"]["id"] == ok.data["items"][0]["id"], again
    faces = run(nx, "nx_find_geometry", owner=top.ref, kind="face")
    assert faces.error["code"] == "NX_WRONG_KIND" and faces.error["field"] == "/owner"
    assert 'kind "edge"' in faces.error["hint"]
    holes = run(nx, "nx_find_holes", owner=top.ref)
    assert holes.error["code"] == "NX_WRONG_KIND"


def test_resolve_errors_say_where_a_ref_or_another_part_goes():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    top = _top(nx, body)
    in_selector = run(nx, "nx_resolve_reference", selector=top.ref)
    assert in_selector.error["code"] == "NX_INVALID_ARGUMENT"
    assert in_selector.error["hint"] == f'A ref goes in anchor: {{"anchor": "{top.ref}"}}'
    by_hand = run(nx, "nx_resolve_reference", selector={"kind": "face", "normal": "+z"})
    assert "run nx_find_geometry" in by_hand.error["hint"]
    other = run(nx, "nx_resolve_reference", anchor="@smoke/a/other.prt:face=RM1 R1")
    assert other.error["code"] == "NX_WRONG_OWNER" and "part='other.prt'" in other.error["hint"]
