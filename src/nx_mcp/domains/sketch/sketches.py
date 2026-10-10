"""Sketch lifecycle and diagnostics (tool-surface 3.5): create, finish, read, conflicts.

NX 2406 findings kept here:

- A principal-plane sketch is placed on a coordinate system built from the exact
  frame (``SketchInPlaceBuilder2.Csystem``) and its frame is read back.
- On a face or datum plane without ``x_direction`` the sketch selects the target
  directly (``PlaneOption.Inferred``), which keeps it associative; NX 2406 ignores
  ``AxisReference`` in that mode, so an explicit x direction uses a coincident
  smart plane plus an axis and origin, still associative to the target.
- A path sketch needs the same work and display part, a validated builder and a
  model update before it can be activated.
- Committing a feature ends sketch mode silently on NX 2406, so every modeling
  tool requires that no sketch is active (``nx_finish_sketch`` first).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Doc,
    EdgeRef,
    FaceRef,
    Items,
    Name,
    Point3,
    Range,
    RefTo,
    SketchRef,
    Vec3,
    forbid_extra,
)
from nx_mcp.domains.sketch import common
from nx_mcp.domains.sketch.common import CurveRow, Frame, Status
from nx_mcp.kit import builders, curve, geom, readback, result, select
from nx_mcp.kit import sketch as ksketch

PRINCIPAL = {
    "xy": ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
    "xz": ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
    "yz": ([0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
}


class _Principal(TypedDict):
    principal: Annotated[
        Literal["xy", "xz", "yz"],
        Doc("Local x,y = world xy: X,Y; xz: X,Z (normal -Y); yz: Y,Z (normal X)"),
    ]


class PrincipalPlacement(_Principal, total=False):
    origin: Point3
    x_axis: Vec3


class _On(TypedDict):
    on: Annotated[str, RefTo("face", "datum_plane")]


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

Placement = Annotated[
    PrincipalPlacement | OnPlacement | PathPlacement,
    Doc("{principal, origin?, x_axis?} | {on, x_direction?} | {path, help_point, ...}"),
]


class SketchData(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    frame: Frame
    active: bool


def _create_summary(a, say):
    p = a.get("placement") or {}
    if "principal" in p:
        where = str(p["principal"]).upper() + " plane"
    elif "on" in p:
        where = "a face or datum plane"
    else:
        where = "an edge path"
    name = f' "{a["name"]}"' if a.get("name") else ""
    return f"Create sketch{name} on {where}"


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


def _on_target(tx, p):
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
    part, nx = tx.part, tx.nx
    with builders.build(tx, part.Sketches.CreateSketchInPlaceBuilder2, nx.Sketch.Null) as b:
        if x is None:
            b.PlaneOption = nx.Sketch.PlaneOption.Inferred
            b.PlaneOrFace.Value = obj
        else:
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
            b.AxisReference = builders.direction(tx, x, origin, "/placement/x_direction")
            b.OriginOption = nx.OriginMethod.SpecifyPoint
            b.SketchOrigin = builders.point(tx, origin)
        sketch = b.Commit()
    return sketch, origin, normal, x


def _on_path(tx, p):
    session, nx = tx.session, tx.nx
    part = tx.part
    if not session.IsBatch and session.Parts.Display != part:
        fail(
            "NX_WRONG_PART_CONTEXT",
            "A path sketch needs the same work and display part",
            hint="Activate the part as work and display part with nx_activate_part",
        )
    edges = select.edges(tx, p["path"], "/placement/path")
    face = None
    if p.get("orienting_face") is not None:
        face = select.one(tx, p["orienting_face"], "face", field="/placement/orienting_face")
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
    keywords=("sketch", "new sketch", "sketch on face", "sketch plane", "profile", "path sketch"),
    requires=("work_part", "no_active_sketch"),
    variants={"principal": PrincipalPlacement, "on": OnPlacement, "path": PathPlacement},
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
) -> SketchData:
    """Create a sketch on a principal plane, a planar face or datum plane, or normal to an edge
    path, and make it active. Later sketch tools use the returned frame; finish the sketch
    before modeling."""
    want = _principal_frame(placement) if "principal" in placement else None
    if want is not None:
        sketch = _on_principal(tx, want)
    elif "on" in placement:
        sketch, origin, normal, x = _on_target(tx, placement)
    else:
        sketch, edges = _on_path(tx, placement)
    got = common.frame_row(ksketch.frame(sketch))
    if want is not None:
        readback.expect_close(got["origin"], want["origin"], "origin")
        for axis in ("x_axis", "y_axis", "normal"):
            readback.expect_same_direction(got[axis], want[axis], axis)
    elif "on" in placement:
        readback.expect_parallel(got["normal"], normal, "normal")
        readback.expect_on_plane(got["origin"], origin, normal, "origin_on_plane")
        if x is not None:
            readback.expect_same_direction(got["x_axis"], x, "x_axis")
    else:
        _verify_path(tx, got, edges)
    if name:
        result.rename(tx, sketch, name)
    sketch.Activate(tx.nx.Sketch.ViewReorient.FalseValue)
    active = tx.session.ActiveSketch
    readback.expect(active is not None and int(active.Tag) == int(sketch.Tag), "active")
    return {**common.sketch_rows(tx, sketch), "frame": got, "active": True}


class FinishData(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    was_active: bool
    curve_count: int


@tool(
    effect="model",
    label="Finish sketch",
    summary="Finish sketch {sketch}",
    keywords=("finish sketch", "exit sketch", "deactivate sketch", "close sketch", "end sketch"),
    requires=("work_part",),
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3"},
)
def nx_finish_sketch(tx: Tx, *, sketch: SketchRef) -> FinishData:
    """Finish the active sketch and update the model so features can use it. A sketch that is
    not active is left as it is."""
    sk = ksketch.resolve(tx, sketch)
    active = tx.session.ActiveSketch
    if active is not None and int(active.Tag) != int(sk.Tag):
        fail(
            "NX_SKETCH_ACTIVE",
            "Another sketch is active",
            hint="Finish the active sketch first (nx_list_objects kind=sketch shows it)",
            field="/sketch",
        )
    nx = tx.nx
    if active is not None:
        with native(default="NX_UPDATE_FAILED"):
            sk.Deactivate(nx.Sketch.ViewReorient.TrueValue, nx.Sketch.UpdateLevel.Model)
        readback.expect(tx.session.ActiveSketch is None, "deactivated")
    return {
        **common.sketch_rows(tx, sk),
        "was_active": active is not None,
        "curve_count": len(ksketch.geometry(sk)),
    }


# ---------------------------------------------------------------------------
# Reading a sketch


class GeometryRow(CurveRow, total=False):
    part: Annotated[dict, Doc("start/end/center in work-part coordinates")]


class SketchInfo(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    active: bool
    frame: Frame
    curves: list[GeometryRow]
    loops: Annotated[list[dict], Doc("Connected curve chains (construction out): closed, curves")]
    constraints: list[common.ConstraintRow]
    status: Status


INCLUDE = ("geometry", "constraints", "status")


def _geometry(tx, sk, f):
    rows = []
    for c in ksketch.geometry(sk):
        if ksketch.curve_kind(tx, c) == "other":
            rows.append({"curve": tx.ref(c, "curve"), "type": "other"})
            continue
        row = common.curve_row(tx, f, c)
        row["part"] = {
            k: ksketch.to_part(f, row[k]) for k in ("start", "end", "center") if k in row
        }
        rows.append(row)
    return rows


def _constraints(tx, sk):
    by_constraint: dict[int, list[str]] = {}
    for c in ksketch.geometry(sk):
        for con in ksketch.attached(tx, sk, c):
            by_constraint.setdefault(int(con.Tag), []).append(tx.ref(c, "curve")["id"])
    return [
        common.constraint_row(tx, con, by_constraint.get(int(con.Tag), []))
        for con in ksketch.constraints(tx, sk)
    ]


def _solver_status(tx, sk):
    """Solver status of a sketch, activating it temporarily under a scratch mark."""
    active = tx.session.ActiveSketch
    if active is not None and int(active.Tag) != int(sk.Tag):
        fail(
            "NX_SKETCH_ACTIVE",
            "Another sketch is active; its status can be read, this one's cannot",
            hint="Finish the active sketch first",
        )
    with tx.scratch(), ksketch.editing(tx, sk) as editing:
        return common.status(tx, editing)


@tool(
    effect="read",
    label="Get sketch",
    summary="Read sketch {sketch}",
    keywords=("sketch info", "sketch curves", "sketch constraints", "degrees of freedom", "solver"),
    requires=("work_part",),
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
    """Read a sketch: frame, curves, constraints with dimension expressions, and solver status.
    Curves are in sketch-local and part coordinates; dof is NX's own indicator (NX 2406: curves
    not fully constrained)."""
    wanted = set(include or INCLUDE)
    sk = ksketch.resolve(tx, sketch)
    f = ksketch.frame(sk)
    active = tx.session.ActiveSketch
    data: SketchInfo = {
        **common.sketch_rows(tx, sk),
        "active": active is not None and int(active.Tag) == int(sk.Tag),
        "frame": common.frame_row(f),
    }
    if "geometry" in wanted:
        data["curves"] = _geometry(tx, sk, f)
        data["loops"] = ksketch.loops(
            [r for r in data["curves"] if r["type"] != "other" and not r.get("construction")]
        )
    if "constraints" in wanted:
        data["constraints"] = _constraints(tx, sk)
    if "status" in wanted:
        data["status"] = _solver_status(tx, sk)
    return data


# ---------------------------------------------------------------------------
# Conflict search


class Trial(TypedDict, total=False):
    constraint: result.Row
    state: str
    relieves_conflict: bool
    error: str


class ConflictData(TypedDict, total=False):
    sketch: result.Row
    state: str
    explicit_conflicts: list[dict]
    trials: list[Trial]
    checked: int
    total_constraints: int
    complete: bool
    relieving: list[str]


@tool(
    effect="read",
    label="Check sketch conflicts",
    summary="Find conflicts in sketch {sketch}",
    keywords=("over constrained", "conflicting constraints", "sketch conflict", "diagnose sketch"),
    requires=("work_part",),
    cost="heavy",
    example={"sketch": "s:3"},
)
def nx_check_sketch(
    tx: Tx,
    *,
    sketch: SketchRef,
    max_checks: Annotated[int, Range(ge=1, le=50), Doc("Constraints to try removing")] = 20,
) -> ConflictData:
    """Find the constraints behind an over- or inconsistently constrained sketch. Each is removed
    on trial (always undone) and the solver re-run; relieving lists those whose removal alone
    solves it."""
    sk = ksketch.resolve(tx, sketch)
    active = tx.session.ActiveSketch
    if active is not None and int(active.Tag) != int(sk.Tag):
        fail("NX_SKETCH_ACTIVE", "Another sketch is active", hint="Finish the active sketch first")
    session, nx = tx.session, tx.nx
    trials: list[Trial] = []
    with tx.scratch(), ksketch.editing(tx, sk) as editing:
        baseline = common.status(tx, editing)
        explicit = common.explicit_conflicts(tx, editing)
        constraints = ksketch.constraints(tx, editing)
        bad = baseline["state"] in ("over_constrained", "inconsistently_constrained")
        candidates = constraints if bad or explicit else []
        for i, con in enumerate(candidates[:max_checks]):
            tx.checkpoint(done=i, total=min(len(candidates), max_checks))
            trial: Trial = {
                "constraint": tx.ref(con, "sketch_constraint"),
                "relieves_conflict": False,
            }
            mark = session.SetUndoMark(nx.Session.MarkVisibility.Invisible, "NX MCP conflict trial")
            try:
                errors = editing.sketch.DeleteObjects([con])
                if errors is not None:
                    errors.Dispose()
                state = common.status(tx, editing)["state"]
                trial["state"] = state
                trial["relieves_conflict"] = state in (
                    "under_constrained",
                    "well_constrained",
                ) and not common.explicit_conflicts(tx, editing)
            except Exception as error:  # a trial failure is reported, not raised
                trial["error"] = str(error)
            finally:
                session.UndoToMark(mark, None)
                session.DeleteUndoMark(mark, None)
            trials.append(trial)
    return {
        "sketch": tx.ref(sk, "sketch"),
        "state": baseline["state"],
        "explicit_conflicts": explicit,
        "trials": trials,
        "checked": len(trials),
        "total_constraints": len(constraints),
        "complete": len(trials) == len(candidates) and not any("error" in t for t in trials),
        "relieving": [t["constraint"]["id"] for t in trials if t["relieves_conflict"]],
    }
