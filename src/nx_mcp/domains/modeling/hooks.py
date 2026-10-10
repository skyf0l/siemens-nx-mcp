"""Modeling listers for ``nx_list_objects`` (bodies, faces, edges, features, datums,
expressions) and the
feature deleter for ``nx_delete_objects`` (``fn(tx, objects)``, inside the caller's mark)."""

from __future__ import annotations

from nx_mcp.core.catalogue import deleter, lister
from nx_mcp.core.errors import fail, native
from nx_mcp.kit import expr, geom, model, result, select

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
    from nx_mcp.domains.modeling.tree import feature_messages, states

    # User-visible features only (a hole's or a sketch's internal sub-feature follows its
    # owner and no feature tool takes it). NX's Suppressed is also True for features after
    # the current one: suppressed is the user's suppression, inactive marks rolled back ones.
    out = []
    for f, suppressed, active in states(tx, tx.part).values():
        stamp = getattr(f, "Timestamp", None)
        details = {
            "suppressed": suppressed,
            "timestamp": None if stamp is None else int(stamp),
            "errors": feature_messages(f),
        }
        if not active:
            details["inactive"] = True
        out.append((f, "feature", {"type": str(f.FeatureType).lower(), "details": details}))
    return out


@lister("expression")
def expressions(tx, owner, owner_kind):
    """Work-part expressions by name, or the parameters of one feature (owner)."""
    if owner_kind == "feature":
        values = list(owner.GetExpressions())
    elif owner is None:
        values = list(tx.part.Expressions)
    else:
        fail("NX_WRONG_KIND", "owner must be a feature here", field="/owner")
    out = []
    for e in sorted(values, key=lambda e: str(e.Name)):
        try:
            value = expr.value(tx, e)
        except Exception:  # a string or list expression has no numeric value
            value = None
        details = {
            "formula": str(e.RightHandSide),
            "value": value,
            "units": expr.units(e),
            "editable": expr.editable(e),
        }
        out.append((e, "expression", {"type": str(getattr(e, "Type", "")).lower(),
                                      "details": details}))  # fmt: skip
    return out


@lister("datum")
def datums(tx, owner, owner_kind):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "datums are listed for the whole part", field="/owner")
    out = []
    # Datum CSYS frames live in CoordinateSystems, not in Datums.
    found = [*tx.part.Datums, *(getattr(tx.part, "CoordinateSystems", None) or [])]
    for datum in {int(d.Tag): d for d in found}.values():
        kind = tx.kind_of(datum)
        details = {"blanked": bool(getattr(datum, "IsBlanked", False))}
        if kind == "datum_plane":
            details["origin"] = geom.xyz(datum.Origin)
            details["normal"] = geom.unit(geom.xyz(datum.Normal))
        out.append((datum, kind, {"type": kind.removeprefix("datum_"), "details": details}))
    return out


def no_active_sketch(tx):
    """Deletes run the model update: refuse while a sketch is active (NX defers it)."""
    if tx.session.ActiveSketch is not None:
        fail("NX_SKETCH_ACTIVE", "Finish the active sketch before deleting model objects",
             hint="Finish the active sketch, then retry")  # fmt: skip


@deleter("feature")
def delete_features(tx, objects):
    """NX's Delete through the update manager: dependent features and their bodies go too."""
    no_active_sketch(tx)
    with native(default="NX_BUILD_FAILED"):
        tx.session.UpdateManager.AddToDeleteList(list(objects))
    model.update(tx)
