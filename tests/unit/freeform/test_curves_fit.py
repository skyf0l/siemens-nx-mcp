"""nx_import_points and nx_create_fit_curve on the shared fake."""

import math
from pathlib import Path

import pytest

from tests.kit import point, run
from tests.kit.kernel import services
from tests.kit.objects import Object
from tests.unit.freeform.fakes import Polyline
from tests.unit.freeform.support import curves_nx
from tests.unit.modeling.fakes import feature

PFF = "PointsFromFileBuilder"
FIT = "FitCurveBuilder"


class Points(list):
    """The part's points: ``CreatePoint`` appends a work-part point."""

    def __init__(self, nx):
        super().__init__()
        self.nx = nx

    def CreatePoint(self, p):
        return self.add([p.X, p.Y, p.Z])

    def add(self, xyz):
        p = Object("POINT")
        p.Coordinates = point(*xyz)
        p.OwningPart, p.IsOccurrence = self.nx.part, False
        self.nx.keep(p)
        self.append(p)
        return p


def circle(r=50.0, n=24, z=0.0):
    return [
        [r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z] for i in range(n)
    ]


class Fitted(Object):
    """A fitted curve: a polyline through ``points`` (closed when asked)."""

    def __init__(self, points, closed, **facts):
        super().__init__("FIT")
        pts = list(points) + ([points[0]] if closed else [])
        self.shape = Polyline(pts)
        for name, value in facts.items():
            setattr(self, name, value)

    def GetLength(self):
        p = self.shape.points
        return sum(math.dist(a, b) for a, b in zip(p, p[1:], strict=False))


def fit_nx(facts=None):
    """A fake whose Points from File reads the staged file and whose fit commits a
    ``Fitted`` curve through the target points with ``facts`` (or ``facts(b)``)."""
    nx = curves_nx()
    nx.part.Points = Points(nx)
    nx.read_files = []

    def from_file(b):
        text = Path(b.FileName).read_text()
        nx.read_files.append((b.FileName, text))
        for line in text.splitlines():
            nx.part.Points.add([float(v) for v in line.split()])
        return Object("GROUP")

    nx.part.CreatePointsFromFileBuilder = lambda: nx.builder(PFF)
    nx.on_commit(PFF, from_file)

    def fit(b):
        targets = b.Target.Add.calls[-1][0][0]
        pts = [[t.Coordinates.X, t.Coordinates.Y, t.Coordinates.Z] for t in targets]
        closed = bool(getattr(b, "IsClosedBSpline", False) is True) or bool(
            getattr(b, "IsClosedCurve", False) is True
        )
        made = facts(b) if callable(facts) else (facts or {})
        f = feature(nx, "Fit Curve")
        c = nx.remember(Fitted(pts, closed, **made))
        f.GetEntities = lambda: [c]
        return f

    nx.on_commit(FIT, fit)
    return nx


def _write(nx, name, text):
    root = services(nx).root
    (root / name).write_text(text)
    return name


def test_import_points_parses_a_csv_with_a_header_and_stages_plain_rows():
    nx = fit_nx()
    path = _write(nx, "hull.csv", "station,wl,y\n# comment\n0,0,0\n10, 5, 0\n\n20;0;1.5\n")
    ok = run(nx, "nx_import_points", path=path)
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["count"] == 3 and ok.data["listed"] == 3
    assert ok.data["first"] == [0, 0, 0] and ok.data["last"] == [20, 0, 1.5]
    assert ok.data["bounds"] == {"min": [0, 0, 0], "max": [20, 5, 1.5]}
    staged, text = nx.read_files[0]
    assert text == "0.0 0.0 0.0\n10.0 5.0 0.0\n20.0 0.0 1.5\n"
    assert Path(staged).name == "hull.txt" and not Path(staged).exists()
    assert nx.live_builders == 0


def test_import_points_refuses_a_bad_row_before_nx_and_checks_what_nx_made():
    nx = fit_nx()
    bad = run(nx, "nx_import_points", path=_write(nx, "bad.txt", "1 2 3\n4 5\n"))
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/path"
    assert "Line 2" in bad.error["message"] and not nx.builders
    few = run(nx, "nx_import_points", path=_write(nx, "one.txt", "1 2 3\n"))
    assert few.error["field"] == "/path" and not nx.builders
    nx.on_commit(PFF, lambda b: Object("GROUP"))  # NX made nothing
    lost = run(nx, "nx_import_points", path=_write(nx, "ok.txt", "1 2 3\n4 5 6\n"))
    assert lost.error["details"]["check"] == "count" and lost.mutation_outcome == "rolled_back"


def test_fit_spline_through_point_refs_defaults_to_a_tolerance_fit():
    nx = fit_nx(lambda b: {"Order": b.Degree + 1, "Periodic": b.IsClosedBSpline,
                           "Get3DPoles": lambda: [0] * 9})  # fmt: skip
    refs = [nx.ref_of(nx.part.Points.add(p), "point") for p in circle()]
    ok = run(nx, "nx_create_fit_curve", points=refs, closed=True)
    assert ok.ok, ok.error
    b = nx.last(FIT)
    kinds = nx.nx.Features.FitCurveBuilder
    assert b.Type is kinds.Types.FitSpline and b.Degree == 3 and b.IsClosedBSpline is True
    assert b.FittingParameters is kinds.FittingParametersOptions.DegreeAndTolerance
    assert b.Tolerance == pytest.approx(0.01)
    assert ok.data["points"] == 24 and ok.data["max_deviation"] == pytest.approx(0, abs=1e-9)
    assert ok.data["degree"] == 3 and ok.data["poles"] == 9 and ok.data["closed"] is True
    seg = run(nx, "nx_create_fit_curve", points=refs, degree=5, segments=12)
    assert seg.ok and nx.last(FIT).Segments == 12, seg.error
    assert nx.last(FIT).FittingParameters is kinds.FittingParametersOptions.DegreeAndSegments


def test_fit_circle_from_a_file_imports_the_points_first():
    nx = fit_nx({"Radius": 50.0, "CenterPoint": point(0, 0, 0)})
    rows = "".join(f"{x} {y} {z}\n" for x, y, z in circle())
    ok = run(nx, "nx_create_fit_curve", shape="circle", path=_write(nx, "c.txt", rows),
             closed=True)  # fmt: skip
    assert ok.ok, ok.error
    assert ok.data["imported"] == 24 and ok.data["radius"] == 50.0
    assert ok.data["center"] == [0, 0, 0] and nx.last(FIT).IsClosedCurve is True
    assert ok.data["length"] == pytest.approx(24 * 2 * 50 * math.sin(math.pi / 24))
    assert [b.kind for b in nx.builders] == [PFF, FIT] and nx.live_builders == 0


def test_fit_argument_checks_and_a_wrong_shape_rolls_back():
    nx = fit_nx()
    refs = [nx.ref_of(nx.part.Points.add(p), "point") for p in circle(n=4)]
    cases = [
        ({"shape": "circle", "degree": 3}, "/degree"),
        ({"segments": 2, "tolerance": 0.1}, "/tolerance"),
        ({"shape": "line", "closed": True}, "/closed"),
        ({"shape": "ellipse"}, "/points"),
        ({"degree": 5}, "/points"),
    ]
    for extra, field in cases:
        bad = run(nx, "nx_create_fit_curve", points=refs, **extra)
        assert bad.error["category"] == "invalid_argument", (extra, bad)
        assert field is None or bad.error["field"] == field, (extra, bad.error)
    path = _write(nx, "x.txt", "1 2 3\n4 5 6\n")
    both = run(nx, "nx_create_fit_curve", points=refs, path=path)
    assert both.error["field"] == "/path" and not nx.builders
    wrong = run(nx, "nx_create_fit_curve", shape="circle", points=refs)  # NX made no arc
    assert wrong.error["details"]["check"] == "shape" and wrong.mutation_outcome == "rolled_back"
