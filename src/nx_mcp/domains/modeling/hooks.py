"""Modeling listers for ``nx_list_objects``: bodies, faces, edges, features, datums."""

from __future__ import annotations

from nx_mcp.core.catalogue import lister
from nx_mcp.core.errors import fail
from nx_mcp.kit import geom, result, select

FACE_TYPES = {22: "plane", 16: "cylinder", 17: "cone", 18: "sphere", 19: "torus"}
EDGE_TYPES = {"Linear": "line", "Circular": "circle", "Elliptical": "ellipse"}


def _bodies(tx, owner, owner_kind):
    if owner is None:
        return list(tx.part.Bodies)
    if owner_kind == "feature":
        return list(owner.GetBodies())
    if owner_kind == "body":
        return [owner]
    fail("NX_WRONG_KIND", "owner must be a body or a feature here", field="/owner")


@lister("body")
def bodies(tx, owner, owner_kind):
    return [
        (b, "body", {"type": "solid" if getattr(b, "IsSolidBody", False) else "sheet"})
        for b in _bodies(tx, owner, owner_kind)
    ]


@lister("face")
def faces(tx, owner, owner_kind):
    found = []
    for body in _bodies(tx, owner, owner_kind):
        for face in body.GetFaces():
            code, point, direction = select.face_data(tx, face)
            extra = {"type": FACE_TYPES.get(code, "other")}
            if code == 22:
                extra["details"] = {"point": point, "normal": geom.unit(direction)}
            found.append((face, "face", extra))
    return found


@lister("edge")
def edges(tx, owner, owner_kind):
    if owner_kind == "face":
        values = list(owner.GetEdges())
    else:
        values = [e for b in _bodies(tx, owner, owner_kind) for e in b.GetEdges()]
    out = []
    for edge in values:
        name = result.enum_name(edge.SolidEdgeType, tx.nx.Edge.EdgeType)
        out.append((edge, "edge", {"type": EDGE_TYPES.get(name, "other")}))
    return out


@lister("feature")
def features(tx, owner, owner_kind):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "features are listed for the whole part", field="/owner")
    return [
        (
            f,
            "feature",
            {"type": str(f.FeatureType).lower(), "details": {"suppressed": bool(f.Suppressed)}},
        )
        for f in tx.part.Features
    ]


@lister("datum")
def datums(tx, owner, owner_kind):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "datums are listed for the whole part", field="/owner")
    out = []
    for datum in tx.part.Datums:
        kind = tx.kind_of(datum)
        extra = {"type": kind.removeprefix("datum_")}
        if kind == "datum_plane":
            extra["details"] = {
                "origin": geom.xyz(datum.Origin),
                "normal": geom.unit(geom.xyz(datum.Normal)),
            }
        out.append((datum, kind, extra))
    return out
