"""nx_check_face_continuity and nx_create_surface_analysis on the fake kit: shared edges,
sampled verdicts, NX's own verdict, display analyses and refusals."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import surface
from tests.kit import run
from tests.unit.inspection.fakes import inspection_nx


class Selection(list):
    def Add(self, objs):  # noqa: N802 - NXOpen name
        self.extend(objs)
        return True


class Builder:
    """A GeometricAnalysis builder: records settings, commits one analysis object."""

    def __init__(self, nx, kind, made, results=(True, True, False, False)):
        self.kind, self.made, self.results = kind, made, list(results)
        self.MultiFace = self.Faces = Selection()
        self.SelectedCurves = NS(ReplaceRules=lambda rules, _f: setattr(self, "rules", rules))
        self.CombOptions = NS(Density=50)
        self.committed = []
        self.destroyed = False
        self.started = False

    def StartRadiusAnalysis(self):  # noqa: N802
        self.started = True

    def Commit(self):  # noqa: N802
        obj = NS(kind=self.kind, Name="", SetName=lambda n: setattr(obj, "Name", n))
        self.committed.append(obj)
        self.made.append(self)
        return None if self.kind == "comb" else obj

    def GetCommittedObjects(self):  # noqa: N802
        return list(self.committed)

    def GetGxResult(self, i):  # noqa: N802
        return self.results[i] if self.committed else False

    def Destroy(self):  # noqa: N802
        self.destroyed = True


def enums(*names):
    return NS(**{n: n for n in names})


def with_analysis(nx, results=(True, True, False, False)):
    made: list = []

    def factory(kind):
        return lambda _obj: Builder(nx, kind, made, results)

    objects = NS(
        CreateSurfaceContinuityAnalysisBuilder=factory("continuity"),
        CreateCurveCurvatureAnalysisBuilder=factory("comb"),
        AnalysisObjectsEx=NS(
            CreateReflectionAnalysisBuilder1=factory("reflection"),
            CreateRadiusAnalysisBuilder1=factory("radius"),
        ),
        __iter__=None,
    )
    nx.part.AnalysisManager.AnalysisObjects = objects
    nx.nx.GeometricAnalysis = NS(
        SurfaceContinuityAnalysisBuilder=NS(Types=enums("MultiFace")),
        ReflectionAnalysisBuilder1=NS(
            Types=enums("LineImages"),
            LineImageTypes=enums("BlackLines", "BlackandWhiteLines", "ColoredLines"),
            NumberOfLinesOptions=enums("Four", "Eight", "Sixteen", "ThirtyTwo", "SixtyFour"),
            LineOrientationType=enums("Horizontal", "Vertical", "Both"),
        ),
        RadiusAnalysisBuilder1=NS(
            Types=enums("Gaussian", "Minimum", "Maximum", "Mean"), DisplayModes=enums("Fringe")
        ),
    )
    return made


def shared_pair(body):
    """Two faces of a fake block that share an edge, and a face opposite the first."""
    first = body.faces[0]
    edge = first.GetEdges()[0]
    second = next(f for f in edge.GetFaces() if f is not first)
    far = next(
        f
        for f in body.faces
        if f is not first and not set(map(id, f.GetEdges())) & set(map(id, first.GetEdges()))
    )
    return first, second, edge, far


def test_shared_edges_are_sampled_and_judged(monkeypatch):
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    first, second, edge, _far = shared_pair(body)
    calls = []

    def report(tx, e, f1, e2, f2, samples):
        calls.append((e, e2, {f1.Tag, f2.Tag}, samples))
        return {"gap": 0.0, "angle": 0.05, "curvature": 0.2, "samples": samples, "unresolved": 0}

    monkeypatch.setattr(surface, "edge_continuity", report)
    ok = run(nx, "nx_check_face_continuity", faces=[first.ref, second.ref], samples=5)
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    assert calls == [(edge, edge, {first.Tag, second.Tag}, 5)]
    d = ok.data
    assert d["edges"][0]["edge"]["id"] == edge.ref and len(d["edges"][0]["faces"]) == 2
    assert (d["g1"], d["g2"]) == (True, False) and d["angle"] == 0.05 and d["curvature"] == 0.2
    assert d["samples"] == 5 and d["tolerances"] == {"angle": 0.1, "curvature": 0.01}
    assert "nx" not in d  # no analysis builders on this host
    loose = run(nx, "nx_check_face_continuity", faces=[first.ref, second.ref],
                tolerances={"curvature": 0.5})  # fmt: skip
    assert loose.data["g2"] is True


def test_nx_verdict_comes_from_a_temporary_analysis(monkeypatch):
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    first, second, _edge, _far = shared_pair(body)
    made = with_analysis(nx, results=(True, False, False, False))
    monkeypatch.setattr(
        surface,
        "edge_continuity",
        lambda *a: {"gap": 0.0, "angle": 90.0, "curvature": 0.0, "samples": 9, "unresolved": 0},
    )
    ok = run(nx, "nx_check_face_continuity", faces=[first.ref, second.ref],
             tolerances={"angle": "0.5 deg"})  # fmt: skip
    assert ok.data["nx"] == {"g0": True, "g1": False} and ok.data["g1"] is False
    (b,) = made
    assert b.Type == "MultiFace" and b.G1Tolerance == 0.5 and b.destroyed
    assert list(b.MultiFace) == [first, second]


def test_unresolved_samples_leave_the_verdict_open(monkeypatch):
    nx = inspection_nx()
    first, second, _e, _f = shared_pair(nx.block(10, 10, 10))
    monkeypatch.setattr(
        surface,
        "edge_continuity",
        lambda *a: {"gap": 0.0, "angle": None, "curvature": None, "samples": 9, "unresolved": 9},
    )
    d = run(nx, "nx_check_face_continuity", faces=[first.ref, second.ref]).data
    assert d["g1"] is None and d["angle"] is None and d["edges"][0]["unresolved"] == 9


def test_faces_without_a_shared_edge_are_refused():
    nx = inspection_nx()
    first, _second, _edge, far = shared_pair(nx.block(10, 10, 10))
    bad = run(nx, "nx_check_face_continuity", faces=[first.ref, far.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "nx_check_continuity" in bad.error["hint"]
    angle = run(nx, "nx_check_face_continuity", faces=[first.ref, far.ref],
                tolerances={"angle": 95})  # fmt: skip
    assert angle.error["field"] == "/tolerances/angle"


def test_reflection_analysis_is_kept_and_named():
    nx = inspection_nx()
    first, second, _e, _f = shared_pair(nx.block(10, 10, 10))
    made = with_analysis(nx)
    ok = run(nx, "nx_create_surface_analysis", type="reflection", faces=[first.ref, second.ref],
             lines=32, line_type="black", orientation="both")  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    (b,) = made
    assert (b.Type, b.LineImageType, b.LineNumber, b.LineOrientation) == (
        "LineImages", "BlackLines", "ThirtyTwo", "Both"
    )  # fmt: skip
    assert b.committed[0].Name == "NX_MCP_REFLECTION" and b.destroyed
    assert ok.data["type"] == "reflection" and ok.data["faces"] == 2


def test_comb_continuity_and_radius_analyses():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    first, second, edge, _f = shared_pair(body)
    made = with_analysis(nx)
    comb = run(nx, "nx_create_surface_analysis", type="curvature_comb", curves=[edge.ref],
               density=80)  # fmt: skip
    assert comb.ok and made[-1].rules == [("EdgeDumb", [edge])]
    assert made[-1].CombOptions.Density == 80 and made[-1].ShowCombs is True
    cont = run(nx, "nx_create_surface_analysis", type="continuity", faces=[first.ref, second.ref],
               angle=0.5)  # fmt: skip
    assert cont.ok and made[-1].G1Tolerance == 0.5 and made[-1].G2 is True
    radius = run(nx, "nx_create_surface_analysis", type="radius", faces=[first.ref],
                 radius_type="mean")  # fmt: skip
    assert radius.ok and made[-1].Type == "Mean" and made[-1].started
    assert made[-1].DisplayMode == "Fringe"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"type": "reflection"}, "/faces"),
        ({"type": "curvature_comb"}, "/curves"),
        ({"type": "curvature_comb", "faces": ["FACE"], "curves": ["EDGE"]}, "/faces"),
        ({"type": "reflection", "faces": ["FACE"], "curves": ["EDGE"]}, "/curves"),
        ({"type": "continuity", "faces": ["FACE"]}, "/faces"),
        ({"type": "continuity", "faces": ["FACE", "FACE2"], "angle": 120}, "/angle"),
    ],
)
def test_display_arguments_are_refused_before_nx(args, field):
    nx = inspection_nx()
    first, second, edge, _f = shared_pair(nx.block(10, 10, 10))
    swap = {"FACE": first.ref, "FACE2": second.ref, "EDGE": edge.ref}
    args = {k: [swap.get(v, v) for v in val] if isinstance(val, list) else val
            for k, val in args.items()}  # fmt: skip
    made = with_analysis(nx)
    bad = run(nx, "nx_create_surface_analysis", **args)
    assert bad.error["field"] == field, bad
    assert made == [] and bad.mutation_outcome in ("not_applicable", "not_started")
