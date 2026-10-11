"""Assembly occurrences in inspection: bodies found by rule, a component as list owner,
and the note that positions are assembled while an explosion is shown."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Object, Part
from tests.unit.inspection.fakes import assembly, inspection_nx


def _assembly(nx):
    return assembly(nx, ("A", [0, 0, 0, 10, 10, 10]), ("B", [50, 0, 0, 60, 10, 20]))


def test_find_geometry_ranks_bodies_with_bounds_volume_and_owner():
    nx = inspection_nx()
    a, b = _assembly(nx)
    ok = run(nx, "nx_find_geometry", kind="body", order="highest", axis="x")
    assert ok.ok, ok
    first, second = ok.data["items"]
    assert first["kind"] == "body" and first["type"] == "solid"
    assert first["min"] == [50, 0, 0] and first["max"] == [60, 10, 20]
    assert first["center"] == [55, 5, 10] and first["volume"] == 2000
    assert first["owner"] == nx.ref_of(b, "component")
    assert second["owner"] == nx.ref_of(a, "component") and second["volume"] == 1000
    selector = json.loads(ok.data["selector"])
    assert selector["kind"] == "body" and selector["order"] == "highest"
    again = run(nx, "nx_resolve_reference", selector=ok.data["selector"])
    assert again.data["object"]["id"] == first["id"] and again.data["rank"] == 55
    near = run(nx, "nx_find_geometry", kind="body", near=[0, 0, 30])
    assert near.data["items"][0]["owner"] == nx.ref_of(a, "component")
    owned = run(nx, "nx_find_geometry", kind="body", owner=nx.ref_of(b, "component"))
    assert owned.page["total"] == 1 and owned.data["items"][0]["owner"] == first["owner"]


def test_part_bodies_have_no_owner_and_sheets_no_volume():
    nx = inspection_nx()
    solid = nx.block(10, 10, 10)
    sheet = nx.block(5, 5, 5, origin=(20, 0, 0))
    sheet.IsSolidBody = False
    ok = run(nx, "nx_find_geometry", kind="body", order="lowest", axis="x")
    rows = {r["id"]: r for r in ok.data["items"]}
    assert rows[solid.ref]["volume"] == 1000 and "owner" not in rows[solid.ref]
    assert rows[sheet.ref]["type"] == "sheet" and "volume" not in rows[sheet.ref]


def test_body_kind_refuses_face_and_edge_filters():
    nx = inspection_nx()
    nx.block(1, 1, 1)
    for extra in ({"geometry_type": "plane"}, {"normal": "+z"}, {"radius": 2.0}, {"role": "bore"}):
        bad = run(nx, "nx_find_geometry", kind="body", **extra)
        assert bad.error["category"] == "invalid_argument", (extra, bad)


def test_component_owner_lists_occurrence_bodies_faces_and_edges():
    nx = inspection_nx()
    a, b = _assembly(nx)
    ref = nx.ref_of(b, "component")
    occurrence = next(iter(b.occurrences.values()))
    for face in occurrence.faces:  # the occurrence's one planar face and one line edge
        nx.remember(face).sense = 1
        nx.face_data[int(face.Tag)] = ("plane", [0, 0, 20], [0, 0, 1])
    for edge in occurrence.edges:
        edge.SolidEdgeType = nx.nx.Edge.EdgeType.Linear
    bodies = run(nx, "nx_list_objects", kind="body", owner=ref)
    assert bodies.ok, bodies
    assert [r["id"] for r in bodies.data["items"]] == [nx.ref_of(occurrence, "body")]
    assert bodies.data["items"][0]["type"] == "solid"
    faces = run(nx, "nx_list_objects", kind="face", owner=ref)
    assert faces.page["total"] == len(occurrence.faces)
    edges = run(nx, "nx_list_objects", kind="edge", owner=ref)
    assert edges.page["total"] == len(occurrence.edges)
    wrong = run(nx, "nx_list_objects", kind="expression", owner=ref)
    assert wrong.error["code"] == "NX_WRONG_KIND" and wrong.error["field"] == "/owner"
    b.IsSuppressed = True
    gone = run(nx, "nx_list_objects", kind="body", owner=ref)
    assert gone.page["total"] == 0


def test_component_owner_lists_its_part_features_with_a_note():
    nx = inspection_nx()
    a, _b = _assembly(nx)
    made = []
    for stamp, kind in ((1, "SKETCH"), (2, "EXTRUDE")):
        f = Object(f"{kind}({stamp})")
        f.FeatureType, f.Timestamp, f.Suppressed, f.IsInternal = kind, stamp, False, False
        f.OwningPart = a.Prototype
        nx.remember(f)
        made.append(f)
    hidden = Object("SKETCH(1:1B)")
    hidden.FeatureType, hidden.Timestamp, hidden.IsInternal = "DATUM_CSYS", 1, True
    a.Prototype.Features, a.Prototype.Leaf = [made[1], hidden, made[0]], "box"
    ok = run(nx, "nx_list_objects", kind="feature", owner=nx.ref_of(a, "component"))
    assert ok.ok, ok
    assert [r["type"] for r in ok.data["items"]] == ["sketch", "extrude"]
    assert ok.data["items"][1]["details"] == {"suppressed": False, "timestamp": 2}
    assert ok.warnings[0]["code"] == "NX_NOTE" and "box" in ok.warnings[0]["message"]


def _explode(nx, name="EXPLODED"):
    explosion = Object(name)
    nx.part.ComponentAssembly.Explosions = [explosion]
    shown = {"tag": int(explosion.Tag)}
    nx.uf.Assem = NS(AskViewExplosion=lambda tag: shown["tag"])
    nx.part.ModelingViews.WorkView = Object("Trimetric")
    return shown


def test_measurements_note_assembled_positions_while_an_explosion_is_shown():
    nx = inspection_nx()
    a, b = _assembly(nx)
    shown = _explode(nx, "EX1")
    refs = {"first": nx.ref_of(a, "component"), "second": nx.ref_of(b, "component")}
    calls = [
        ("nx_measure_distance", refs),
        ("nx_measure_bounds", {"bodies": [refs["second"]]}),
        ("nx_find_geometry", {"kind": "body"}),
        ("nx_check_interference", {"objects": [refs["first"], refs["second"]]}),
    ]
    for tool, args in calls:
        ok = run(nx, tool, **args)
        assert ok.ok, (tool, ok)
        notes = [w for w in ok.warnings if w["code"] == "NX_NOTE"]
        assert len(notes) == 1 and "EX1" in notes[0]["message"], (tool, ok.warnings)
        assert "assembled (unexploded)" in notes[0]["message"]
    shown["tag"] = 0
    plain = run(nx, "nx_measure_distance", **refs)
    assert plain.ok and not plain.warnings, plain


def test_a_read_of_a_background_part_gets_no_explosion_note():
    """Gate 4s: a measurement bound to a part that is not displayed measures that part,
    not the exploded assembly on screen (NX's work and display part): no note."""
    nx = inspection_nx()
    _assembly(nx)
    _explode(nx, "EX1")
    parts = nx.session.Parts
    shown = parts.Display
    plate = Part(nx.session, Path(services(nx).root) / "plate.prt")
    parts.Work = parts.Display = shown
    body = nx.block(10, 10, 10)
    nx.part.Bodies.remove(body)
    plate.Bodies.append(body)
    body.OwningPart = plate
    ok = run(nx, "nx_measure_bounds", bodies=[nx.ref_of(body, "body")])
    assert ok.ok, ok
    assert not [w for w in ok.warnings if "EX1" in w["message"]], ok.warnings
    assert parts.Work is shown and parts.Display is shown and nx.session.calls == []
