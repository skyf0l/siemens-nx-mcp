"""Sketch lifecycle and diagnostics (tool-surface 3.5): create, finish, read, conflicts.

NX 2406 findings kept here:

- A principal-plane sketch is placed on a coordinate system built from the exact
  frame (``SketchInPlaceBuilder2.Csystem``) and its frame is read back.
- On a face or datum plane the inferred placement (``PlaneOption.Inferred``) puts the
  origin at the face centre on some faces (a block's) and at a vertex on others (an
  extrude's), and ignores ``AxisReference``. The sketch therefore uses a coincident smart
  plane (still associative to the target) with an axis and an origin: the absolute
  origin dropped onto the plane, x the absolute X projected (``face_frame``).
- A path sketch needs the same work and display part, a validated builder and a
  model update before it can be activated.
- Committing a feature ends sketch mode silently on NX 2406, so every modeling
  tool requires that no sketch is active (``nx_finish_sketch`` first).
- NX has one active sketch per session: an unfinished sketch is parked at the end of
  ``nx_create_sketch`` (deactivated with a model update, ``kit.sketch.park``), so calls
  in other parts run. It stays its part's open sketch (``active`` and ``parked`` true):
  the sketch tools activate it by ref for each call, its part's model tools refuse as
  before, and ``nx_finish_sketch`` finishes it.
"""

from __future__ import annotations

import contextlib
import difflib
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core import parking
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    REF_PATTERN,
    Doc,
    EdgeRef,
    FaceRef,
    Items,
    Json,
    Name,
    Point3,
    Range,
    RefTo,
    SketchRef,
    Text,
    Vec3,
    forbid_extra,
)
from nx_mcp.domains.sketch import common
from nx_mcp.domains.sketch import curves as sketch_curves
from nx_mcp.domains.sketch.common import CurveRow, Frame, Status
from nx_mcp.kit import builders, curve, geom, readback, result, select, units
from nx_mcp.kit import sketch as ksketch

PRINCIPAL = {
    "xy": ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
    "xz": ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
    "yz": ([0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
}


# NX's standard views name the same planes: top = XY, front = XZ, right = YZ.
PLANE_ALIASES = {"top": "xy", "front": "xz", "right": "yz"}


class _Principal(TypedDict):
    principal: Annotated[
        str,
        Text(32),
        Doc("xy|top, xz|front or yz|right; local x,y = X,Y | X,Z (normal -Y) | Y,Z (normal X)"),
    ]


class PrincipalPlacement(_Principal, total=False):
    origin: Point3
    x_axis: Vec3


class _On(TypedDict):
    on: Annotated[
        str,
        RefTo("face", "datum_plane"),
        Doc(
            "Origin: part origin dropped onto it, not the face centre; x: X projected (Y if "
            "normal is X)"
        ),
    ]


class OnPlacement(_On, total=False):
    x_direction: Vec3


class _Path(TypedDict):
    path: Annotated[list[EdgeRef], Items(1, 50)]
    help_point: Point3


class PathPlacement(_Path, total=False):
    percent: Annotated[float, Range(ge=0, le=100)]
    orienting_face: FaceRef
    reverse_normal: bool
    reverse_axis: bool


forbid_extra(PrincipalPlacement, OnPlacement, PathPlacement)

# A plane name ('xy', any case) at the origin, or a face/datum plane ref.
PlaneOrRef = Annotated[str, Text(128)]

Placement = Annotated[
    PrincipalPlacement | OnPlacement | PathPlacement | PlaneOrRef,
    Doc("'xy' | {principal, origin?, x_axis?} | {on, x_direction?} | {path, help_point..}"),
]


def principal_name(value, field="/placement/principal"):
    """``xy``/``xz``/``yz`` from a natural spelling ('XY', 'xy plane', 'X-Y', 'Top')."""
    text = str(value).strip().lower()
    key = re.sub(r"[\s_\-]+", "", text)
    key = key.removesuffix("plane") or key
    key = PLANE_ALIASES.get(key, key)
    if key in PRINCIPAL:
        return key
    near = difflib.get_close_matches(key, [*PRINCIPAL, *PLANE_ALIASES], n=1, cutoff=0.5)
    guess = f"; did you mean {near[0]!r}?" if near else ""
    if key in ("yx", "zx", "zy"):
        guess = f"; use {key[::-1]!r} (local x is its first axis)"
    fail(
        "NX_INVALID_ARGUMENT",
        f"Unknown principal plane {value!r}{guess}",
        field=field,
        hint="Use 'xy', 'xz' or 'yz' (top, front, right), e.g. placement {principal: 'xy'}",
    )


def normal_placement(placement) -> dict:
    """The placement as one of the three object forms (pure; raises invalid_argument).

    A string is a principal plane name or a face/datum plane ref; a principal name is
    matched case-insensitively."""
    if isinstance(placement, str):
        if re.match(REF_PATTERN, placement.strip()):
            return {"on": placement.strip()}
        return {"principal": principal_name(placement, "/placement")}
    if "principal" in placement:
        return {**placement, "principal": principal_name(placement["principal"])}
    return placement


class SketchData(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    frame: Frame
    active: Annotated[bool, Doc("Open (not finished): sketch tools edit it by ref")]
    parked: Annotated[bool, Doc("Open but deactivated between calls; finish it to model")]
    curves: Annotated[list[result.Row], Doc("The created curves, in order (with curves)")]
    loops: Annotated[list[sketch_curves.Loop], Doc("Connected curve chains (with curves)")]


def _create_summary(a, say):
    p = a.get("placement") or {}
    if isinstance(p, str):
        p = {"on": p} if re.match(REF_PATTERN, p.strip()) else {"principal": p}
    if "principal" in p:
        try:
            where = principal_name(p["principal"]).upper() + " plane"
        except NXError:
            where = str(p["principal"]) + " plane"
    elif "on" in p:
        where = "a face or datum plane"
    else:
        where = "an edge path"
    name = f' "{a["name"]}"' if a.get("name") else ""
    n = len(a.get("curves") or [])
    with_curves = f" with {n} curve" + ("" if n == 1 else "s") if n else ""
    return f"Create sketch{name} on {where}{with_curves}"


def _matrix(tx, x, y, z):
    m = tx.nx.Matrix3x3()
    m.Xx, m.Xy, m.Xz = (float(v) for v in x)
    m.Yx, m.Yy, m.Yz = (float(v) for v in y)
    m.Zx, m.Zy, m.Zz = (float(v) for v in z)
    return m


def _principal_frame(p):
    """Requested frame of a principal placement (pure; raises invalid_argument)."""
    x0, y0 = PRINCIPAL[p["principal"]]
    normal = geom.cross(x0, y0)
    origin = [float(v) for v in p.get("origin") or (0.0, 0.0, 0.0)]
    if p.get("x_axis") is None:
        return {"origin": origin, "x_axis": list(x0), "y_axis": list(y0), "normal": normal}
    x = geom.unit(p["x_axis"], "/placement/x_axis")
    if abs(geom.dot(x, normal)) > 1e-9:
        fail(
            "NX_INVALID_ARGUMENT",
            f"x_axis must lie in the {p['principal']} plane",
            field="/placement/x_axis",
        )
    return {"origin": origin, "x_axis": x, "y_axis": geom.cross(normal, x), "normal": normal}


def _on_principal(tx, want):
    part, nx = tx.part, tx.nx
    with native(default="NX_BUILD_FAILED"):
        csys = part.CoordinateSystems.CreateCoordinateSystem(
            nx.Point3d(*want["origin"]),
            _matrix(tx, want["x_axis"], want["y_axis"], want["normal"]),
            False,
        )
    with builders.build(tx, part.Sketches.CreateSketchInPlaceBuilder2, nx.Sketch.Null) as b:
        b.Csystem = csys
        return b.Commit()


def face_frame(origin, normal, x=None):
    """``(origin, x)`` of a sketch on the plane through ``origin`` with unit ``normal``:
    the part's absolute origin dropped onto the plane, and ``x`` (default the absolute X
    projected onto the plane, the absolute Y on a plane normal to X)."""
    at = geom.scale(normal, geom.dot(origin, normal))
    if x is None:
        base = [1.0, 0.0, 0.0] if abs(normal[0]) < 0.9 else [0.0, 1.0, 0.0]
        x = geom.unit(geom.sub(base, geom.scale(normal, geom.dot(base, normal))))
    return at, x


def _on_check(tx, p):
    """``(obj, origin, normal, x)`` of an ``on`` placement: the planar face or datum plane
    and the sketch x direction, refused before anything is built."""
    obj, origin, normal = select.planar(tx, p["on"], "/placement/on")
    x = None
    if p.get("x_direction") is not None:
        x = geom.unit(p["x_direction"], "/placement/x_direction")
        if abs(geom.dot(x, normal)) > 1e-6:
            fail(
                "NX_INVALID_ARGUMENT",
                "x_direction must lie in the target plane",
                field="/placement/x_direction",
            )
    return obj, origin, normal, x


def _on_target(tx, checked):
    """A sketch on a planar face or datum plane (``_on_check``), at a predictable frame
    (``face_frame``): NX's own inferred origin is the face centre on some faces and a
    vertex on others."""
    obj, origin, normal, x = checked
    at, x = face_frame(origin, normal, x)
    part, nx = tx.part, tx.nx
    with builders.build(tx, part.Sketches.CreateSketchInPlaceBuilder2, nx.Sketch.Null) as b:
        # A coincident smart plane keeps the sketch associative to the target; NX 2406
        # ignores AxisReference and SketchOrigin on an Inferred plane.
        plane = part.Planes.CreatePlane(
            nx.Point3d(*origin),
            nx.Vector3d(*normal),
            nx.SmartObject.UpdateOption.WithinModeling,
        )
        plane.SetMethod(nx.PlaneTypes.MethodType.Coincident)
        plane.SetGeometry([obj])
        plane.Evaluate()
        b.PlaneOption = nx.Sketch.PlaneOption.NewPlane
        b.PlaneReference = plane
        b.AxisReference = builders.direction(tx, x, at, "/placement/x_direction")
        b.OriginOption = nx.OriginMethod.SpecifyPoint
        b.SketchOrigin = builders.point(tx, at)
        sketch = b.Commit()
    return sketch, at, normal, x


def _path_check(tx, p):
    """``(edges, orienting face or None)`` of a path placement, refused before anything is
    built."""
    session = tx.session
    if not session.IsBatch and session.Parts.Display != tx.part:
        fail(
            "NX_WRONG_PART_CONTEXT",
            "A path sketch needs the same work and display part",
            hint="Activate the part as work and display part with nx_activate_part",
        )
    edges = select.edges(tx, p["path"], "/placement/path")
    face = None
    if p.get("orienting_face") is not None:
        face = select.one(tx, p["orienting_face"], "face", field="/placement/orienting_face")
    return edges, face


def _on_path(tx, p, checked):
    session, nx = tx.session, tx.nx
    part = tx.part
    edges, face = checked
    along = nx.SketchAlongPathBuilder
    with builders.build(tx, part.Sketches.CreateSketchAlongPathBuilder, None) as b:
        section = b.Section
        section.Clear()
        section.AddToSection(
            [part.ScRuleFactory.CreateRuleEdgeDumb(edges)],
            edges[0],
            None,
            None,
            nx.Point3d(*(float(v) for v in p["help_point"])),
            nx.Section.Mode.Create,
            False,
        )
        b.PlaneOrientation = along.PlaneOrientationType.NormalToPath
        b.SketchOrient = getattr(
            along.SketchOrientationType, "RelativeToFace" if face else "Automatic"
        )
        if face is not None:
            builders.fill(tx, b.OrientingFace, [face], "Face")
        location = b.PlaneLocation
        location.IsParameterUsed = False
        location.IsPercentUsed = True
        location.Expression.RightHandSide = repr(float(p.get("percent", 0.0)))
        location.Update(nx.GeometricUtilities.OnPathDimensionBuilder.UpdateReason.Path)
        b.ReversePlaneNormal = bool(p.get("reverse_normal", False))
        b.ReverseAxis = bool(p.get("reverse_axis", False))
        if not b.Validate():
            fail("NX_BUILD_FAILED", "NX rejected the path sketch inputs", field="/placement")
        sketch = b.Commit()
    with native(default="NX_UPDATE_FAILED"):
        errors = session.UpdateManager.DoUpdate(tx.mark)
    if errors:
        fail("NX_UPDATE_FAILED", f"The model update after the path sketch reported {errors} errors")
    return sketch, edges


def _verify_path(tx, got, edges):
    """The sketch origin lies on the path and its normal follows the path tangent."""
    best = None
    for edge in edges:
        foot, tangent = curve.closest(tx, edge, got["origin"])
        gap = geom.dist(foot, got["origin"])
        if best is None or gap < best[0]:
            best = (gap, tangent)
    readback.expect_close(best[0], 0.0, "origin_on_path", tol=geom.tol(got["origin"]) * 10)
    readback.expect_parallel(got["normal"], best[1], "normal_along_path")


@tool(
    effect="model",
    label="Create sketch",
    summary=_create_summary,
    keywords=(
        "sketch",
        "new sketch",
        "sketch on face",
        "sketch plane",
        "profile",
        "path sketch",
        "rectangle sketch",
    ),
    requires=("work_part", "no_active_sketch"),
    variants={"principal": PrincipalPlacement, "on": OnPlacement, "path": PathPlacement},
    display_bound=("/placement/path",),  # a path sketch needs its part displayed
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"placement": {"principal": "xy"}, "name": "PROFILE"},
)
def nx_create_sketch(
    tx: Tx,
    *,
    placement: Placement,
    name: Name | None = None,
    curves: Annotated[
        list[Annotated[dict, Json(max_keys=8, max_chars=8000)]],
        Items(1, 100),
        Doc("Profile: nx_create_sketch_curves items, e.g. {type: polyline, points}"),
    ]
    | None = None,
    finish: Annotated[bool, Doc("Finish the sketch after the curves, ready for modeling")] = False,
) -> SketchData:
    """Create a sketch on a principal plane, a planar face or datum plane, or normal to an edge
    path. It stays open (parked: deactivated between calls) for the sketch tools; curves and
    finish make a profile in one call (loops reports closure): each closed loop is a region,
    a loop inside another makes a hollow profile. Finish before modeling."""
    where = normal_placement(placement)
    plans = sketch_curves.check_curves(curves) if curves else []
    want = _principal_frame(where) if "principal" in where else None
    with tx.checks():  # refusals before anything is built: not_started
        if name:
            _require_new_name(tx, name)
        checked: tuple | None = None
        if "on" in where:
            checked = _on_check(tx, where)
        elif want is None:
            checked = _path_check(tx, where)
    if want is not None:
        sketch = _on_principal(tx, want)
    elif "on" in where:
        sketch, origin, normal, x = _on_target(tx, checked)
    else:
        sketch, edges = _on_path(tx, where, checked)
    got = common.frame_row(ksketch.frame(sketch))
    if want is not None:
        readback.expect_close(got["origin"], want["origin"], "origin")
        for axis in ("x_axis", "y_axis", "normal"):
            readback.expect_same_direction(got[axis], want[axis], axis)
    elif "on" in where:
        readback.expect_parallel(got["normal"], normal, "normal")
        readback.expect_close(got["origin"], origin, "origin")
        readback.expect_same_direction(got["x_axis"], x, "x_axis")
    else:
        _verify_path(tx, got, edges)
    if name:
        with native({SKETCH_NAME_EXISTS: "NX_NAME_CONFLICT"}, default="NX_BUILD_FAILED"):
            result.rename(tx, sketch, name)
    sketch.Activate(tx.nx.Sketch.ViewReorient.FalseValue)
    active = tx.session.ActiveSketch
    readback.expect(active is not None and int(active.Tag) == int(sketch.Tag), "active")
    data: SketchData = {**common.sketch_rows(tx, sketch), "frame": got, "active": True}
    if plans:
        try:
            made = sketch_curves.add_curves(tx, sketch, plans)
        except BaseException:
            _leave(tx, sketch)  # the kernel then undoes the whole call
            raise
        data["curves"] = [r["curve"] for r in made["curves"]]
        data["loops"] = made["loops"]
    if finish:
        _deactivate(tx, sketch)
        data["active"] = False
    else:  # open, but not active in NX between calls: other parts' calls run
        with native(default="NX_UPDATE_FAILED"):
            ksketch.park(tx, sketch)
        readback.expect(tx.session.ActiveSketch is None, "parked")
        data["parked"] = True
    return data


# NX 2406: "Sketch already exists with specified name" when a sketch is named after another.
SKETCH_NAME_EXISTS = 1720003


def _require_new_name(tx, name):
    """Refuse a name another sketch of the part has (NX compares without case)."""
    for other in tx.part.Sketches:
        if str(getattr(other, "Name", "")).upper() == str(name).upper():
            fail(
                "NX_NAME_CONFLICT",
                f"The part already has a sketch named {other.Name}",
                field="/name",
                hint="Choose another name, or omit name (NX numbers sketches SKETCH_000...); "
                "details.existing is that sketch",
                details={"existing": tx.ref(other, "sketch")["id"]},
            )


def _deactivate(tx, sk):
    """Finish the active sketch ``sk`` with a model update and read it back. The view is
    reoriented only when the sketch's part is displayed: a background part's finish
    never turns the shown part's view (the tools that activate a sketch display its part,
    ``kit.sketch``)."""
    nx = tx.nx
    shown = ksketch.same(tx.session.Parts.Display, getattr(sk, "OwningPart", None))
    reorient = nx.Sketch.ViewReorient.TrueValue if shown else nx.Sketch.ViewReorient.FalseValue
    with native(default="NX_UPDATE_FAILED"):
        sk.Deactivate(reorient, nx.Sketch.UpdateLevel.Model)
    readback.expect(tx.session.ActiveSketch is None, "deactivated")


def _leave(tx, sk):
    """Leave sketch mode before a failed call's rollback (best effort)."""
    try:
        if tx.session.ActiveSketch is not None:
            sk.Deactivate(tx.nx.Sketch.ViewReorient.FalseValue, tx.nx.Sketch.UpdateLevel.SketchOnly)
    except Exception:  # the rollback still runs; NX undo also leaves the sketch
        pass


class FinishData(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    was_active: Annotated[bool, Doc("It was open (active or parked)")]
    curve_count: int


def _finish_summary(a, say):
    return f"Finish sketch {say('sketch')}" if a.get("sketch") else "Finish the active sketch"


@tool(
    effect="model",
    label="Finish sketch",
    summary=_finish_summary,
    keywords=("finish sketch", "exit sketch", "deactivate sketch", "close sketch", "end sketch"),
    requires=("work_part",),
    display_bound=True,  # activates the sketch: NX 2406 faults outside the shown part
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3"},
)
def nx_finish_sketch(
    tx: Tx,
    *,
    sketch: SketchRef | None = None,
) -> FinishData:
    """Finish a sketch (default: the part's open one) and update the model so features can use
    it. A finished sketch is left as it is."""
    with tx.checks():
        active = ksketch.open_sketch(tx)
        if sketch is None:
            if active is None:
                return {"was_active": False}
            sk = active
        else:
            sk = ksketch.resolve(tx, sketch)
        if active is not None and int(active.Tag) != int(sk.Tag):
            fail(
                "NX_SKETCH_ACTIVE",
                "Another sketch is active",
                hint="Finish the active sketch first (nx_list_objects kind=sketch shows it)",
                field="/sketch",
            )
    if active is not None:
        if tx.session.ActiveSketch is None:  # parked: open it again to finish it
            with native(default="NX_UPDATE_FAILED"):
                sk.Activate(tx.nx.Sketch.ViewReorient.FalseValue)
        _deactivate(tx, sk)
        parking.unpark(tx.services, tx.part)
        _reopen_on_undo(tx, sk)
    return {
        **common.sketch_rows(tx, sk),
        "was_active": active is not None,
        "curve_count": len(ksketch.geometry(sk)),
    }


def _reopen_on_undo(tx, sk) -> None:
    """Mark the call's undo-history entry (``parked_sketch``): NX's undo of this finish
    leaves the sketch deactivated, as it was while parked, and the undo parks it again
    (``domains/session/history.py``). In a batch the batch's entry carries it; the
    first finish of the call wins (the state before the call)."""
    root = tx
    while getattr(root, "parent", None) is not None:
        root = root.parent
    note = dict(root.history_note or {})
    note.setdefault("parked_sketch", tx.ref(sk, "sketch")["id"])
    root.history_note = note


# ---------------------------------------------------------------------------
# Reading a sketch


class GeometryRow(CurveRow, total=False):
    part: Annotated[dict, Doc("start/end/center in work-part coordinates")]
    constraints: Annotated[int, Doc("Constraints and dimensions on this curve")]
    projected: Annotated[bool, Doc("Projected or intersection curve (follows the model)")]
    source: Annotated[str, Doc("projected: the model edge, curve or face it comes from")]
    dof: Annotated[
        int,
        Doc(
            "Ways it can still move (0: fully fixed); freedom shared by joined curves counts on each"
        ),
    ]


class SketchInfo(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    active: Annotated[bool, Doc("Open (not finished)")]
    parked: Annotated[bool, Doc("Open but deactivated between calls")]
    frame: Frame
    curves: list[GeometryRow]
    loops: Annotated[list[dict], Doc("Connected curve chains (construction out): closed, curves")]
    constraints: list[common.ConstraintRow]
    status: Status


INCLUDE = ("geometry", "constraints", "status")


def _geometry(tx, sk, f, found):
    """Curve rows with their constraint count and, when known, their remaining DOF."""
    per_curve = found["curves"] if found else {}
    included = _included_curves(tx, sk, f)
    rows = []
    for c in ksketch.geometry(sk):
        count = len(ksketch.attached(tx, sk, c))
        if ksketch.curve_kind(tx, c) == "other":
            row = {"curve": tx.ref(c, "curve"), "type": "other", "constraints": count}
        else:
            row = common.curve_row(tx, f, c)
            row["part"] = {
                k: ksketch.to_part(f, row[k]) for k in ("start", "end", "center") if k in row
            }
            row["constraints"] = count
        if int(c.Tag) in per_curve:
            row["dof"] = per_curve[int(c.Tag)]
        row.update(included.get(int(c.Tag)) or _included(tx, sk, c))
        rows.append(row)
    return rows


# NX 2406 features that make sketch curves from model geometry; their curves are not
# "included geometry" (Sketch.IsIncludedGeometry is False for them).
PROJECTION_FEATURES = ("CPROJ", "Intersection Curve")


def _included_curves(tx, sk, f) -> dict[int, dict]:
    """``{curve tag: {"projected": True, "source": ref}}`` for the curves the sketch's
    projection (``CPROJ``) and intersection features made. The source is the projected
    edge or curve (the feature's section), or the intersected face (read from the
    intersection's builder while the sketch is active, never with another sketch open);
    it is left out when it cannot be told."""
    s = sk.sketch if isinstance(sk, ksketch.Editing) else sk
    curves = {int(c.Tag): c for c in ksketch.geometry(s)}
    out: dict[int, dict] = {}
    if not curves:
        return out
    pending = []
    part = getattr(s, "OwningPart", None) or tx.part
    for feature in part.Features:
        try:
            kind = str(feature.FeatureType)
            if kind not in PROJECTION_FEATURES:
                continue
            made = [curves[int(e.Tag)] for e in feature.GetEntities() if int(e.Tag) in curves]
        except Exception:  # NX cannot describe this feature: not a projection of ours
            continue
        for c in made:
            out[int(c.Tag)] = {"projected": True}
        if made and kind == "CPROJ":
            _assign_sources(tx, f, made, _section_sources(feature), out)
        elif made:
            pending.append((feature, made))
    if pending:
        _intersection_sources(tx, part, s, pending, out)
    return out


def _section_sources(feature) -> list:
    """The edges and curves of a projection feature's sections, in order."""
    found: list = []
    try:
        for section in feature.GetSections():
            for data in section.GetSectionData():
                for element in data.GetSectionElementsData():
                    obj = element.GetSectionElementData()[0]
                    if obj is not None and all(int(o.Tag) != int(obj.Tag) for o in found):
                        found.append(obj)
    except Exception:  # NX cannot read the section back
        return []
    return found


def _assign_sources(tx, f, made, sources, out) -> None:
    """Each curve's source: the only one, else the one whose projection lies on it."""
    points: list = []
    tol = 0.0
    if len(sources) == 1:
        rows = [(c, None) for c in made]
    else:
        tol = units.part_tolerance(tx, 0.01)
        try:
            rows = [(c, ksketch.read(tx, f, c)) for c in made]
            points = [(obj, common.samples(tx, f, obj)) for obj in sources]
        except Exception:  # geometry NX cannot evaluate: the sources are left out
            return
    for c, row in rows:
        source = (
            sources[0]
            if row is None
            else next((obj for obj, p in points if common.on_row(row, p, tol)), None)
        )
        if source is not None:
            with contextlib.suppress(Exception):  # another part's geometry has no ref here
                out[int(c.Tag)]["source"] = tx.ref(source)["id"]


def _intersection_sources(tx, part, s, pending, out) -> None:
    """The faces of the intersection features (``pending``: ``(feature, curves)``); NX
    opens their builders only with the sketch active, so a parked sketch is activated
    under a scratch mark; with another sketch open the faces are left out."""
    if _other_active(tx, s):
        return
    with tx.scratch(), ksketch.editing(tx, s):
        factory = part.Sketches.CreateIntersectionCurveBuilder
        for feature, made in pending:
            try:
                b = factory(feature)
                try:
                    faces = list(b.FaceCollector.GetObjects())
                finally:
                    b.Destroy()
                source = tx.ref(faces[0])["id"] if len(faces) == 1 else None
            except Exception:  # the face is a courtesy: the curves stay flagged
                continue
            for c in made:
                if source is not None:
                    out[int(c.Tag)]["source"] = source


def _included(tx, sk, curve) -> dict:
    """``{"projected": True, "source": ref}`` for a curve NX reports as included geometry
    (``Sketch.IsIncludedGeometry``), else {}."""
    s = sk.sketch if isinstance(sk, ksketch.Editing) else sk
    ask = getattr(s, "IsIncludedGeometry", None)
    try:
        if ask is None or not bool(ask(curve)):
            return {}
    except Exception:  # NX cannot say for this object
        return {}
    out: dict = {"projected": True}
    try:
        parent = s.GetParentOfIncludedGeometry(curve)
        if parent is not None:
            out["source"] = tx.ref(parent)["id"]
    except Exception:  # the parent cannot be referenced (another part's geometry)
        pass
    return out


def _constraints(tx, sk):
    by_constraint: dict[int, list[str]] = {}
    for c in ksketch.geometry(sk):
        for con in ksketch.attached(tx, sk, c):
            by_constraint.setdefault(int(con.Tag), []).append(tx.ref(c, "curve")["id"])
    tags = ksketch.tags(sk)
    return [
        common.constraint_row(tx, con, by_constraint.get(int(con.Tag), []), tags)
        for con in ksketch.constraints(tx, sk)
    ]


def _other_active(tx, sk) -> bool:
    active = ksketch.open_sketch(tx)
    return active is not None and int(active.Tag) != int(sk.Tag)


def _solver_status(tx, sk):
    """Solver status and checked DOF analysis of a sketch, activating it temporarily
    under a scratch mark."""
    if _other_active(tx, sk):
        fail(
            "NX_SKETCH_ACTIVE",
            "Another sketch of the part is open; its status can be read, this one's cannot",
            hint="Finish the open sketch (details.open_sketch) first, or ask for geometry and "
            "constraints only",
            details=ksketch.open_details(tx, ksketch.open_sketch(tx)),
        )
    with tx.scratch(), ksketch.editing(tx, sk) as editing:
        return common.status_row(tx, editing, ksketch.status(tx, editing))


def _activates(services, part, args) -> bool:
    """Geometry and status activate the sketch (solver), constraints alone do not."""
    return bool(set(args.get("include") or INCLUDE) & {"geometry", "status"})


@tool(
    switch="work",
    effect="read",
    label="Get sketch",
    summary="Read sketch {sketch}",
    keywords=("sketch info", "sketch curves", "sketch constraints", "degrees of freedom", "solver"),
    requires=("work_part",),
    display_bound=(_activates,),  # activates the sketch for geometry and status: shown
    cost="light",
    example={"sketch": "s:3", "include": ["geometry", "status"]},
)
def nx_get_sketch(
    tx: Tx,
    *,
    sketch: SketchRef,
    include: Annotated[
        list[Literal["geometry", "constraints", "status"]],
        Items(1, 3),
        Doc("Sections to return (default all)"),
    ]
    | None = None,
) -> SketchInfo:
    """Read a sketch: frame, curves, constraints with dimension expressions, solver status.
    Curves are in sketch-local and part coordinates with dof (remaining degrees of freedom,
    0: fully constrained); constraints name the point of each curve they use."""
    wanted = set(include or INCLUDE)
    sk = ksketch.resolve(tx, sketch)
    f = ksketch.frame(sk)
    active = ksketch.open_sketch(tx)
    data: SketchInfo = {
        **common.sketch_rows(tx, sk),
        "active": active is not None and int(active.Tag) == int(sk.Tag),
        "frame": common.frame_row(f),
    }
    if data["active"] and tx.session.ActiveSketch is None:
        data["parked"] = True
    found = None
    other = _other_active(tx, sk)
    if other and wanted != {"status"}:  # the solver status needs this sketch activated
        if "status" in wanted:
            wanted.discard("status")
            open_ = ksketch.open_details(tx, ksketch.open_sketch(tx))
            tx.warn(
                "NX_NOTE",
                f"status and per-curve dof left out: another sketch of the part is open "
                f"({open_.get('open_sketch_name', '')} {open_.get('open_sketch', '')}); "
                "finish it to read them",
            )
    elif "status" in wanted:
        data["status"], found = _solver_status(tx, sk)
    elif "geometry" in wanted:
        found = _solver_status(tx, sk)[1]  # per-curve DOF needs NX's count to check
    if "geometry" in wanted:
        data["curves"] = _geometry(tx, sk, f, found)
        data["loops"] = ksketch.loops(
            [r for r in data["curves"] if r["type"] != "other" and not r.get("construction")]
        )
    if "constraints" in wanted:
        data["constraints"] = _constraints(tx, sk)
    return data


# ---------------------------------------------------------------------------
# Conflict search


class Trial(TypedDict, total=False):
    constraint: result.Row
    state: str
    relieves_conflict: bool
    error: str


class FreeCurve(TypedDict):
    curve: str
    dof: Annotated[int | None, Doc("Ways it can still move; None: not computable")]


class ConflictData(TypedDict, total=False):
    sketch: result.Row
    state: str
    note: str
    explicit_conflicts: list[dict]
    trials: list[Trial]
    checked: int
    total_constraints: int
    complete: bool
    relieving: list[str]
    dof: Annotated[int | None, Doc("Under-constrained: remaining degrees of freedom")]
    free: Annotated[list[FreeCurve], Doc("Under-constrained: the curves that can still move")]


def _free_curves(tx, sk, found) -> list[FreeCurve]:
    """Curves that can still move: by the DOF analysis, else those without constraints."""
    out: list[FreeCurve] = []
    for c in ksketch.geometry(sk):
        if ksketch.curve_kind(tx, c) == "other":
            continue
        if found is not None:
            n = found["curves"].get(int(c.Tag), 0)
            if n > 0:
                out.append({"curve": tx.ref(c, "curve")["id"], "dof": n})
        elif not ksketch.attached(tx, sk, c):
            out.append({"curve": tx.ref(c, "curve")["id"], "dof": None})
    return out


@tool(
    effect="read",
    label="Check sketch conflicts",
    summary="Find conflicts in sketch {sketch}",
    keywords=(
        "over constrained",
        "under constrained",
        "conflicting constraints",
        "diagnose sketch",
        "check sketch",
        "fully constrained",
        "free curves",
        "constraint conflict",
    ),
    requires=("work_part",),
    display_bound=True,  # activates the sketch: NX 2406 faults outside the shown part
    cost="heavy",
    example={"sketch": "s:3"},
)
def nx_check_sketch(
    tx: Tx,
    *,
    sketch: SketchRef,
    max_checks: Annotated[int, Range(ge=1, le=50), Doc("Constraints to try removing")] = 20,
) -> ConflictData:
    """Diagnose a sketch's constraints. Under-constrained: free lists the curves that can still
    move (dimension or constrain them). Over- or inconsistently constrained (sketches opened
    from files: the sketch tools refuse conflicts): each constraint is removed on trial (always
    undone) and relieving lists those whose removal alone solves it."""
    sk = ksketch.resolve(tx, sketch)
    if _other_active(tx, sk):
        fail(
            "NX_SKETCH_ACTIVE",
            "Another sketch is active",
            hint="Finish the active sketch (details.open_sketch) first",
            details=ksketch.open_details(tx, ksketch.open_sketch(tx)),
        )
    trials: list[Trial] = []
    free: list[FreeCurve] | None = None
    dof = None
    with tx.scratch(), ksketch.editing(tx, sk) as editing:
        native = ksketch.status(tx, editing)
        under = native["status"] == "UnderConstrained"
        baseline, found = common.status_row(tx, editing, native, dof=under)
        explicit = common.explicit_conflicts(tx, editing)
        constraints = ksketch.constraints(tx, editing)
        bad = baseline["state"] in ("over_constrained", "inconsistently_constrained")
        candidates = constraints if bad or explicit else []
        for i, con in enumerate(candidates[:max_checks]):
            tx.checkpoint(done=i, total=min(len(candidates), max_checks))
            trial: Trial = {"constraint": tx.ref(con, "sketch_constraint")}
            trial.update(common.trial(tx, editing, con))  # type: ignore[typeddict-item]
            trials.append(trial)
        if under and not candidates:
            free, dof = _free_curves(tx, editing, found), baseline["dof"]
    out: ConflictData = {
        "sketch": tx.ref(sk, "sketch"),
        "state": baseline["state"],
        "explicit_conflicts": explicit,
        "trials": trials,
        "checked": len(trials),
        "total_constraints": len(constraints),
        "complete": len(trials) == len(candidates) and not any("error" in t for t in trials),
        "relieving": [t["constraint"]["id"] for t in trials if t.get("relieves_conflict")],
    }
    if free is not None:
        out["free"], out["dof"] = free, dof
        count = baseline["free_curves"] if baseline["free_curves"] is not None else len(free)
        out["note"] = (
            f"The sketch is under-constrained (NX counts {count} curves not fully "
            "constrained): add dimensions or constraints to the curves in free"
            + ("" if found is not None else " (free lists only those without any constraint)")
        )
    elif not candidates:
        out["note"] = (
            f"The sketch is {baseline['state']}, not over-constrained and without conflicts: "
            "nothing to relieve"
        )
    return out
