"""nx_check_model and nx_get_model_summary on the fake kit."""

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
