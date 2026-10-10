"""Sealed internal voids of solid bodies: shells by face adjacency, void volumes by facets.

    groups = shells(body)                 # [[face, ...], ...] one list per shell
    rows = voids(tx, body, groups)        # [{"faces", "volume", "min", "max"}] inner shells

NX 2406 findings encoded here:

- NX 2406 has no shell query in Python: ``NXOpen.Body`` has no ``GetShells`` and the UF
  bindings have no ``UF_MODL_ask_body_shells``/``ask_shell_faces``. A shell is a set of
  faces connected through shared edges, so shells are the connected components of the
  face/edge adjacency (``Edge.GetFaces``); a face without edges (a sphere) is a shell
  of its own. A solid body has one outer shell; every other shell bounds a void.
- Void volumes come from a UF facet model (``UF_FACET_facet_solid``, faces tagged):
  the signed volume of a shell's facets (divergence theorem) is positive for the outer
  shell and negative for a void, whose face normals point into the void. With 0.05 rad
  angular tolerances a revolved 10,115.9 mm^3 void measured 10,115.3 (outer 47,121 of
  47,124). The facet model is temporary: it is made under ``tx.scratch`` and undone.
- Void boxes are the union of the void faces' UF bounding boxes (conservative).
"""

from __future__ import annotations

import contextlib
from typing import Any

from nx_mcp.core.errors import native

FACET_ANGLE = 0.05  # rad, surface and curve angular tolerance of the facet model
NO_FACET = -1  # UF_FACET_NULL_FACET_ID


def shells(body) -> list[list]:
    """Faces of ``body`` grouped by shell (connected through shared edges)."""
    faces = {int(f.Tag): f for f in body.GetFaces()}
    parent = {tag: tag for tag in faces}

    def root(tag):
        while parent[tag] != tag:
            parent[tag] = parent[parent[tag]]
            tag = parent[tag]
        return tag

    for edge in body.GetEdges():
        tags = [int(f.Tag) for f in edge.GetFaces() if int(f.Tag) in parent]
        for other in tags[1:]:
            parent[root(other)] = root(tags[0])
    groups: dict[int, list] = {}
    for tag, face in faces.items():
        groups.setdefault(root(tag), []).append(face)
    return list(groups.values())


def _box(tx, faces) -> tuple[list[float], list[float]]:
    boxes = [[float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(f.Tag)] for f in faces]
    return (
        [min(b[i] for b in boxes) for i in range(3)],
        [max(b[i + 3] for b in boxes) for i in range(3)],
    )


def _signed_volumes(tx, body, groups) -> list[float]:
    """Signed facet volume of each shell (temporary facet model, undone by the caller)."""
    facet = tx.uf.Facet
    params = facet.AskDefaultParameters()
    params.SpecifyParameters = True
    params.SpecifySurfaceTolerance = True
    params.SurfaceAngularTolerance = FACET_ANGLE
    params.SpecifyCurveTolerance = True
    params.CurveAngularTolerance = FACET_ANGLE
    model = facet.FacetSolid(body.Tag, params)
    out = []
    for faces in groups:
        total = 0.0
        for face in faces:
            face_id = facet.AskFaceIdOfSolidFace(model, face.Tag)
            current = NO_FACET
            while True:
                current = facet.CycleFacetsInFace(model, face_id, current)
                if current == NO_FACET:
                    break
                count, vertices = facet.AskVerticesOfFacet(model, current)
                p = _points(vertices, int(count))
                for i in range(1, len(p) - 1):
                    a, b, c = p[0], p[i], p[i + 1]
                    total += (
                        a[0] * (b[1] * c[2] - b[2] * c[1])
                        - a[1] * (b[0] * c[2] - b[2] * c[0])
                        + a[2] * (b[0] * c[1] - b[1] * c[0])
                    ) / 6.0
        out.append(total)
    return out


def _points(vertices, count) -> list[list[float]]:
    """Facet vertices as points: NXOpen gives a list of xyz triples (or a flat list)."""
    values = list(vertices)
    if values and isinstance(values[0], (int, float)):
        return [[float(v) for v in values[3 * i : 3 * i + 3]] for i in range(count)]
    return [[float(c) for c in v] for v in values[:count]]


def voids(tx, body, groups) -> list[dict]:
    """``[{faces, volume, min, max}]`` of the inner shells of a solid body with more than
    one shell. ``volume`` is None where faceting fails; the outer shell is then the one
    with the largest box."""
    volumes: list[float] | None = None
    # suppress (inner) leaves the volumes None without facets (licence, kernel); a failed
    # scratch rollback (outer) still fails the call
    with tx.scratch(), contextlib.suppress(Exception):
        volumes = _signed_volumes(tx, body, groups)
    with native():
        boxes = [_box(tx, faces) for faces in groups]
    if volumes is not None:
        outer = max(range(len(groups)), key=lambda i: volumes[i])
    else:
        outer = max(range(len(groups)), key=lambda i: _extent(boxes[i]))
    rows: list[dict[str, Any]] = []
    for i, faces in enumerate(groups):
        if i == outer:
            continue
        rows.append(
            {
                "faces": len(faces),
                "volume": None if volumes is None else abs(volumes[i]),
                "min": boxes[i][0],
                "max": boxes[i][1],
            }
        )
    rows.sort(key=lambda r: -(r["volume"] or 0.0))
    return rows


def _extent(box) -> float:
    low, high = box
    out = 1.0
    for i in range(3):
        out *= max(high[i] - low[i], 0.0)
    return out
