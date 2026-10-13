"""Add supports, loads, keep-in/out regions and shape constraints to a topology study.

Each call edits the study's feature in NX's Topology Optimization task and commits one
builder: ``AnalysisConstraintBuilder`` (fixed, pinned), ``AnalysisLoadBuilder`` (force
by vector in N, pressure in MPa), ``ConstructionBodyBuilder`` (keep-in / keep-out: faces
grown into a region ``thickness`` deep, or separate bodies) or ``ShapeConstraintBuilder``
(planar symmetry, minimum member size, extrude along a vector). Inch parts get lbf and
psi expressions holding the same values.

Load cases are NX subcases (``CreateSubcaseManager``: its ``SubcaseList`` starts with the
study's subcases; ``CreateSubcase`` + ``Append``, renaming an item renames the subcase). A
support or load without ``load_case`` joins every existing case; with one it joins that case
only. A new case starts with the supports every case shares; the first one named takes over
NX's "Default Subcase" while that holds no load.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    BodyRef,
    Direction,
    Doc,
    FaceRef,
    FeatureRef,
    Items,
    Length,
    Range,
    RefTo,
    Text,
)
from nx_mcp.domains.optimization import common
from nx_mcp.kit import builders, readback, result, select, units

Kind = Literal[
    "fixed", "pinned", "force", "pressure", "keep_in", "keep_out", "symmetry", "member_size",
    "extrude",
]  # fmt: skip
SymmetryPlane = Annotated[str, RefTo("datum_plane", "face", doc="Symmetry plane ref")]
DEFAULT_DEPTH_MM = 5.0  # region depth grown from faces when none is given
SUPPORTS = {"fixed": "Fixed", "pinned": "Pinned"}
REGIONS = {"keep_in": "KeepIn", "keep_out": "KeepOut"}


class ConditionData(TypedDict):
    study: result.Row
    kind: str
    faces: int
    supports: int
    loads: int
    load_case: str | None


def _check(kind, faces, bodies, thickness, magnitude, direction, planes, size, load_case):
    """Cross-field rules the schema cannot say, before NX is touched."""
    takes = {
        "faces": kind in (*SUPPORTS, "force", "pressure", *REGIONS),
        "bodies": kind in REGIONS,
        "thickness": kind in REGIONS,
        "magnitude": kind in ("force", "pressure"),
        "direction": kind in ("force", "extrude"),
        "planes": kind == "symmetry",
        "size": kind == "member_size",
        "load_case": kind in (*SUPPORTS, "force", "pressure"),
    }
    given = {
        "faces": faces, "bodies": bodies, "thickness": thickness, "magnitude": magnitude,
        "direction": direction, "planes": planes, "size": size, "load_case": load_case,
    }  # fmt: skip
    for field, value in given.items():
        if value is not None and not takes[field]:
            fail("NX_INVALID_ARGUMENT", f"{kind} takes no {field}", field="/" + field)
    if kind in REGIONS:
        if (faces is None) == (bodies is None):
            fail("NX_INVALID_ARGUMENT", f"{kind} takes faces or bodies (one)", field="/faces")
        if bodies is not None and thickness is not None:
            fail("NX_INVALID_ARGUMENT", "thickness applies to faces", field="/thickness")
    needs = {"force": ("direction", direction), "extrude": ("direction", direction),
             "symmetry": ("planes", planes), "member_size": ("size", size)}  # fmt: skip
    if kind in needs and needs[kind][1] is None:
        fail("NX_INVALID_ARGUMENT", f"{kind} needs {needs[kind][0]}", field="/" + needs[kind][0])
    for field, value in (("thickness", thickness), ("size", size)):
        if value is not None and not float(value) > 0:
            fail("NX_INVALID_ARGUMENT", f"{field} must be > 0", field="/" + field)


def committed(b):
    """What a study builder made: NX 2406 commits supports, mass targets and shape
    constraints but returns None; then the builder's committed objects, else True."""
    made = b.commit()
    if made is None:
        objects = list(b.GetCommittedObjects() or []) if b.has("GetCommittedObjects") else []
        made = objects[0] if objects else True
    return made


def _named(study, name):
    key = name.strip().casefold()
    return [c for c in common.all_of(study, "GetAllSubcases") if str(c.Name).casefold() == key]


def _new_case(tx, study, name):
    """Make load case ``name``: NX's default case while it holds no load, else a new case
    with the supports every case shares."""
    cases = common.all_of(study, "GetAllSubcases")
    loads = {int(x.Tag) for x in common.all_of(study, "GetAllAnalysisLoads")}
    refs = [list(c.GetReferences() or []) for c in cases]
    with builders.build(tx, study.CreateSubcaseManager) as b:
        items = b.SubcaseList
        if len(cases) == 1 and not any(int(x.Tag) in loads for x in refs[0]):
            items.FindItem(0).Name = name
        else:
            tags = [{int(y.Tag) for y in r} for r in refs]
            shared = [x for x in refs[0] if int(x.Tag) not in loads
                      and all(int(x.Tag) in t for t in tags)]  # fmt: skip
            item = b.CreateSubcase()
            item.Name = name
            item.IncludedInOptimization = True
            for x in shared:
                item.AddReference(x)
            items.Append(item)  # NX 2406: CreateSubcase does not list the item
        b.commit()
    made = _named(study, name)
    readback.expect(len(made) == 1, "load_case", f"NX made no load case {name!r}")
    return made[0]


def _holds(case, item) -> bool:
    return any(int(x.Tag) == int(item.Tag) for x in list(case.GetReferences() or []))


def _join(tx, study, made, load_case):
    """A new support or load joins its load case, else every case (NX solves only the
    referenced ones). NX 2406 puts a new one in the active case itself: a named case takes
    it out of the others first."""
    cases = common.all_of(study, "GetAllSubcases")
    if load_case is None:
        for case in cases:
            if not _holds(case, made):
                case.AddReference(made)
        return
    for case in cases:
        if _holds(case, made) and str(case.Name).casefold() != load_case.strip().casefold():
            case.RemoveReference(made)
    found = _named(study, load_case)
    target = found[0] if found else _new_case(tx, study, load_case)
    if not _holds(target, made):
        target.AddReference(made)


def _support(tx, study, kind, faces):
    DS = common.ds(tx)
    with builders.build(tx, study.CreateAnalysisConstraintBuilder, DS.AnalysisConstraint.Null) as b:
        b.Type = getattr(DS.AnalysisConstraintBuilder.ConstraintType, SUPPORTS[kind])
        builders.fill(tx, b.SelectFace, faces, "Face")
        return committed(b)


def _load(tx, study, kind, faces, magnitude, vector):
    options = common.ds(tx).AnalysisLoadBuilder
    with builders.build(tx, study.CreateAnalysisLoadBuilder, common.ds(tx).AnalysisLoad.Null) as b:
        if kind == "force":
            b.Type = options.AnalysisLoadType.Force
            b.ForceType = options.AnalysisLoadForceType.ByVector
            b.FaceVector = builders.direction(tx, vector, field="/direction")
            common.drive(tx, b.Force, magnitude, "force")
        else:
            b.Type = options.AnalysisLoadType.Pressure
            common.drive(tx, b.Pressure, magnitude, "pressure")
        builders.fill(tx, b.SelectFace, faces, "Face")
        return committed(b)


def _regions(tx, study, design, kind, faces, bodies, thickness):
    """One region per face (NX refuses faces that do not touch: "Selected faces are not
    connected"), or one over the bodies; returns what NX committed."""
    if faces:
        return [_region(tx, study, design, kind, [f], [], thickness) for f in faces]
    return [_region(tx, study, design, kind, [], bodies, None)]


def _region(tx, study, design, kind, faces, bodies, thickness):
    options = common.ds(tx).ConstructionBodyBuilder
    with builders.build(
        tx, study.CreateConstructionBodyBuilder, common.ds(tx).ConstructionBody.Null
    ) as b:
        b.DesignSpace = design
        b.Method = getattr(options.MethodOption, REGIONS[kind])
        if faces:
            b.Type = options.ConstructionType.ByFace
            builders.fill(tx, b.FaceCollector, faces, "Face")
            if thickness is None:
                thickness = DEFAULT_DEPTH_MM / units.MM_PER[units.part_units(tx)]
            common.length(tx, b.ThickenOffset, thickness)
        else:
            b.Type = options.ConstructionType.ByBody
            for body in bodies:
                b.Bodies.Add(body)
        return committed(b)


def _plane(tx, value, field):
    _obj, origin, normal = select.plane(tx, value, field)
    return tx.part.Planes.CreatePlane(
        tx.nx.Point3d(*(float(v) for v in origin)),
        tx.nx.Vector3d(*(float(v) for v in normal)),
        tx.nx.SmartObject.UpdateOption.WithinModeling,
    )


def _shape(tx, study, design, kind, planes, size, vector):
    S = common.ds(tx).ShapeConstraintBuilder
    with builders.build(
        tx, study.CreateShapeConstraintBuilder, common.ds(tx).ShapeConstraint.Null
    ) as b:
        b.DesignSpace = design
        if kind == "symmetry":
            b.Type = S.ConstraintType.PlanarSymmetry
            b.PlanarSymmetryPlane1 = planes[0]
            if len(planes) > 1:
                b.PlanarSymmetryPlane2 = planes[1]
        elif kind == "member_size":
            b.Type = S.ConstraintType.MinimumMemberSize
            common.length(tx, b.MinimumDiameter, size)
        else:
            b.Type = S.ConstraintType.ExtrudeAlongVector
            b.ExtrudeDirection = builders.direction(tx, vector, field="/direction")
        return committed(b)


@tool(
    effect="model",
    label="Add topology condition",
    summary={
        "fixed": "Fix {faces} of {study}",
        "pinned": "Pin {faces} of {study}",
        "force": "Force {magnitude} N on {faces}",
        "pressure": "Pressure {magnitude} MPa on {faces}",
        "keep_in": "Keep-in region on {study}",
        "keep_out": "Keep-out region on {study}",
        "symmetry": "Symmetry constraint on {study}",
        "member_size": "Minimum member {size} on {study}",
        "extrude": "Extrusion constraint on {study}",
    },
    keywords=(
        "support",
        "fixed constraint",
        "load",
        "force",
        "pressure",
        "keep in",
        "keep out",
        "symmetry",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={
        "fixed": ("faces",),
        "pinned": ("faces",),
        "force": ("faces", "magnitude"),
        "pressure": ("faces", "magnitude"),
    },
    cost="heavy",
    display_bound=True,
    example={
        "study": "f:12",
        "kind": "force",
        "faces": ["fc:9"],
        "magnitude": 1000,
        "direction": "-z",
    },  # fmt: skip
)
def nx_create_topology_condition(
    tx: Tx,
    *,
    study: FeatureRef,
    kind: Kind,
    faces: Annotated[
        list[FaceRef], Items(1, 100), Doc("Design space faces; supports, loads: or scenery")
    ]
    | None = None,
    bodies: Annotated[list[BodyRef], Items(1, 20), Doc("keep_in/out: region bodies")] | None = None,
    thickness: Annotated[Length, Doc("keep_in/out depth from faces; 5 mm")] | None = None,
    magnitude: Annotated[float, Range(gt=0, le=1e9), Doc("force N; pressure MPa")] | None = None,
    direction: Annotated[Direction, Doc("force or extrude direction")] | None = None,
    planes: Annotated[list[SymmetryPlane], Items(1, 2)] | None = None,
    size: Annotated[Length, Doc("member_size: minimum diameter")] | None = None,
    load_case: Annotated[str, Text(40), Doc("Support/load: its load case (made if new); omit: all")]
    | None = None,
) -> ConditionData:
    """Add a support, load, keep-in/out region or shape constraint to a topology study.
    Pressure acts along the face normal; keep-in grows thickness into the body. Gravity,
    scenery bodies, bonds: nx_create_topology_environment."""
    _check(kind, faces, bodies, thickness, magnitude, direction, planes, size, load_case)
    with tx.checks():
        feature = common.feature_of(tx, study)
        found = common.active_study(feature)
        designs = common.all_of(found, "GetAllDesignSpaces")
        if not designs:
            fail("NX_NOT_FOUND", "The study has no design space", field="/study")
        body = designs[0].Body
        scenery = common.scenery_bodies(found) if kind not in REGIONS else []
        picked = common.design_faces(tx, faces, "/faces", body, scenery) if faces else []
        others = select.bodies(tx, bodies, "/bodies") if bodies else []
        for i, other in enumerate(others):
            if int(other.Tag) == int(body.Tag):
                fail("NX_INVALID_ARGUMENT", "Pass bodies other than the design space",
                     field=f"/bodies/{i}")  # fmt: skip
        vector = select.direction(tx, direction, "/direction") if direction is not None else None
        common.require_license(tx)
        before = (
            len(common.all_of(found, "GetAllAnalysisConstraints")),
            len(common.all_of(found, "GetAllAnalysisLoads")),
        )
    with common.task(tx, feature) as active:
        current = common.active_study(active)
        design = common.all_of(current, "GetAllDesignSpaces")[0]
        if kind in SUPPORTS or kind in ("force", "pressure"):
            getter = "GetAllAnalysisConstraints" if kind in SUPPORTS else "GetAllAnalysisLoads"
            known = {int(x.Tag) for x in common.all_of(current, getter)}
            if kind in SUPPORTS:
                _support(tx, current, kind, picked)
            else:
                _load(tx, current, kind, picked, float(magnitude or 0.0), vector)
            fresh = [x for x in common.all_of(current, getter) if int(x.Tag) not in known]
            readback.expect(len(fresh) == 1, "committed", "NX made no " + kind, count=len(fresh))
            with native():
                _join(tx, current, fresh[0], load_case)
        elif kind in REGIONS:
            made = _regions(tx, current, design, kind, picked, others, thickness)
            readback.expect(all(m is not None for m in made), "committed")
        else:
            shown = [_plane(tx, p, f"/planes/{i}") for i, p in enumerate(planes or [])]
            _shape(tx, current, design, kind, shown, size, vector)
    found = common.active_study(feature)
    supports = len(common.all_of(found, "GetAllAnalysisConstraints"))
    loads = len(common.all_of(found, "GetAllAnalysisLoads"))
    want = (before[0] + (kind in SUPPORTS), before[1] + (kind in ("force", "pressure")))
    readback.expect((supports, loads) == want, "count", supports=supports, loads=loads)
    return {
        "study": tx.ref(feature, kind="feature"),
        "kind": kind,
        "faces": len(picked),
        "supports": supports,
        "loads": loads,
        "load_case": load_case,
    }
