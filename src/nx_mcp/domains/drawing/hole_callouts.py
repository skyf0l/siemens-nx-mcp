"""Hole callouts and hole tables on drawings (tool-surface 4), used by
``nx_create_drawing_dimension kind=hole_callout`` and ``nx_create_drawing_table kind=hole``.

NX 2406 findings encoded here:

- ``Dimensions.CreateHoleCalloutDimensionBuilder`` sits behind a feature toggle that is
  off on 2406 ("The feature toggle controlling this function is not activated"):
  callouts are radial dimensions with measurement method ``HoleCallout``.
- That method needs the hole *feature* as first associativity. Given an edge, NX resets
  the method to ``Diametral`` and draws a plain diameter. A hole package (leader to its
  first hole) and one hole instance (``SIMPLE HOLE(3:1A)``, leader to that hole) work.
- The callout text cannot be read back (``GetDimensionText`` gives " ", UF text data
  too). It is composed from the dimension's ``HoleCalloutSettings`` (display flag,
  prefix, value, suffix, line breaks); those values follow the feature after an edit.
- NX gives a multi-hole package no count: ``<n>X`` is appended before the callout and
  stays as created; only when the whole package is selected (one instance or edge: no count).
- A hole table takes bodies with their view (``SelectedObjects.AddWithViews``); a view
  alone is "Invalid selected objects". Labels are numbered across the part; rows are a
  title, a header (``Hole No.``, X, Y, Z), group rows (size text) and hole rows.
- X and Y of a hole row run along the view's horizontal and vertical axes from the
  ordinate origin. NX 2406 can measure a table from another point of a straight
  origin edge than the requested end (cause not pinned down): the table is matched
  against every snap point of the edge; ``auto`` takes the one that matches (``origin``
  in the result, with a warning), an explicit snap point is refused naming the match.
"""

from __future__ import annotations

import math
import re

from nx_mcp.core.catalogue import refresher
from nx_mcp.core.errors import fail
from nx_mcp.domains.drawing.common import geometry, update
from nx_mcp.domains.drawing.dimensions import placed, text_spot
from nx_mcp.kit import builders, drawing, facts, geom, readback, result, select, units
from nx_mcp.kit.sketch import snake  # unknown parameter and feature names

LINE_BREAK = "LineBreak"
PARAMETERS = {
    "Diameter": "diameter",
    "Depth": "depth",
    "CounterBoreDiameter": "counterbore_diameter",
    "CounterBoreDepth": "counterbore_depth",
    "CounterSinkDiameter": "countersink_diameter",
    "CounterSinkAngle": "countersink_angle",
    "TaperAngle": "taper_angle",
    "PatternFeatureCount": "count",
    "ThreadSize": "thread_size",
    "ThreadDepth": "thread_depth",
    "ThreadTolerance": "thread_tolerance",
    "TapDrillDiameter": "tap_drill_diameter",
    "MajorDiameter": "major_diameter",
    "MinorDiameter": "minor_diameter",
}
FEATURE_TYPES = {
    "GeneralHole": "general",
    "ThreadedHole": "threaded",
    "DrillSizeHole": "drill_size",
    "ScrewClearanceHole": "screw_clearance",
    "SymbolicThread": "symbolic_thread",
}
DIAMETER = "Ø"


# ---- pure helpers -------------------------------------------------------------------------


def clean_value(text):
    """A callout parameter value as NX shows it: no comment, no quotes, ``6.0`` -> ``6``."""
    value = str(text or "").split("//")[0].strip().strip('"').strip()
    number = to_number(value)
    if number is not None and not re.search(r"[eE]", value):
        return f"{number:.6f}".rstrip("0").rstrip(".")
    return value


def to_number(text):
    """A float from NX text (period or comma decimals) or None."""
    value = str(text or "").strip().replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _affix(parts):
    if isinstance(parts, str):
        parts = [parts]
    return "".join(str(p) for p in parts or []).replace("<o>", DIAMETER).replace("<O>", DIAMETER)


def compose(params, count=None):
    """Callout lines from ``(name, shown, prefix, value, suffix)`` parameters as NX lays
    them out; ``count`` > 1 starts the first line with ``<count>X``."""
    lines, line = [], ""
    for name, shown, prefix, value, suffix in params:
        if name == LINE_BREAK:
            if line.strip():
                lines.append(line.strip())
            line = ""
            continue
        value = clean_value(value)
        if not shown or not value:
            continue
        line += _affix(prefix) + value + _affix(suffix)
    if line.strip():
        lines.append(line.strip())
    if count and count > 1 and lines:
        lines[0] = f"{count}X {lines[0]}"
    return lines


def values(params):
    """Shown parameters by name: numbers as floats, the rest (thread size) as text."""
    out = {}
    for name, shown, _prefix, value, _suffix in params:
        text = clean_value(value)
        if name == LINE_BREAK or not shown or not text:
            continue
        key = PARAMETERS.get(name, snake(name))
        number = to_number(text)
        out[key] = number if number is not None else text
    return out


def parse_hole_table(rows):
    """Hole rows of an evaluated hole table, sorted by X then Y: ``label``, ``position``
    [x, y] (``z`` when a number) and ``size`` (the group row above). Rows before the
    ``X``/``Y`` header are the title; NX orders groups by size, so edits reorder rows."""
    header = next(
        (i for i, r in enumerate(rows) if {"x", "y"} <= {c.strip().lower() for c in r}), None
    )
    if header is None:
        return []
    titles = [c.strip().lower() for c in rows[header]]
    ix, iy = titles.index("x"), titles.index("y")
    iz = titles.index("z") if "z" in titles else None
    holes, size = [], None
    for row in rows[header + 1 :]:
        label = row[0].strip()
        x, y = to_number(row[ix]), to_number(row[iy])
        if label and x is not None and y is not None:
            hole = {"label": label, "position": [x, y], "size": size}
            z = to_number(row[iz]) if iz is not None else None
            if z is not None:
                hole["z"] = z
            holes.append(hole)
        elif label and all(not c.strip() for c in row[1:]):
            size = label.replace("<O>", DIAMETER)  # group row: size and depth of the holes below
    return sorted(holes, key=lambda h: (h["position"][0], h["position"][1], h["label"]))


def match_positions(expected, actual, tol):
    """Indices of ``expected`` [x, y] points with no ``actual`` point within ``tol`` on each
    axis (table values are rounded per coordinate)."""
    free = list(actual)
    missing = []
    for i, p in enumerate(expected):
        hit = next(
            (q for q in free if max(abs(a - b) for a, b in zip(p, q, strict=True)) <= tol), None
        )
        if hit is None:
            missing.append(i)
        else:
            free.remove(hit)
    return missing


# ---- hole features ------------------------------------------------------------------------


def _is_package(feature):
    return type(feature).__name__ == "HolePackage"


def _is_instance(feature):
    kind = str(getattr(feature, "FeatureType", "")).upper()
    return not _is_package(feature) and ("HOLE" in kind and "PACKAGE" not in kind)


def _tags(objects):
    return {int(o.Tag) for o in objects}


def packages(tx):
    return [f for f in tx.part.Features if _is_package(f)]


def hole_of_edge(tx, edge, field):
    """``(target, package)`` for an edge of a Hole feature: its hole instance and package."""
    tag = int(edge.Tag)
    package = next((p for p in packages(tx) if tag in _tags(p.GetEdges())), None)
    if package is None:
        fail("NX_WRONG_KIND", "The edge is not on a Hole feature", field=field,
             hint="Pick an edge of a hole made by nx_create_hole, or pass hole")  # fmt: skip
    instance = next(
        (f for f in tx.part.Features if _is_instance(f) and tag in _tags(f.GetEdges())), None
    )
    return instance or package, package


def hole_of_feature(tx, feature, field):
    """``(target, package)`` for a Hole package or one of its hole instances."""
    if _is_package(feature):
        return feature, feature
    if _is_instance(feature):
        edges = _tags(feature.GetEdges())
        package = next((p for p in packages(tx) if edges & _tags(p.GetEdges())), None)
        if package is not None:
            return feature, package
    fail("NX_WRONG_KIND", "Select a Hole feature", field=field)


def shown_count(package, whole):
    """The ``<n>X`` count of a callout: the package's holes when the whole package is
    selected, 1 for one hole instance or one edge."""
    return len(list(package.GetOrigins())) if whole else 1


def circles(tx, feature, view):
    """Circular edges of a hole feature seen true shape in ``view``, as edge facts."""
    z = drawing.axes(view)[2]
    out = []
    for edge in feature.GetEdges():
        info = facts.edge(tx, edge)
        normal = info.get("normal")
        if info["kind"] == "arc" and normal and drawing.seen_true_shape(normal, z):
            out.append((edge, info))
    return out


# ---- callouts -----------------------------------------------------------------------------


def _settings(tx, dimension):
    """``(feature type, [(name, shown, prefix, value, suffix)], method)`` of a callout."""
    A = tx.nx.Annotations
    kinds = A.HoleCalloutSettingsBuilder.Parametertype
    names = [n for n in dir(kinds) if n[:1].isupper() and n != "ValueOf"]
    with builders.build(tx, tx.part.Dimensions.CreateRadialDimensionBuilder, dimension) as b:
        method = result.enum_name(
            b.Measurement.Method, A.DimensionMeasurementBuilder.MeasurementMethod
        )
        hs = b.Style.HoleCalloutSettings
        found = ("unknown", [])
        for ft in hs.GetListOfFeatures():
            listed = [result.enum_name(p, kinds, names) for p in hs.GetListOfParameters(ft)]
            params = [
                (LINE_BREAK, True, [], "", [])
                if name == LINE_BREAK  # NX 2406 answers no prefix or suffix for a line break
                else (
                    name,
                    bool(hs.GetNthParameterDisplay(ft, i)),
                    list(hs.GetNthParameterPrefix(ft, i) or []),
                    str(hs.GetNthParameterValueString(ft, i) or ""),
                    list(hs.GetNthParameterSuffix(ft, i) or []),
                )
                for i, name in enumerate(listed)
            ]
            if any(v for n, _s, _p, v, _x in params if n != LINE_BREAK):
                kind = result.enum_name(ft, A.HoleCalloutSettingsBuilder.Featuretype)
                found = (FEATURE_TYPES.get(kind, snake(kind)), params)
                break
    return found[0], found[1], method


def _count(dimension):
    try:
        before = list(dimension.GetAppendedText().GetBeforeText())
    except Exception:  # appended text NX cannot report
        return None
    for text in before:
        m = re.fullmatch(r"\s*(\d+)\s*X\s*", str(text))
        if m:
            return int(m.group(1))
    return None


def callout_info(tx, dimension):
    """What a hole callout shows: ``text`` lines, ``parameters`` and ``feature_type``."""
    kind, params, method = _settings(tx, dimension)
    count = _count(dimension)
    out = {
        "method": "hole_callout" if method == "HoleCallout" else method.lower(),
        "feature_type": kind,
        "text": compose(params, count),
        "parameters": values(params),
    }
    if count:
        out["count"] = count
    return out


def is_callout(dimension):
    return type(dimension).__name__ == "HoleDimension"


def _default_origin(tx, view, sh, info):
    """Outside the view border on the side of the hole (up-right at the view centre)."""
    c2 = drawing.to_sheet(tx, view, info["center"])
    return text_spot(tx, sh, view, c2, drawing.outward(drawing.borders(tx, view), c2))


def create_callout(tx, view_ref, hole, first, origin):
    """A hole callout on a Hole feature (``hole``) or one of its edges (``first``)."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        if hole is not None:
            feature = select.resolve(tx, hole, "feature", field="/hole", work_part=True)
            target, package = hole_of_feature(tx, feature, "/hole")
            field = "/hole"
        else:
            edge = geometry(tx, first, ("edge",), "/first")
            target, package = hole_of_edge(tx, edge, "/first")
            field = "/first"
        seen = circles(tx, target, view)
        if not seen:
            fail("NX_UNSUPPORTED_GEOMETRY", "The hole is not seen along its axis in this view: "
                 "use a view that looks into the hole", field=field)  # fmt: skip
        if hole is None:
            picked = next((s for s in seen if int(s[0].Tag) == int(edge.Tag)), None)
            if picked is None:
                fail("NX_UNSUPPORTED_GEOMETRY", "The edge is not a circle seen true shape in "
                     "this view", field="/first")  # fmt: skip
        else:
            toward = drawing.axes(view)[2]  # the hole edge nearest the viewer, the largest
            picked = max(seen, key=lambda s: (round(geom.dot(s[1]["center"], toward), 6),
                                              s[1]["radius"]))  # fmt: skip
        count = shown_count(package, whole=hole is not None and target is package)
        smallest = min(info["radius"] for _e, info in seen)
    info = picked[1]
    pick = [c + info["radius"] * x for c, x in zip(info["center"], info["x_axis"], strict=True)]
    A = tx.nx.Annotations
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:
            origin = _default_origin(tx, view, sh, info)
        factory = tx.part.Dimensions.CreateRadialDimensionBuilder
        with builders.build(tx, factory, A.Dimension.Null) as b:
            b.FirstAssociativity.SetValue(
                tx.nx.InferSnapType.SnapType.NotSet,
                target,
                view,
                tx.nx.Point3d(*pick),
                None,
                None,
                tx.nx.Point3d(0.0, 0.0, 0.0),
            )
            b.Measurement.Method = A.DimensionMeasurementBuilder.MeasurementMethod.HoleCallout
            if count > 1:
                b.AppendedText.SetBefore([f"{count}X"])
            b.Origin.OriginPoint = sh.point(origin)
            dimension = b.commit()
        found = callout_info(tx, dimension)
        readback.expect(found["method"] == "hole_callout", "method",
                        "NX made a plain dimension, not a hole callout", method=found["method"])  # fmt: skip
        readback.expect(bool(found["text"]), "text", "The callout shows no hole parameters")
        if "diameter" in found["parameters"]:
            readback.expect_close(found["parameters"]["diameter"], 2.0 * smallest, "diameter")
        if found["feature_type"] == "threaded":
            readback.expect("thread_size" in found["parameters"], "thread",
                            "The callout shows no thread size", **found["parameters"])  # fmt: skip
        row = tx.ref(dimension, "dimension")
        row["value"] = result.measured(dimension.ComputedSize)
        return placed(tx, sh, dimension, {
            "dimension": row,
            "callout": {**found, "hole": tx.ref(target, "feature")},
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": units.part_units(tx),
        })  # fmt: skip


@refresher("dimension")
def refresh_callouts(tx, objects):
    """Update the views of hole callouts so they show the edited holes."""
    callouts = [d for d in getattr(tx.part, "Dimensions", None) or [] if is_callout(d)]
    if objects is not None:
        wanted = _tags(objects)
        callouts = [d for d in callouts if int(d.Tag) in wanted]
    views = {}
    for d in callouts:
        for i in range(1, int(getattr(d, "NumberOfAssociativities", 0) or 0) + 1):
            view = getattr(d.GetAssociativity(i), "ObjectView", None)
            if view is not None:
                views[int(view.Tag)] = view
    if not views:
        return []
    drawing.refresh(tx, list(views.values()))
    update(tx)
    return [tx.ref(d, "dimension") for d in callouts]


# ---- hole tables ----------------------------------------------------------------------------


def _origin_snap(tx, edge, snap, field):
    """``(snap, native snap type, {snap point name: model point})``: the requested point
    first, then the edge's other snap points."""
    from nx_mcp.domains.drawing.dimensions import ordinate_snap

    info = facts.edge(tx, edge)
    snap = ordinate_snap(info, snap, field)
    S = tx.nx.InferSnapType.SnapType
    native = S.Center if snap == "center" else S.Start if snap == "start" else S.End
    names = [] if info.get("closed") else ["start", "end"]
    names += ["center"] if info["kind"] == "arc" else []
    points = {snap: info[snap], **{n: info[n] for n in names if n != snap}}
    return snap, native, points


def expected_positions(tx, view, origin, bodies):
    """Hole-feature centres in the bodies, seen along their axis in ``view``: [x, y] from
    ``origin`` along the view axes, in part units."""
    ax, ay, az = drawing.axes(view)
    tags = _tags(bodies)
    out = []
    for package in packages(tx):
        try:
            owners = _tags(package.GetBodies())
        except Exception:  # features NX cannot answer for
            owners = tags
        if not owners & tags:
            continue
        for p, d in zip(package.GetOrigins(), package.GetDirections(), strict=False):
            if not drawing.seen_true_shape(geom.xyz(d), az):
                continue
            rel = geom.sub(geom.xyz(p), origin)
            out.append([geom.dot(rel, ax), geom.dot(rel, ay)])
    return out


def solid_bodies(tx):
    return [b for b in tx.part.Bodies if getattr(b, "IsSolidBody", True)]


def create_table(tx, view_ref, position, origin_ref, origin_point, table_info):
    """A hole table of the work part's solid bodies in ``view``, positions from an edge
    point; verified against the Hole features' centres."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edge = geometry(tx, origin_ref, ("edge",), "/origin")
        chosen, snap, points = _origin_snap(tx, edge, origin_point, "/origin")
        at = points[chosen]
        bodies = solid_bodies(tx)
        if not bodies:
            fail("NX_WRONG_PART_CONTEXT", "The work part has no solid body", field="/view")
    A = tx.nx.Annotations
    with drawing.on_sheet(tx, sheet) as sh:
        factory = tx.part.Annotations.HoleTables.CreateHoleTableBuilder
        with builders.build(tx, factory, A.HoleTable.Null) as b:
            b.SelectedObjects.AddWithViews(bodies, [view] * len(bodies))
            b.OrdinateOrigin.SetValue(
                snap, edge, view, tx.nx.Point3d(*at), None, None, tx.nx.Point3d(0.0, 0.0, 0.0)
            )
            b.Origin.Origin.SetValue(None, None, sh.point(position))
            table = b.commit(validate=True)
        update(tx)
        out = table_info(tx, table, "hole")
        used = verify_table(tx, view, points, bodies, out, sh.scale, origin_point == "auto")
        out["origin"] = {"origin_point": used, "point": [float(v) for v in points[used]]}
        return out


def places(rows):
    """The fewest decimals of the numbers in hole rows (2 when none)."""
    found = [
        len(c.strip().replace(",", ".").partition(".")[2])
        for r in rows
        for c in r[1:]
        if to_number(c) is not None
    ]
    return min(found) if found else 2


def verify_table(tx, view, points, bodies, out, scale, auto=False):
    """Every Hole-feature centre is a row of the table at its X/Y from the origin (the
    table shows sheet units; ``scale`` is part units per sheet unit).

    ``points`` maps the edge's snap points to model points, the requested one first.
    Returns the name of the point the table measures from: the requested one, or with
    ``auto`` another snap point of the edge that matches every hole (with a warning)."""
    holes = out.get("holes") or []
    shown = [h["position"] for h in holes]
    tol = 0.51 * 10 ** -places(out.get("rows") or [])

    def expected(point):
        return [[x / scale, y / scale] for x, y in expected_positions(tx, view, point, bodies)]

    def unsigned(point):
        """NX 2406 shows unsigned X/Y distances: from an origin at the right edge a hole
        10 to its left is listed at X 10.00."""
        return not match_positions([[abs(x), abs(y)] for x, y in expected(point)], shown, tol)

    chosen, *others = points
    wanted = expected(points[chosen])
    readback.expect(len(holes) >= len(wanted), "rows", "The table lists fewer holes than "
                    "the Hole features have", rows=len(holes), holes=len(wanted))  # fmt: skip
    missing = match_positions(wanted, shown, tol)
    if not missing:
        return chosen
    other = next((n for n in others if not match_positions(expected(points[n]), shown, tol)), None)
    if other is None and unsigned(points[chosen]):
        return chosen
    other = other or next((n for n in others if unsigned(points[n])), None)
    if other is not None and auto:
        tx.warn("NX_UPDATE_WARNING", f"NX measures this hole table from the origin edge's "
                f"{other} point, not its {chosen} point (see origin)")  # fmt: skip
        return other
    point = [round(float(v), 6) for v in points[chosen]]
    fail(
        "NX_VERIFICATION_FAILED",
        f"Hole positions in the table differ from the Hole features measured from the origin "
        f"edge's {chosen} point {point} (X along the view's horizontal axis, Y along its "
        "vertical axis)",
        hint=f"NX measured from the edge's {other} point: pass origin_point '{other}'"
        if other is not None
        else "Pick an origin edge that ends at the corner to measure from, with origin_point "
        "'start' or 'end' for that end (nx_find_geometry gives an edge's ends)",
        details={
            "check": "positions",
            "origin_point": chosen,
            "missing": [wanted[i] for i in missing][:20],
            "table": shown[:20],
        },
    )
