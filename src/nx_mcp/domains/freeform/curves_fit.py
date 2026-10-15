"""Points from a workspace text file, and curves fitted through points (Fit Curve).

NX 2406 lessons kept here:

- ``Part.CreatePointsFromFileBuilder``: ``FileName`` names the file; ``Commit()`` returns a
  ``Group`` named after the file that holds the new points (absolute, part units, in file
  order). NX also read an 'x,y,z' CSV with a header line, but the tool parses the file
  itself (errors name the line) and hands NX a staged 'x y z' copy, so the points NX makes
  are exactly the rows it checked; the staged copy is removed afterwards.
- ``FeatureCollection.CreateFitCurveBuilder(None)``: ``Type`` FitSpline/FitLine/FitCircle/
  FitEllipse, ``TargetSourceType`` SpecifiedPoints, ``Target.Add(points)``; a spline fits
  ``FittingParameters`` DegreeAndSegments (``Degree``, ``Segments``) or DegreeAndTolerance
  (``Tolerance``), ``IsClosedBSpline`` makes it periodic; circle and ellipse
  ``IsClosedCurve`` (full curve, else an arc). The feature type is "Fit Curve".
  24 points of an R50 circle: the circle fit read radius 50.0; a closed cubic spline of
  12 segments deviated 0.026 at most, of 6 segments 0.40. 16 points of a 40 x 20 ellipse
  fitted major/minor radii 40/20.
- NX reports no deviation: the read-back measures every target point's distance to the
  fitted curve (UF closest point).
"""

from __future__ import annotations

import contextlib
import math
import re
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, PathTo, Point3, Range, RefTo
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import args, builders, curve, files, geom, measure, readback, result, select, units

SUFFIXES = (".txt", ".csv", ".dat", ".xyz")
MAX_POINTS = 10000
MAX_LISTED = 500  # point refs returned by nx_import_points
MAX_BYTES = 5 * 1024 * 1024
MAX_HEADER = 5  # non-numeric lines allowed before the first row
DEFAULT_DEGREE = 3
DEFAULT_TOLERANCE_MM = 0.01
SPLIT = re.compile(r"[,;\s]+")
PointRef = Annotated[str, RefTo("point", doc="Point 'pt:5'")]
FitShape = Literal["spline", "line", "circle", "ellipse"]
TYPES = {"spline": "FitSpline", "line": "FitLine", "circle": "FitCircle", "ellipse": "FitEllipse"}
MIN_POINTS = {"line": 2, "circle": 3, "ellipse": 5}
FILE_DOC = "x y z rows (spaces, tabs, commas or semicolons); .txt/.csv/.dat/.xyz"


class Bounds(TypedDict):
    min: Point3
    max: Point3


class PointsData(TypedDict):
    points: list[result.Row]  # the first MAX_LISTED, in file order
    count: int
    listed: int
    first: Point3
    last: Point3
    bounds: Bounds


class FitData(TypedDict):
    feature: result.Row
    curve: result.Row
    shape: str
    points: int  # target points fitted
    max_deviation: float  # largest point-to-curve distance, part units
    mean_deviation: float
    length: float
    start: Point3
    end: Point3
    closed: bool


class FitShapeData(FitData, total=False):
    degree: int  # spline
    poles: int  # spline
    radius: float  # circle
    center: Point3  # circle, ellipse
    major_radius: float  # ellipse
    minor_radius: float  # ellipse
    imported: int  # path: points created from the file


# ---- the file -------------------------------------------------------------------------
def _numbers(tokens):
    try:
        values = [float(t) for t in tokens]
    except ValueError:
        return None
    return values if all(math.isfinite(v) for v in values) else None


def read_rows(path, field="/path") -> list[list[float]]:
    """The ``[x, y, z]`` rows of a points file: header lines (before the first row), blank
    lines and '#' comments are skipped; any other line must hold exactly 3 numbers."""
    file = Path(path)
    if file.stat().st_size > MAX_BYTES:
        fail("NX_INVALID_ARGUMENT", f"The file is over {MAX_BYTES // (1024 * 1024)} MB",
             field=field)  # fmt: skip
    text = file.read_text(encoding="utf-8-sig", errors="replace")
    rows: list[list[float]] = []
    header = 0
    for number, line in enumerate(text.splitlines(), start=1):
        body = line.strip()
        if not body or body.startswith(("#", "//")):
            continue
        tokens = [t for t in SPLIT.split(body) if t]
        values = _numbers(tokens)
        if values is None and not rows and header < MAX_HEADER:
            header += 1
            continue
        if values is None or len(values) != 3:
            fail("NX_INVALID_ARGUMENT", f"Line {number}: expected 3 numbers x y z, got {body[:60]!r}",
                 field=field, details={"line": number})  # fmt: skip
        rows.append(values)
        if len(rows) > MAX_POINTS:
            fail("NX_INVALID_ARGUMENT", f"The file has more than {MAX_POINTS} points",
                 field=field)  # fmt: skip
    if len(rows) < 2:
        fail("NX_INVALID_ARGUMENT", f"The file has {len(rows)} point rows; at least 2 are needed",
             field=field)  # fmt: skip
    return rows


def _staged(tx, path, rows) -> Path:
    """An 'x y z' copy of ``rows`` under the workspace staging folder (NX reads it)."""
    folder = files.temp_path(tx.workspace.root, "points", tx.operation_id).parent
    target = folder / f"{Path(path).stem}.txt"
    target.write_text("".join(f"{x!r} {y!r} {z!r}\n" for x, y, z in rows), encoding="ascii")
    return target


def _unstage(target: Path) -> None:
    with contextlib.suppress(OSError):
        target.unlink()
    with contextlib.suppress(OSError):
        target.parent.rmdir()


def import_rows(tx, path, rows) -> list:
    """Points made by NX's Points from File from ``rows`` (checked against them)."""
    before = {int(p.Tag) for p in tx.part.Points}
    staged = _staged(tx, path, rows)
    try:
        with builders.build(tx, tx.part.CreatePointsFromFileBuilder) as b:
            b.FileName = str(staged)
            b.CoordinateOption = tx.nx.GeometricUtilities.PointsFromFileBuilder.Options.Absolute
            b.commit()
    finally:
        _unstage(staged)
    with native(default="NX_ERROR"):
        made = [p for p in tx.part.Points if int(p.Tag) not in before]
    readback.expect(len(made) == len(rows), "count", "NX made a different number of points",
                    expected=len(rows), made=len(made))  # fmt: skip
    limit = max(units.part_tolerance(tx), geom.tol(*rows[:1]))
    for i, (p, row) in enumerate(zip(made, rows, strict=True)):
        got = geom.xyz(p.Coordinates)
        if geom.dist(got, row) > max(limit, geom.tol(row)):
            readback.expect(False, f"point[{i}]", "A point is not where the file puts it",
                            expected=row, point=got)  # fmt: skip
        if i % 500 == 0:
            tx.checkpoint()
    return made


def _bounds(rows) -> Bounds:
    return {
        "min": cc.clean([min(r[i] for r in rows) for i in range(3)]),
        "max": cc.clean([max(r[i] for r in rows) for i in range(3)]),
    }


@tool(
    effect="model",
    label="Import points from file",
    summary="Import points from {path}",
    keywords=("points from file", "import points", "offset table", "xyz file", "csv points"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    example={"path": "inbox/hull_offsets.txt"},
)
def nx_import_points(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("read", *SUFFIXES), Doc(FILE_DOC)],
) -> PointsData:
    """Create points in the work part from a workspace text file of x y z rows (absolute,
    part units), e.g. a hull offset table. Spaces, tabs, commas or semicolons separate the
    numbers; header lines before the first row, blank lines and '#' comments are skipped;
    2-10000 rows. The points are plain (non-associative) points, not a feature. Returns the
    first 500 point refs; nx_create_fit_curve fits a curve through them (or reads the file
    itself with path)."""
    rows = read_rows(path)
    made = import_rows(tx, path, rows)
    listed = [tx.ref(p, kind="point") for p in made[:MAX_LISTED]]
    return {
        "points": listed,
        "count": len(made),
        "listed": len(listed),
        "first": cc.clean(rows[0]),
        "last": cc.clean(rows[-1]),
        "bounds": _bounds(rows),
    }


# ---- fit curve --------------------------------------------------------------------------
def _check(shape, count, degree, segments, tolerance, closed, field):
    spline_only = {"degree": degree, "segments": segments, "tolerance": tolerance}
    if shape != "spline":
        for name, value in spline_only.items():
            if value is not None:
                fail("NX_INVALID_ARGUMENT", f"{name} applies to shape spline only",
                     field=f"/{name}")  # fmt: skip
        if shape == "line" and closed:
            fail("NX_INVALID_ARGUMENT", "A line cannot be closed", field="/closed")
        need = MIN_POINTS[shape]
    else:
        if segments is not None and tolerance is not None:
            fail("NX_INVALID_ARGUMENT", "segments and tolerance are mutually exclusive: give one",
                 field="/tolerance")  # fmt: skip
        if tolerance is not None and not float(tolerance) > 0:
            fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field="/tolerance")
        need = (DEFAULT_DEGREE if degree is None else int(degree)) + 1
    if count < need and shape == "spline" and degree is not None and count >= 2:
        fail("NX_INVALID_ARGUMENT", f"A degree {degree} fit needs at least {need} points, got "
             f"{count}", field="/degree",
             hint=f"Send degree {count - 1} or lower, or fit more points")  # fmt: skip
    if count < need:
        fail("NX_INVALID_ARGUMENT", f"A {shape} fit needs at least {need} points, got {count}",
             field=field)  # fmt: skip


def _check_spread(shape, coords, field):
    """Circle and ellipse fits need points off one line (NX: "Unable to fit curve")."""
    if shape not in ("circle", "ellipse"):
        return
    first = coords[0]
    far = max(coords, key=lambda p: geom.dist(p, first))
    axis = geom.sub(far, first)
    size = geom.norm(axis)
    off = max((geom.norm(geom.cross(axis, geom.sub(p, first))) for p in coords), default=0.0)
    if size <= 0 or off <= 1e-6 * size * max(size, 1.0):
        fail("NX_INVALID_ARGUMENT", f"The points lie on one line: no {shape} fits them",
             field=field, hint='Send shape "line" for collinear points')  # fmt: skip


def _configure(tx, b, shape, targets, degree, segments, tolerance, closed):
    kinds = tx.nx.Features.FitCurveBuilder
    b.Type = getattr(kinds.Types, TYPES[shape])
    b.TargetSourceType = kinds.TargetSourceTypes.SpecifiedPoints
    b.Target.Add(targets)
    if shape == "spline":
        b.Degree = int(degree)
        if segments is not None:
            b.FittingParameters = kinds.FittingParametersOptions.DegreeAndSegments
            b.Segments = int(segments)
        else:
            b.FittingParameters = kinds.FittingParametersOptions.DegreeAndTolerance
            b.Tolerance = float(tolerance)
        b.IsClosedBSpline = bool(closed)
    elif shape in ("circle", "ellipse"):
        b.IsClosedCurve = bool(closed)


def _deviations(tx, fitted, coords) -> list[float]:
    out = []
    with curve.evaluator(tx, fitted) as e:
        for i, p in enumerate(coords):
            out.append(geom.dist(e.closest(p)[0], p))
            if i % 500 == 0:
                tx.checkpoint()
    return out


def _one(tx, feature):
    made = cc.output_curves(tx, feature)
    readback.expect(len(made) == 1, "curves", "NX built more than one curve", count=len(made))
    return made[0]


def _backwards(tx, fitted, coords) -> bool:
    """Whether an open fitted curve starts at the last point and ends at the first."""
    start, end = curve.ends(tx, fitted)
    first, last = coords[0], coords[-1]
    return geom.dist(start, last) + geom.dist(end, first) < (
        geom.dist(start, first) + geom.dist(end, last)
    )


def _shape_facts(tx, shape, fitted, degree, closed) -> dict:
    if shape == "spline":
        readback.expect(hasattr(fitted, "Get3DPoles"), "shape", "NX built no spline")
        got = int(fitted.Order) - 1
        readback.expect(got == degree, "degree", f"NX built degree {got}", degree=got)
        readback.expect(bool(fitted.Periodic) == bool(closed), "closed",
                        periodic=bool(fitted.Periodic))  # fmt: skip
        return {"degree": got, "poles": len(fitted.Get3DPoles())}
    if shape == "circle":
        readback.expect(hasattr(fitted, "Radius"), "shape", "NX built no circle or arc")
        return {"radius": float(fitted.Radius), "center": cc.clean(geom.xyz(fitted.CenterPoint))}
    if shape == "ellipse":
        readback.expect(hasattr(fitted, "MajorRadius"), "shape", "NX built no ellipse")
        return {
            "major_radius": float(fitted.MajorRadius),
            "minor_radius": float(fitted.MinorRadius),
            "center": cc.clean(geom.xyz(fitted.CenterPoint)),
        }
    readback.expect(
        not any(hasattr(fitted, n) for n in ("Radius", "MajorRadius", "Get3DPoles")),
        "shape",
        "NX built no line",
    )
    return {}


@tool(
    effect="model",
    label="Create fit curve",
    summary=lambda a, say: (
        f"Fit a {a.get('shape') or 'spline'} through {say('points' if a.get('points') else 'path')}"
    ),
    keywords=(
        "fit curve",
        "fit spline",
        "best fit circle",
        "curve through points",
        "smooth points",
        "approximate scanned points",
        "point cloud",
        "best fit",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="shape",
    cost="heavy",
    previewable=True,
    example={"shape": "spline", "path": "inbox/hull_station_5.txt", "degree": 3},
)
def nx_create_fit_curve(
    tx: Tx,
    *,
    shape: FitShape = "spline",
    points: Annotated[list[PointRef], Items(2, MAX_POINTS), Doc("Points to fit; or give path")]
    | None = None,
    path: Annotated[str, PathTo("read", *SUFFIXES), Doc(FILE_DOC + "; or give points")]
    | None = None,
    degree: Annotated[int, Range(ge=1, le=24), Doc("spline: default 3")] | None = None,
    segments: Annotated[int, Range(ge=1, le=500), Doc("spline: fixed segment count")] | None = None,
    tolerance: Annotated[Length, Doc("spline: fit tolerance (default 0.01 mm); not with segments")]
    | None = None,
    closed: Annotated[
        bool, Doc("spline: periodic; circle/ellipse: full curve, else an arc")
    ] = False,
) -> FitShapeData:
    """Create an associative least-squares curve fitted through points. Give point refs, or
    a workspace x y z file whose points are created first (as nx_import_points). spline:
    degree, and segments or a tolerance (default 0.01 mm); line; circle (radius, center);
    ellipse. An open spline runs from the first point to the last. Returns max_deviation and
    mean_deviation, the measured point-to-curve distances. For a curve exactly through few
    points use nx_create_spline."""
    source = args.exactly_one(points=points, path=path)
    rows = read_rows(path) if source == "path" else None
    count = len(rows) if rows is not None else len(points or [])
    field = "/path" if source == "path" else "/points"
    _check(shape, count, degree, segments, tolerance, closed, field)
    with tx.checks():
        targets = []
        if points is not None:
            args.distinct("/points", points, "points")
            targets = [
                select.one(tx, r, "point", field=f"/points/{i}") for i, r in enumerate(points)
            ]
            with native(default="NX_ERROR"):
                _check_spread(shape, [geom.xyz(p.Coordinates) for p in targets], field)
        else:
            _check_spread(shape, rows, field)
    cc.licensed(tx)
    degree = DEFAULT_DEGREE if degree is None else int(degree)
    if shape == "spline" and segments is None and tolerance is None:
        tolerance = units.part_tolerance(tx, DEFAULT_TOLERANCE_MM)
    if rows is not None:
        targets = import_rows(tx, path, rows)
        coords = rows
    else:
        with native(default="NX_ERROR"):
            coords = [geom.xyz(p.Coordinates) for p in targets]
    with builders.build(tx, tx.part.Features.CreateFitCurveBuilder, None) as b:
        _configure(tx, b, shape, targets, degree, segments, tolerance, closed)
        feature = b.commit()
    fitted = _one(tx, feature)
    backwards = not closed and _backwards(tx, fitted, coords)
    if backwards and shape != "spline":
        tx.warn("NX_NOTE", f"The fitted {shape} runs from the last point to the first")
    elif backwards:
        # NX 2406 fitted splines from the last point to the first: run it the file's way.
        with builders.build(tx, tx.part.Features.CreateFitCurveBuilder, feature) as b:
            b.HasReversedDirection = not bool(b.HasReversedDirection)
            feature = b.commit() or feature
        fitted = _one(tx, feature)
        readback.expect(not _backwards(tx, fitted, coords), "direction",
                        "The fitted curve runs from the last point to the first")  # fmt: skip
    facts = _shape_facts(tx, shape, fitted, degree, closed)
    gaps = _deviations(tx, fitted, coords)
    start, end = curve.ends(tx, fitted)
    out: FitShapeData = {
        "feature": tx.ref(feature, kind="feature"),
        "curve": tx.ref(fitted, kind="curve"),
        "shape": shape,
        "points": len(coords),
        "max_deviation": max(gaps),
        "mean_deviation": sum(gaps) / len(gaps),
        "length": measure.length(tx, fitted),
        "start": cc.clean(start),
        "end": cc.clean(end),
        "closed": geom.dist(start, end) <= cc.gap_tolerance(tx, start, end),
        **facts,
    }
    if rows is not None:
        out["imported"] = len(targets)
    return out
