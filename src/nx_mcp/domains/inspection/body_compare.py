"""Body compare: two versions of a solid (a supplier part's old and new release) face by
face, with their volume and area differences (tool-surface 3.9).

Faces are matched on their own data, deterministic and licence-free: UF ``AskFaceData``
(surface type, position, direction, radii), the face box, the edge count and the face
area. A ``matching`` pair has the same surface, box, edges and area; a ``changed`` pair
keeps its surface (the same plane, cylinder axis and radius, ...) but not its bounds,
edges or area (a face a new hole or pocket cuts); the faces left over are ``removed``
(control only) or ``added`` (sample only). The bodies are compared where they are: a
moved copy reports every face changed or added/removed.

NX 2406 measured: ``GeometricUtilities.CompareManager.CreateBodyCompareBuilder`` (an
internal API, gateway) compares (``SelectControlBody``/``SelectSampleBody``,
``Update*BodiesToEngine``, ``CompareAction`` -> ``IsCompareDone`` True) but exposes no
result: ``Commit`` returns None, ``GetCommittedObjects`` is empty, ``AnalysisResult`` stays
None and ``ReportAction`` writes only to the Information window (a listing redirected to a
file stays empty). Not used.
"""

from __future__ import annotations

import math
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Length, Range, RefTo
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import geom, measure, result
from nx_mcp.kit import units as kunits

# UF AskFaceData surface types.
CYLINDER, CONE, SPHERE, TORUS, PLANE = 16, 17, 18, 19, 22
AREA_REL = 1e-6  # areas within this fraction (or a tolerance-wide rim) are the same
LIMIT = 50
BodyOrComponent = Annotated[
    str, RefTo("body", "component", doc="Solid body, or component with one solid 'b:3'")
]


class ChangedFace(TypedDict):
    control: result.Row
    sample: result.Row
    area_change: float


class Counts(TypedDict):
    matching: int
    changed: int
    added: int
    removed: int


class Totals(TypedDict):
    control: float
    sample: float
    difference: float


class _CompareRequired(TypedDict):
    identical: bool
    counts: Counts
    changed: list[ChangedFace]
    added: list[result.Row]
    removed: list[result.Row]
    volume: Totals
    area: Totals
    tolerance: float


class CompareData(_CompareRequired, total=False):
    truncated: bool  # changed/added/removed lists were cut at limit; counts are complete


def _solid(tx, ref, field):
    obj = tx.resolve(ref, "body", "component")
    found = [b for b in kbodies.of(tx, obj) if getattr(b, "IsSolidBody", False)]
    if len(found) != 1:
        fail(
            "NX_WRONG_GEOMETRY",
            f"{ref} has {len(found)} solid bodies; compare one solid with one solid",
            field=field,
            hint="Pass the body ref (nx_list_objects kind body)",
        )
    return found[0]


def face_area(tx, face) -> float:
    return measure.area(tx, [face])


def signature(tx, face) -> dict:
    """The facts two faces are compared on (part units)."""
    with native():
        data = tx.uf.Modeling.AskFaceData(face.Tag)
        edges = len(list(face.GetEdges()))
    return {
        "face": face,
        "type": int(data[0]),
        "point": [float(v) for v in data[1]],
        "dir": [float(v) for v in data[2]],
        "box": [float(v) for v in data[3]],
        "radius": float(data[4]),
        "rad_data": float(data[5]),
        "edges": edges,
        "area": face_area(tx, face),
    }


def _within(a, b, tol):
    return abs(a - b) <= tol


def _off_line(point, origin, direction):
    n = geom.norm(direction)
    if n < 1e-12:
        return geom.dist(point, origin)
    return geom.norm(geom.cross(geom.sub(point, origin), direction)) / n


def same_surface(a, b, tol) -> bool:
    """Whether two faces lie on one underlying surface."""
    if a["type"] != b["type"]:
        return False
    kind = a["type"]
    if kind == PLANE:
        n = geom.unit(a["dir"])
        return (
            geom.same_direction(n, geom.unit(b["dir"]))
            and abs(geom.dot(n, geom.sub(b["point"], a["point"]))) <= tol
        )
    if kind in (CYLINDER, CONE, TORUS):
        return (
            geom.parallel(geom.unit(a["dir"]), geom.unit(b["dir"]))
            and _off_line(b["point"], a["point"], a["dir"]) <= tol
            and (kind == CONE or _within(a["radius"], b["radius"], tol))
            and _within(a["rad_data"], b["rad_data"], tol if kind == TORUS else 1e-9)
            and (kind != TORUS or geom.dist(a["point"], b["point"]) <= tol)
        )
    if kind == SPHERE:
        return geom.dist(a["point"], b["point"]) <= tol and _within(a["radius"], b["radius"], tol)
    return (  # free-form and other faces: their reported data and box agree
        geom.dist(a["point"], b["point"]) <= tol
        and geom.dist(a["dir"], b["dir"]) <= 1e-9
        and _same_box(a, b, tol)
    )


def _same_box(a, b, tol):
    return all(_within(x, y, tol) for x, y in zip(a["box"], b["box"], strict=True))


def _area_tol(area, tol):
    """A boundary moved by ``tol`` along the perimeter of a square of this area."""
    return max(AREA_REL * abs(area), 4 * tol * math.sqrt(abs(area)), tol * tol)


def identical(a, b, tol) -> bool:
    return (
        same_surface(a, b, tol)
        and _same_box(a, b, tol)
        and a["edges"] == b["edges"]
        and _within(a["area"], b["area"], _area_tol(a["area"], tol))
    )


def _centre(s):
    box = s["box"]
    return [(box[i] + box[i + 3]) / 2 for i in range(3)]


def match(control, sample, tol):
    """``(matching pairs, changed pairs, removed, added)`` of two signature lists."""
    left, right = list(control), list(sample)
    matching, changed = [], []
    for wanted in (identical, same_surface):
        for a in list(left):
            found = [b for b in right if wanted(a, b, tol)]
            if not found:
                continue
            b = min(found, key=lambda s: geom.dist(_centre(a), _centre(s)))
            (matching if wanted is identical else changed).append((a, b))
            left.remove(a)
            right.remove(b)
    return matching, changed, left, right


def _totals(control, sample) -> Totals:
    return {"control": control, "sample": sample, "difference": sample - control}


@tool(
    effect="read",
    label="Compare bodies",
    summary="Compare two bodies face by face",
    keywords=(
        "body compare",
        "geometry compare",
        "version compare",
        "supplier part",
        "changed faces",
        "model diff",
        "revision",
    ),
    requires=("work_part",),
    cost="light",
    example={"control": "b:1", "sample": "b:2"},
)
def nx_analyze_body_changes(
    tx: Tx,
    *,
    control: Annotated[BodyOrComponent, Doc("Old version: body, or component with one solid")],
    sample: Annotated[BodyOrComponent, Doc("New version, same kind, placed where the old one is")],
    tolerance: Annotated[Length, Doc("Position/size tolerance; default 0.001 mm")] | None = None,
    limit: Annotated[int, Range(ge=1, le=500), Doc("Rows per list")] = LIMIT,
) -> CompareData:
    """Compare two versions of a solid face by face, plus volume and area differences.
    Bodies are compared where they sit (no alignment). changed: same surface but new
    bounds, edges or area (e.g. cut by a new hole); added: sample only; removed: control
    only; differences are sample - control. Components: their occurrences in one assembly."""
    first = _solid(tx, control, "/control")
    second = _solid(tx, sample, "/sample")
    if int(first.Tag) == int(second.Tag):
        fail("NX_INVALID_ARGUMENT", "control and sample are the same body", field="/sample")
    tol = kunits.part_tolerance(tx) if tolerance is None else float(tolerance)
    if tol <= 0:
        fail("NX_INVALID_ARGUMENT", "tolerance must be above 0", field="/tolerance")
    faces = []
    for body in (first, second):
        with native():
            listed = list(body.GetFaces())
        rows = []
        for i, face in enumerate(listed):
            tx.checkpoint(i, len(listed))
            rows.append(signature(tx, face))
        faces.append(rows)
    matching, changed, removed, added = match(faces[0], faces[1], tol)
    masses = [measure.mass(tx, [b]) for b in (first, second)]
    areas = [m["area"] for m in masses]
    if None in areas:
        areas = [sum(s["area"] for s in rows) for rows in faces]
    out: CompareData = {
        "identical": not (changed or removed or added),
        "counts": {
            "matching": len(matching),
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
        },
        "changed": [
            {
                "control": tx.ref(a["face"], kind="face"),
                "sample": tx.ref(b["face"], kind="face"),
                "area_change": b["area"] - a["area"],
            }
            for a, b in changed[:limit]
        ],
        "added": [tx.ref(s["face"], kind="face") for s in added[:limit]],
        "removed": [tx.ref(s["face"], kind="face") for s in removed[:limit]],
        "volume": _totals(masses[0]["volume"], masses[1]["volume"]),
        "area": _totals(float(areas[0]), float(areas[1])),
        "tolerance": tol,
    }
    if max(len(changed), len(added), len(removed)) > limit:
        out["truncated"] = True
    if math.isclose(out["volume"]["difference"], 0.0, abs_tol=tol**3):
        out["volume"]["difference"] = 0.0
    return out
