"""nx_list_objects (first slice) on the shared fake kit, through the real kernel."""

from tests.kit import fake_nx, run


def test_faces_of_a_body_with_type_filter_and_page():
    nx = fake_nx()
    body = nx.block(40, 20, 10)
    ok = run(nx, "nx_list_objects", kind="face", owner=body.ref, type="plane", limit=4)
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    assert ok.page == {"offset": 0, "limit": 4, "returned": 4, "total": 6, "next_offset": 4}
    top = next(r for r in ok.data["items"] if r["details"]["normal"] == [0, 0, 1])
    assert top["details"]["point"][2] == 10 and top["type"] == "plane" and top["kind"] == "face"
    rest = run(nx, "nx_list_objects", kind="face", owner=body.ref, offset=4, limit=4)
    assert rest.page["returned"] == 2 and rest.page["next_offset"] is None
    none = run(nx, "nx_list_objects", kind="face", owner=body.ref, type="cylinder")
    assert none.page["total"] == 0


def test_bodies_edges_datums_and_sketches():
    nx = fake_nx()
    body = nx.block(1, 2, 3)
    nx.datum_plane(origin=(0, 0, 5), normal=(0, 0, 2))
    nx.sketch()
    bodies = run(nx, "nx_list_objects", kind="body")
    assert bodies.data["items"][0]["id"] == body.ref and bodies.data["items"][0]["type"] == "solid"
    edges = run(nx, "nx_list_objects", kind="edge", owner=body.ref)
    assert edges.page["total"] == 12 and {r["type"] for r in edges.data["items"]} == {"line"}
    datums = run(nx, "nx_list_objects", kind="datum")
    row = datums.data["items"][0]
    assert row["type"] == "plane" and row["details"]["normal"] == [0, 0, 1]
    sketches = run(nx, "nx_list_objects", kind="sketch")
    assert sketches.page["total"] == 1 and sketches.data["items"][0]["details"]["active"] is False


def test_bad_owner_and_unknown_kind():
    nx = fake_nx()
    nx.block(1, 1, 1)
    plane = nx.datum_plane()
    bad = run(nx, "nx_list_objects", kind="face", owner=plane.ref)
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.mutation_outcome == "not_applicable"
    unknown = run(nx, "nx_list_objects", kind="widget")
    assert unknown.error["field"] == "/kind"
    big = run(nx, "nx_list_objects", kind="body", limit=500)
    assert big.error["field"] == "/limit"


def test_every_registered_lister_is_listable_and_every_kind_has_a_lister():
    """nx_list_objects reaches the listers of every domain (drawing: sheets, views,
    dimensions, annotations, tables). A domain that registers a new @lister adds its
    kind to ListKind in domains/inspection/inventory.py."""
    import typing

    from nx_mcp.core import catalogue
    from nx_mcp.domains.inspection.inventory import ListKind

    catalogue.load()
    listable = set(typing.get_args(ListKind))
    registered = set(catalogue.hooks("lister"))
    assert registered - listable == set(), "add these kinds to ListKind"
    assert listable - registered == set(), "these ListKind values have no lister"


def test_drawing_sheets_and_views_are_listed_through_their_hooks():
    from tests.unit.drawing.fakes import drawing_nx

    nx = drawing_nx()
    sheet = nx.make_sheet("SHT1")
    nx.make_view(sheet, "Top@1")
    sheets = run(nx, "nx_list_objects", kind="drawing_sheet")
    assert sheets.ok, sheets
    assert [r["kind"] for r in sheets.data["items"]] == ["drawing_sheet"]
    views = run(nx, "nx_list_objects", kind="drawing_view")
    assert views.ok and [r["name"] for r in views.data["items"]] == ["Top@1"], views
    with_views = run(nx, "nx_list_objects", kind="drawing_sheet", include="views")
    assert [v["name"] for v in with_views.data["items"][0]["details"]["views"]] == ["Top@1"]


def _adjacent(body):
    """Give the fake block NX's face<->edge adjacency (an edge lies in a face's plane)."""

    def on(face, edge):
        n, o = face.normal, face.origin
        return all(
            abs(sum(n[i] * (getattr(v, "XYZ"[i]) - o[i]) for i in range(3))) < 1e-9
            for v in edge.GetVertices()
        )

    for face in body.faces:
        face.GetEdges = lambda f=face: [e for e in body.edges if on(f, e)]
    for edge in body.edges:
        edge.GetFaces = lambda e=edge: [f for f in body.faces if on(f, e)]


def test_face_and_edge_adjacency():
    nx = fake_nx()
    body = nx.block(40, 20, 10)
    _adjacent(body)
    faces = run(nx, "nx_list_objects", kind="face", owner=body.ref, include="adjacency")
    assert faces.ok, faces
    assert all(len(r["details"]["edges"]) == 4 for r in faces.data["items"])
    assert faces.data["items"][0]["details"]["normal"]  # the summary is kept
    edges = run(nx, "nx_list_objects", kind="edge", owner=body.ref, include="adjacency", limit=1)
    assert len(edges.data["items"][0]["details"]["faces"]) == 2
    bad = run(nx, "nx_list_objects", kind="body", include="adjacency")
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/include"


def test_feature_rows_carry_timestamp_and_errors_and_expressions_are_listed():
    from tests.kit.objects import Feature

    nx = fake_nx()
    feature = Feature("EXTRUDE(1)")
    feature.Suppressed, feature.Timestamp = False, 3
    feature.GetFeatureErrorMessages = lambda: ["Section is open"]
    nx.part.Features.append(nx._own(feature, "feature"))
    nx.expression("p2", "20", 20.0)
    nx.expression("p1", "10", 10.0)
    features = run(nx, "nx_list_objects", kind="feature")
    assert features.ok, features
    details = features.data["items"][0]["details"]
    assert details == {"suppressed": False, "timestamp": 3, "errors": ["Section is open"]}
    exprs = run(nx, "nx_list_objects", kind="expression", name_contains="p")
    assert exprs.ok, exprs
    assert [r["name"] for r in exprs.data["items"]] == ["p1", "p2"]
    assert {"formula", "value", "units"} <= set(exprs.data["items"][0]["details"])
