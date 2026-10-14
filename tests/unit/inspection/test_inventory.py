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
    assert sheets.data["items"][0]["details"]["active"] in (True, False)  # displayed: known
    nx.session.Parts.Display = None  # a part in the background: NX names no current sheet
    hidden = run(nx, "nx_list_objects", kind="drawing_sheet")
    assert hidden.ok and hidden.data["items"][0]["details"]["active"] is None, hidden


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


def test_unknown_type_names_are_refused_with_the_valid_names():
    nx = fake_nx()
    body = nx.block(1, 1, 1)
    nx.sketch()
    for kind, wrong in (("face", "hexagon"), ("face", "freeform"), ("edge", "spline")):
        bad = run(nx, "nx_list_objects", kind=kind, owner=body.ref, type=wrong)
        assert bad.error["code"] == "NX_INVALID_ARGUMENT", bad
        assert bad.error["field"] == "/type" and bad.mutation_outcome == "not_applicable"
    message = run(nx, "nx_list_objects", kind="face", type="hexagon").error["message"]
    assert "cylinder" in message and "cone" in message and "plane" in message
    untyped = run(nx, "nx_list_objects", kind="sketch", type="planar")
    assert untyped.error["field"] == "/type"
    open_ended = run(nx, "nx_list_objects", kind="feature", type="no_such_feature")
    assert open_ended.ok and open_ended.page["total"] == 0


def test_known_type_names_follow_the_listers():
    import typing

    from nx_mcp.domains.inspection.inventory import TYPES, ListKind
    from nx_mcp.domains.modeling import hooks

    assert set(TYPES) <= set(typing.get_args(ListKind))
    assert set(TYPES["face"]) == {*hooks.FACE_TYPES.values(), "other"}
    assert set(TYPES["edge"]) == {*hooks.EDGE_TYPES.values(), "other"}


def test_type_names_are_matched_without_case_kind_word_plural_or_adjective():
    nx = fake_nx()
    body = nx.block(40, 20, 10)
    for kind, spelled in (
        ("face", "Plane"), ("face", "face plane"), ("face", "planar"), ("face", "planes"),
        ("face", "Face Planes"), ("edge", "straight"), ("edge", "edge line"),
    ):  # fmt: skip
        ok = run(nx, "nx_list_objects", kind=kind, owner=body.ref, type=spelled)
        assert ok.ok and ok.page["total"] in (6, 12), (spelled, ok)
    arcs = run(nx, "nx_list_objects", kind="edge", owner=body.ref, type="arc")
    assert arcs.ok and arcs.page["total"] == 0
    datums = run(nx, "nx_list_objects", kind="datum", type="datum plane")
    assert datums.ok, datums


def test_feature_types_with_spaces_can_be_filtered():
    from tests.kit.objects import Feature

    nx = fake_nx()
    hole = Feature("SIMPLE HOLE(3)")
    hole.FeatureType, hole.Suppressed = "SIMPLE HOLE", False
    hole.GetFeatureErrorMessages = lambda: []
    nx.part.Features.append(nx._own(hole, "feature"))
    for spelled in ("simple hole", "SIMPLE_HOLE", "Simple-Hole"):
        ok = run(nx, "nx_list_objects", kind="feature", type=spelled)
        assert ok.ok and ok.page["total"] == 1, (spelled, ok)


def test_owner_takes_an_anchor_and_explains_names():
    from tests.kit.kernel import services
    from tests.unit.inspection.fakes import inspection_nx

    nx = inspection_nx()
    body = nx.block(40, 20, 10)
    face = body.faces[0]
    anchor = run(nx, "nx_create_anchor", object=face.ref).data["anchor"]
    edges = run(nx, "nx_list_objects", kind="edge", owner=anchor)
    assert edges.ok and edges.page["total"] == 4, edges
    body_anchor = run(nx, "nx_create_anchor", object=body.ref).data["anchor"]
    faces = run(nx, "nx_list_objects", kind="face", owner=body_anchor)
    assert faces.ok and faces.page["total"] == 6, faces
    named = run(nx, "nx_list_objects", kind="face", owner="SIMPLE HOLE(3)")
    assert named.error["code"] == "NX_INVALID_ARGUMENT" and named.error["field"] == "/owner"
    assert "not a reference" in named.error["message"]
    services(nx).references.find_anchor = lambda text, kind: None
    stale = run(nx, "nx_list_objects", kind="face", owner="@work.prt:NOTHING(9)")
    assert stale.error["code"] == "NX_STALE_REFERENCE" and stale.error["field"] == "/owner"


def test_compact_rows_give_id_name_and_box_only():
    from tests.unit.inspection.fakes import inspection_nx

    nx = inspection_nx()
    blocks = [nx.block(5, 5, 5, origin=(10 * i, 0, 0)) for i in range(3)]
    ok = run(nx, "nx_list_objects", kind="body", compact=True)
    assert ok.ok and ok.page["total"] == 3, ok
    assert ok.data["items"][1] == {
        "id": blocks[1].ref, "name": ok.data["items"][1]["name"],
        "min": [10, 0, 0], "max": [15, 5, 5],
    }  # fmt: skip
    faces = run(nx, "nx_list_objects", kind="face", owner=blocks[0].ref, compact=True)
    assert set(faces.data["items"][0]) == {"id", "name", "min", "max"}
    bad = run(nx, "nx_list_objects", kind="feature", compact=True)
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/compact"
    mixed = run(nx, "nx_list_objects", kind="face", compact=True, include="adjacency")
    assert mixed.error["field"] == "/include"


def test_pattern_copies_carry_their_pattern_and_instance():
    """NX: a copy's feature sits in an instance feature (or directly) contained in the
    pattern feature, in instance order; the seed and plain bodies have no instance."""
    from tests.kit.objects import Feature

    nx = fake_nx()
    seed = nx.block(5, 5, 5)
    copies = [nx.block(5, 5, 5, origin=(10 * i, 0, 0)) for i in (1, 2, 3)]
    pattern = nx._own(Feature("Pattern Feature(4)"), "feature")
    pattern.FeatureType = "Pattern Feature"
    instances = []
    for i, body in enumerate(copies, start=1):
        instance = Feature(f"Pattern Feature(4:{i}A)")
        instance.ContainerFeature = pattern
        output = Feature(f"Pattern Feature(4:{i}A:1A)")
        output.ContainerFeature = instance
        body.GetFeatures = lambda o=output: [o]
        instances.append(instance)
    pattern.GetImmediateContainedFeatures = lambda: list(instances)
    plain = Feature("BLOCK(1)")
    plain.ContainerFeature = None
    seed.GetFeatures = lambda: [plain]
    nx.part.Features.append(pattern)
    ok = run(nx, "nx_list_objects", kind="body")
    assert ok.ok, ok
    rows = {r["id"]: r for r in ok.data["items"]}
    assert "details" not in rows[seed.ref]
    for i, body in enumerate(copies, start=1):
        assert rows[body.ref]["details"] == {"pattern": pattern.ref, "instance": i}
