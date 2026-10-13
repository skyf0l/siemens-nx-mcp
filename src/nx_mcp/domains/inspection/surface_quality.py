"""Surface quality: tangency and curvature continuity across the shared edges of faces, and
NX display analyses (reflection stripes, curvature combs, continuity, radius) to look at
(tool-surface 3.9).

``nx_check_face_continuity`` samples every edge two of the selected faces share with
``kit.surface.edge_continuity`` (normal angle in degrees, shape-operator difference in
1/part unit; sampled, not certified). NX's own Surface Continuity analysis (multi-face,
gateway) runs on the same faces under ``tx.scratch`` and adds its G0/G1 verdict.

``nx_create_surface_analysis`` keeps one NX analysis object in the work part (saved with
it, shown in its views) for nx_capture_view; ``nx_undo_operation`` with the call's
operation removes it. Only gateway analysis builders are used.

NX 2406 measured (``GeometricAnalysis``, part not displayed):

- ``SurfaceContinuityAnalysisBuilder.GetGxResult(i)`` is a pass flag per G0..G3 against
  the builder tolerances, False before ``Commit``; there is no numeric result. Two block
  faces (a 90 degree edge) fail G1 at any G1Tolerance up to 179; a 5 mm blend and its
  planar neighbour pass G1 at 0.5 and fail G2 up to a G2Tolerance of 2 (pass at 3): the
  G2 tolerance is not a curvature in 1/mm, so G2 comes only from the sampling.
- ``RadiusAnalysisBuilder1.DataRange`` Min/Max after ``StartRadiusAnalysis`` are single
  precision (-25.0000006 for a 25 mm cylinder) and ignored a second face: not used for
  numbers.
- ``CurveCurvatureAnalysisBuilder.Commit()`` returns None; ``GetCommittedObjects()``
  holds the comb. Every analysis object is removed by an undo to an earlier mark.
- A committed continuity analysis adds two expressions (its distance and angle
  thresholds); they go with it on undo.
- ``SelectObjectList.Add(list)`` takes faces; the comb's ``SelectedCurves`` is an
  ``ScCollector`` (dumb curve and edge rules).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Doc, FaceRef, Items, Range, RefTo, forbid_extra
from nx_mcp.domains.inspection import common
from nx_mcp.kit import builders, licenses, result, select, surface
from nx_mcp.kit import units as kunits

LICENSE = ("gateway",)
NOTE = "NX surface analysis needs the gateway license"


# ---------------------------------------------------------------------------
# Continuity across shared edges


class Tolerances(TypedDict, total=False):
    angle: Annotated[Angle, Doc("G1 normal angle, under 90 degrees; default 0.1")]
    curvature: Annotated[float, Range(gt=0), Doc("G2 shape difference, 1/part unit; 0.01")]


forbid_extra(Tolerances)
DEFAULTS = {"angle": 0.1, "curvature": 0.01}


class EdgeContinuity(TypedDict):
    edge: result.Row
    faces: list[result.Row]
    angle: float | None
    curvature: float | None
    g1: bool | None
    g2: bool | None
    unresolved: int


class NxVerdict(TypedDict):
    g0: bool
    g1: bool


class _FaceContinuityRequired(TypedDict):
    edges: list[EdgeContinuity]
    g1: bool | None
    g2: bool | None
    angle: float | None
    curvature: float | None
    samples: int
    tolerances: dict[str, float]


class FaceContinuityData(_FaceContinuityRequired, total=False):
    nx: NxVerdict


def _limits(tolerances) -> dict[str, float]:
    limits = {**DEFAULTS, **{k: float(v) for k, v in (tolerances or {}).items()}}
    if not 0 < limits["angle"] < 90:
        fail("NX_INVALID_ARGUMENT", "angle must be 0 to 90 degrees", field="/tolerances/angle")
    return limits


def _shared_edges(faces) -> list[tuple]:
    """``[(edge, face, other face)]`` for every edge two of ``faces`` share, once each."""
    chosen = {int(f.Tag): f for f in faces}
    seen, out = set(), []
    for face in faces:
        for edge in face.GetEdges():
            if int(edge.Tag) in seen:
                continue
            owners = [f for f in edge.GetFaces() if int(f.Tag) in chosen]
            if len(owners) == 2:
                seen.add(int(edge.Tag))
                out.append((edge, owners[0], owners[1]))
    return out


def _nx_verdict(tx, faces, angle) -> NxVerdict | None:
    """NX's multi-face Surface Continuity pass flags for G0 (part tolerance) and G1
    (``angle``), from an analysis object made and undone under ``tx.scratch``; None when
    this host has no such builder or NX refuses."""
    collection = getattr(getattr(tx.part, "AnalysisManager", None), "AnalysisObjects", None)
    create = getattr(collection, "CreateSurfaceContinuityAnalysisBuilder", None)
    analysis = getattr(tx.nx, "GeometricAnalysis", None)
    if create is None or analysis is None or licenses.served(tx, LICENSE) is False:
        return None
    kinds = analysis.SurfaceContinuityAnalysisBuilder.Types
    verdict: NxVerdict | None = None
    with tx.scratch():  # a failed undo of the analysis object raises, never swallowed
        try:
            with builders.build(tx, create, None) as b:
                b.Type = kinds.MultiFace
                b.MultiFace.Add(list(faces))
                b.G0, b.G1, b.G2, b.G3 = True, True, False, False
                b.G0Tolerance = kunits.part_tolerance(tx)
                b.G1Tolerance = float(angle)
                b.Commit()
                verdict = {"g0": bool(b.GetGxResult(0)), "g1": bool(b.GetGxResult(1))}
        except Exception:  # only a second opinion: the sampled values stand without it
            verdict = None
    return verdict


@tool(
    effect="read",
    label="Check face continuity",
    summary=lambda a, say: "Continuity across " + common.count_text(a, "faces", "face"),
    keywords=(
        "surface continuity",
        "tangency",
        "g1 g2",
        "curvature continuity",
        "class a",
        "fairing",
        "kink",
    ),
    requires=("work_part",),
    cost="light",
    example={"faces": ["fc:12", "fc:13"], "tolerances": {"angle": "0.5 deg"}},
)
def nx_check_face_continuity(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(2, 100)],
    tolerances: Tolerances | None = None,
    samples: Annotated[int, Range(ge=1, le=50), Doc("Points per shared edge")] = 9,
) -> FaceContinuityData:
    """Check G1/G2 continuity across every edge two of the faces share. E.g. a blend and its
    neighbours, hull plates: sampled max normal angle (deg) and curvature difference per
    edge; nx adds NX's own G0/G1 verdict. Separate sheets: nx_check_continuity."""
    limits = _limits(tolerances)
    selected = select.faces(tx, faces)
    shared = _shared_edges(selected)
    if not shared:
        fail(
            "NX_WRONG_GEOMETRY",
            "No two of the faces share an edge",
            field="/faces",
            hint="Select neighbouring faces (nx_find_geometry kind face near the edge); for "
            "boundary edges of separate sheets use nx_check_continuity",
        )
    rows: list[EdgeContinuity] = []
    for i, (edge, first, second) in enumerate(shared):
        tx.checkpoint(i, len(shared))
        report = surface.edge_continuity(tx, edge, first, edge, second, samples)
        angle, curvature = report["angle"], report["curvature"]
        g1 = angle <= limits["angle"] if angle is not None else None
        g2 = (g1 and curvature <= limits["curvature"]) if curvature is not None else None
        rows.append(
            {
                "edge": tx.ref(edge, kind="edge"),
                "faces": [tx.ref(first, kind="face"), tx.ref(second, kind="face")],
                "angle": angle,
                "curvature": curvature,
                "g1": g1,
                "g2": g2,
                "unresolved": report["unresolved"],
            }
        )
    complete = all(r["angle"] is not None for r in rows)
    out: FaceContinuityData = {
        "edges": rows,
        "g1": all(r["g1"] for r in rows) if complete else None,
        "g2": all(r["g2"] for r in rows) if complete else None,
        "angle": max(r["angle"] for r in rows) if complete else None,  # type: ignore[type-var]
        "curvature": max(r["curvature"] for r in rows) if complete else None,  # type: ignore[type-var]
        "samples": samples * len(rows),
        "tolerances": limits,
    }
    verdict = _nx_verdict(tx, selected, limits["angle"])
    if verdict is not None:
        out["nx"] = verdict
    return out


# ---------------------------------------------------------------------------
# Display analyses


AnalysisType = Literal["reflection", "curvature_comb", "continuity", "radius"]
LINES = {4: "Four", 8: "Eight", 16: "Sixteen", 32: "ThirtyTwo", 64: "SixtyFour"}
LINE_IMAGES = {
    "black": "BlackLines",
    "black_white": "BlackandWhiteLines",
    "colored": "ColoredLines",
}
ORIENTATIONS = {"horizontal": "Horizontal", "vertical": "Vertical", "both": "Both"}
RADII = {"gaussian": "Gaussian", "minimum": "Minimum", "maximum": "Maximum", "mean": "Mean"}
TYPE_DOC = "reflection: zebra on faces; curvature_comb: curves; continuity: G0-G2 marks; radius map"
CurveOrEdge = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]


class AnalysisData(TypedDict):
    type: str
    name: str
    faces: int
    curves: int
    analyses: int


def _collection(tx):
    manager = getattr(tx.part, "AnalysisManager", None)
    collection = getattr(manager, "AnalysisObjects", None)
    if collection is None:
        fail("NX_UNSUPPORTED_OPTION", "This NX has no analysis objects", field="/type")
    return collection


def _count(collection) -> int:
    try:
        return len(list(collection))
    except TypeError:  # not iterable on this host
        return 0


def _reflection(tx, collection, faces, lines, line_type, orientation):
    ex = collection.AnalysisObjectsEx
    create = (
        getattr(ex, "CreateReflectionAnalysisBuilder1", None) or ex.CreateReflectionAnalysisBuilder
    )
    kinds = tx.nx.GeometricAnalysis.ReflectionAnalysisBuilder1
    with builders.build(tx, create, None) as b:
        b.Type = kinds.Types.LineImages
        b.LineImageType = getattr(kinds.LineImageTypes, LINE_IMAGES[line_type])
        b.LineNumber = getattr(kinds.NumberOfLinesOptions, LINES[lines])
        b.LineOrientation = getattr(kinds.LineOrientationType, ORIENTATIONS[orientation])
        b.Faces.Add(list(faces))
        b.Commit()
        return list(b.GetCommittedObjects())


def _comb(tx, collection, curves, density):
    with builders.build(tx, collection.CreateCurveCurvatureAnalysisBuilder, None) as b:
        factory = tx.part.ScRuleFactory
        edges = [c for c in curves if common.kind_of(tx, c) == "edge"]
        plain = [c for c in curves if common.kind_of(tx, c) != "edge"]
        rules = ([factory.CreateRuleCurveDumb(plain)] if plain else []) + (
            [factory.CreateRuleEdgeDumb(edges)] if edges else []
        )
        b.SelectedCurves.ReplaceRules(rules, False)
        b.ShowCombs = True
        if density is not None:
            b.CombOptions.Density = int(density)
        b.Commit()  # NX 2406 returns None: the comb is in the committed objects
        return list(b.GetCommittedObjects())


def _continuity(tx, collection, faces, angle):
    kinds = tx.nx.GeometricAnalysis.SurfaceContinuityAnalysisBuilder.Types
    with builders.build(tx, collection.CreateSurfaceContinuityAnalysisBuilder, None) as b:
        b.Type = kinds.MultiFace
        b.MultiFace.Add(list(faces))
        b.G0, b.G1, b.G2 = True, True, True
        b.G0Tolerance = kunits.part_tolerance(tx)
        b.G1Tolerance = float(angle)
        b.Commit()
        return list(b.GetCommittedObjects())


def _radius(tx, collection, faces, radius_type):
    ex = collection.AnalysisObjectsEx
    kinds = tx.nx.GeometricAnalysis.RadiusAnalysisBuilder1
    with builders.build(tx, ex.CreateRadiusAnalysisBuilder1, None) as b:
        b.Type = getattr(kinds.Types, RADII[radius_type])
        b.DisplayMode = kinds.DisplayModes.Fringe
        b.Faces.Add(list(faces))
        b.StartRadiusAnalysis()
        b.Commit()
        return list(b.GetCommittedObjects())


@tool(
    effect="model",
    label="Create surface analysis",
    summary="Show {type} analysis",
    keywords=(
        "zebra",
        "reflection lines",
        "highlight stripes",
        "curvature comb",
        "porcupine",
        "radius map",
        "surface quality display",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="type",
    required_by={
        "reflection": ("faces",),
        "curvature_comb": ("curves",),
        "continuity": ("faces",),
        "radius": ("faces",),
    },
    cost="heavy",
    invalidates="deleted",
    example={"type": "reflection", "faces": ["fc:12", "fc:13"], "lines": 16},
)
def nx_create_surface_analysis(
    tx: Tx,
    *,
    type: Annotated[AnalysisType, Doc(TYPE_DOC)],
    faces: Annotated[list[FaceRef], Items(1, 200), Doc("reflection, continuity (2+), radius")]
    | None = None,
    curves: Annotated[list[CurveOrEdge], Items(1, 200), Doc("curvature_comb")] | None = None,
    lines: Annotated[Literal[4, 8, 16, 32, 64], Doc("reflection stripes")] = 16,
    line_type: Annotated[Literal["black", "black_white", "colored"], Doc("reflection")] = (
        "black_white"
    ),
    orientation: Annotated[Literal["horizontal", "vertical", "both"], Doc("reflection")] = (
        "vertical"
    ),
    density: Annotated[int, Range(ge=2, le=1000), Doc("curvature_comb needles")] | None = None,
    angle: Annotated[Angle, Doc("continuity G1 tolerance; default 0.1 deg")] = 0.1,
    radius_type: Annotated[Literal["gaussian", "minimum", "maximum", "mean"], Doc("radius")] = (
        "minimum"
    ),
) -> AnalysisData:
    """Create an NX display analysis to look at or capture with nx_capture_view. Reflection
    zebra stripes, curvature combs, continuity marks or a radius map; it stays in the part
    until nx_undo_operation with this call's operation removes it. Numbers:
    nx_check_face_continuity, nx_analyze_faces, nx_analyze_curve."""
    if type in ("reflection", "continuity", "radius") and curves is not None:
        fail("NX_INVALID_ARGUMENT", f"{type} takes faces, not curves", field="/curves")
    if type == "curvature_comb" and faces is not None:
        fail("NX_INVALID_ARGUMENT", "curvature_comb takes curves, not faces", field="/faces",
             hint="For faces use reflection or radius")  # fmt: skip
    if type == "continuity" and faces is not None and len(faces) < 2:
        fail("NX_INVALID_ARGUMENT", "continuity compares 2 or more faces", field="/faces")
    angle = float(angle)
    if not 0 < angle < 90:
        fail("NX_INVALID_ARGUMENT", "angle must be 0 to 90 degrees", field="/angle")
    with tx.checks():
        picked_faces = select.faces(tx, faces) if faces is not None else []
        picked_curves = [
            select.one(tx, ref, "curve", "edge", field=f"/curves/{i}")
            for i, ref in enumerate(curves or [])
        ]
        licenses.require(tx, LICENSE, NOTE)
        collection = _collection(tx)
    with native(default="NX_BUILD_FAILED"):
        if type == "reflection":
            made = _reflection(tx, collection, picked_faces, lines, line_type, orientation)
        elif type == "curvature_comb":
            made = _comb(tx, collection, picked_curves, density)
        elif type == "continuity":
            made = _continuity(tx, collection, picked_faces, angle)
        else:
            made = _radius(tx, collection, picked_faces, radius_type)
    if not made:
        fail("NX_BUILD_FAILED", f"NX made no {type} analysis")
    name = f"NX_MCP_{type.upper()}"
    with native():
        for obj in made:
            obj.SetName(name)
    return {
        "type": type,
        "name": name,
        "faces": len(picked_faces),
        "curves": len(picked_curves),
        "analyses": _count(collection),
    }
