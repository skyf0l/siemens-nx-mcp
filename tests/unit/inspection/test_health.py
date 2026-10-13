"""nx_check_model and nx_get_model_summary on the fake kit."""

import pytest

from tests.kit import run
from tests.kit.objects import Feature
from tests.unit.inspection.fakes import assembly, inspection_nx


def _feature(nx, name, errors=(), warnings=(), suppressed=False):
    f = Feature(name)
    f.OwningPart, f.IsOccurrence, f.Suppressed = nx.part, False, suppressed
    f.GetFeatureErrorMessages = lambda: list(errors)
    f.GetFeatureWarningMessages = lambda: list(warnings)
    nx.part.Features.append(f)
    nx.remember(f)
    return f


def test_healthy_part_and_feature_diagnostics():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    _feature(nx, "EXTRUDE(1)")
    ok = run(nx, "nx_check_model")
    assert ok.ok and ok.data["healthy"] is True and ok.data["issues"] == [], ok
    assert ok.data["features_checked"] == 1 and ok.data["bodies_checked"] == 1
    _feature(nx, "BLEND(2)", errors=["Blend failed"])
    _feature(nx, "CHAMFER(3)", warnings=["Overlap"], suppressed=True)
    bad = run(nx, "nx_check_model", limit=2)
    assert bad.data["healthy"] is False and bad.data["errors"] == 1 and bad.data["warnings"] == 1
    assert bad.page["total"] == 3 and bad.page["next_offset"] == 2
    first = bad.data["issues"][0]
    assert first["kind"] == "feature_diagnostic" and first["message"] == "Blend failed"
    assert first["object"]["kind"] == "feature" and first["part"] == "work.prt"


def test_body_consistency_faults_are_errors():
    nx = inspection_nx()
    body = nx.block(1, 1, 1)
    face = body.faces[0]
    nx.inconsistent[int(body.Tag)] = (1, [640001], [face.Tag])
    ok = run(nx, "nx_check_model")
    issue = ok.data["issues"][0]
    assert issue["kind"] == "body_consistency" and ok.data["healthy"] is False
    assert issue["faults"][0]["code"] == 640001 and issue["faults"][0]["message"] == "fault 640001"
    assert issue["faults"][0]["object"]["id"] == face.ref


def test_assembly_scope_reports_unloaded_and_suppressed_components():
    nx = inspection_nx()
    a, b, c = assembly(
        nx, ("A", [0, 0, 0, 1, 1, 1]), ("B", [2, 0, 0, 3, 1, 1]), ("C", [4, 0, 0, 5, 1, 1])
    )
    a.Prototype.Features = []
    a.Prototype.Tag = 9001
    b.IsSuppressed = True
    c.Prototype = None
    ok = run(nx, "nx_check_model", scope="assembly")
    kinds = sorted(i["kind"] for i in ok.data["issues"])
    assert kinds == ["suppressed_component", "unloaded_component"]
    assert ok.data["parts_checked"] == 2 and ok.data["warnings"] == 1


def _constraints(nx, *rows):
    """A positioner on the work part with constraints ``(component, status, suppressed)``;
    statuses are NX 2406's SolverStatus values."""
    from types import SimpleNamespace as NS

    from tests.kit.objects import Object

    status = NS(Solved=9, CannotSolve=10, NotSolved=8, Suppressed=1, OverConstrained=3)
    nx.nx.Positioning = NS(Constraint=NS(
        SolverStatus=status, Type=NS(Concentric=7, Touch=2), Alignment=NS(CoAlign=11),
    ))  # fmt: skip
    made = []
    for component, state, suppressed in rows:
        c = Object("CONCENTRIC")
        c.OwningPart, c.Suppressed = nx.part, suppressed
        c.ConstraintType, c.ConstraintAlignment = 7, 11
        c.GetConstraintStatus = lambda state=state: getattr(status, state)
        c.GetReferences = lambda comp=component: [NS(GetMovableObject=lambda: comp)]
        nx.remember(c)
        made.append(c)
    nx.part.ComponentAssembly.Positioner = NS(Constraints=made)
    return made


def test_unsolved_assembly_constraints_are_errors_or_warnings():
    nx = inspection_nx()
    a, b = assembly(nx, ("A", [0, 0, 0, 1, 1, 1]), ("B", [2, 0, 0, 3, 1, 1]))
    broken, _ok, _held, pending = _constraints(
        nx, (a, "CannotSolve", False), (b, "Solved", False), (a, "CannotSolve", True),
        (b, "NotSolved", False),
    )  # fmt: skip
    for scope in ("assembly", "part"):
        ok = run(nx, "nx_check_model", scope=scope)
        assert ok.ok and ok.data["healthy"] is False, ok
        found = [i for i in ok.data["issues"] if i["kind"] == "assembly_constraint"]
        assert [(i["severity"], i["status"]) for i in found] == [
            ("error", "CannotSolve"), ("warning", "NotSolved")]  # fmt: skip
        assert found[0]["object"]["id"] == nx.ref_of(broken, "assembly_constraint")
        assert found[0]["components"] == [nx.ref_of(a, "component")]
        assert "concentric constraint is CannotSolve" in found[0]["message"]
        assert "nx_edit_assembly_constraint" in found[0]["message"]
        assert found[1]["object"]["id"] == nx.ref_of(pending, "assembly_constraint")
    summary = run(nx, "nx_get_model_summary")
    assert summary.data["health"] == {"healthy": False, "errors": 1, "warnings": 1}


def test_summary_overview_and_sections():
    nx = inspection_nx()
    nx.block(40, 20, 10)
    _feature(nx, "EXTRUDE(1)")
    nx.expression("p1", "10", 10.0)
    over = run(nx, "nx_get_model_summary")
    assert over.ok, over
    data = over.data
    assert data["section"] == "overview" and data["counts"]["bodies"] == 1
    assert data["bounds"]["max"] == [40, 20, 10] and data["health"]["healthy"] is True
    feats = run(nx, "nx_get_model_summary", section="features")
    assert feats.data["items"][0]["type"] == "extrude" and feats.page["total"] == 1
    exprs = run(nx, "nx_get_model_summary", section="expressions")
    assert exprs.data["items"][0]["formula"] == "10" and exprs.data["items"][0]["value"] == 10.0
    comps = run(nx, "nx_get_model_summary", section="components")
    assert comps.page["total"] == 0
    bad = run(nx, "nx_get_model_summary", section="features", limit=0)
    assert bad.error["field"] == "/limit"


def _void_body(nx):
    """A 40 mm cube with a sealed 10 mm cubic void: two shells (no shared edges)."""
    outer = nx.block(40, 40, 40)
    inner = nx.block(10, 10, 10, origin=(10, 10, 10))
    nx.part.Bodies.remove(inner)
    for face in inner.faces:  # a void's faces point into the void
        face.normal = [-c for c in face.normal]
    outer.faces += inner.faces
    outer.edges += inner.edges
    nx.facet_boxes = {}
    for body, box in ((outer, [0, 0, 0, 40, 40, 40]), (inner, [10, 10, 10, 20, 20, 20])):
        for face in body.faces[:6]:
            axis = next(i for i in range(3) if face.normal[i])
            flat = [float(v) for v in box]
            flat[axis] = flat[axis + 3] = float(face.origin[axis])
            nx.facet_boxes[int(face.Tag)] = flat
    return outer


def test_a_sealed_void_is_a_warning_with_its_volume_and_box():
    nx = inspection_nx()
    body = _void_body(nx)
    curves = len(nx.part.Curves)
    ok = run(nx, "nx_check_model")
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    assert ok.data["healthy"] is False and ok.data["warnings"] == 1 and ok.data["errors"] == 0
    issue = ok.data["issues"][0]
    assert issue["kind"] == "internal_void" and issue["severity"] == "warning"
    assert issue["object"]["id"] == body.ref and issue["shells"] == 2
    (void,) = issue["voids"]
    assert void["faces"] == 6 and void["volume"] == pytest.approx(1000.0)
    assert void["min"] == [10, 10, 10] and void["max"] == [20, 20, 20]
    params = nx.uf.Facet.models[0][1]
    assert params.SurfaceAngularTolerance == 0.05 and len(nx.part.Curves) == curves
    summary = run(nx, "nx_get_model_summary").data
    assert summary["health"] == {"healthy": False, "errors": 0, "warnings": 1}


def test_a_void_without_facets_keeps_its_box_and_no_volume():
    nx = inspection_nx()
    _void_body(nx)
    nx.facet_error = RuntimeError("no licence")
    issue = run(nx, "nx_check_model").data["issues"][0]
    assert issue["voids"][0]["volume"] is None and issue["voids"][0]["max"] == [20, 20, 20]


def test_one_shell_bodies_have_no_void_issue():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    assert run(nx, "nx_check_model").data["issues"] == []


def test_summary_features_follow_the_timeline_without_internal_features():
    nx = inspection_nx()
    sketch = _feature(nx, "SKETCH(1)")
    csys = _feature(nx, "DATUM_CSYS(0)")
    internal = _feature(nx, "SKETCH(1:1B)")
    extrude = _feature(nx, "EXTRUDE(2)")
    sketch.FeatureType, csys.FeatureType, internal.FeatureType = (
        "SKETCH",
        "DATUM_CSYS",
        "DATUM_CSYS",
    )
    internal.IsInternal = True
    for f, stamp in ((csys, 0), (sketch, 1), (internal, 1), (extrude, 2)):
        f.Timestamp = stamp
    feats = run(nx, "nx_get_model_summary", section="features")
    assert feats.ok, feats
    types = [i["type"] for i in feats.data["items"]]
    assert types == ["datum_csys", "sketch", "extrude"] and feats.page["total"] == 3
    assert "SKETCH(1:1B)" not in [i["name"] for i in feats.data["items"]]
    over = run(nx, "nx_get_model_summary")
    assert over.data["counts"]["features"] == 3
    health = run(nx, "nx_check_model")
    assert health.data["features_checked"] == 4  # internal features are still checked


def _examine(nx, failed: dict):
    """Examine Geometry whose body face intersection check fails ``failed[body tag]``."""
    from types import SimpleNamespace as NS

    seen: list = []
    nx.nx.GeometricAnalysis = NS(ExamineGeometry=NS(Check=NS(BodyFaceIntersections="faces")))
    nx.part.AnalysisManager = NS(CreateExamineGeometryObject=lambda: NS(
        ClearAllChecks=lambda: None, SetCheck=lambda check: None,
        ObjectsToExamine=NS(Add=seen.extend), Examine=lambda: None,
        GetFailedObjects=lambda check: failed.get(int(seen[-1].Tag), []),
        Destroy=lambda: None,
    ))  # fmt: skip
    return seen


def test_a_self_overlapping_solid_is_an_error_naming_its_faces():
    """Bug-fix round: a tube OD 8 on a pitch-5 helix passed UF consistency as healthy."""
    nx = inspection_nx()
    good, bad = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(20, 0, 0))
    seen = _examine(nx, {int(bad.Tag): bad.faces[:3] + bad.faces[:2]})  # NX lists pairs
    ok = run(nx, "nx_check_model")
    assert ok.ok and ok.data["healthy"] is False and ok.data["errors"] == 1, ok
    (issue,) = ok.data["issues"]
    assert issue["kind"] == "self_intersection" and issue["object"]["id"] == bad.ref
    assert [f["id"] for f in issue["faces"]] == [f.ref for f in bad.faces[:3]]
    assert issue["message"].startswith("3 faces cut other faces") and seen == [good, bad]
    summary = run(nx, "nx_get_model_summary").data
    assert summary["health"] == {"healthy": False, "errors": 1, "warnings": 0}


def test_self_overlap_is_examined_on_a_bounded_number_of_solids(monkeypatch):
    from nx_mcp.domains.inspection import health

    monkeypatch.setattr(health, "CLASH_BODIES", 2)
    nx = inspection_nx()
    for i in range(3):
        nx.block(1, 1, 1, origin=(3 * i, 0, 0))
    seen = _examine(nx, {})
    ok = run(nx, "nx_check_model")
    assert ok.data["healthy"] is True and len(seen) == 2, ok
    (note,) = ok.data["issues"]
    assert note["severity"] == "info" and note["kind"] == "self_intersection"
    assert "1 solid bodies past the first 2" in note["message"]
