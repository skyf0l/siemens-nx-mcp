"""Helpers shared by the drawing tools (private to the drawing domain).

Attribute names are the ones the legacy tools wrote, so drawings made before the
migration keep their managed detail boundaries, table kinds and construction state.
"""

from __future__ import annotations

from nx_mcp.core.errors import fail
from nx_mcp.kit import drawing, facts, geom, parts, select

DETAIL = "NX_MCP_DETAIL_V1"  # managed detail-view boundary (parent handle, center, radius)
TABLE_KIND = "NX_MCP_DRAWING_TABLE_V1"  # "title_block" / "revision"
NATIVE_TABLE = "NX_MCP_NATIVE_TABLE_V1"  # title block -> handle of its tabular note
CONSTRUCTION = "NX_MCP_DRAWING_CONSTRUCTION_V1"  # "show" / "hide" / "partial"
CONSTRUCTION_LAYERS = "NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1"
LEADER_STUB_MM = 5.0
ORIGIN_TOL_MM = 1e-3


def uf_types(tx):
    """The ``NXOpen.UF`` module (structs such as ``Drf.Object``)."""
    tx.uf  # noqa: B018 - imports NXOpen.UF on first use
    return tx.nx.UF


def update(tx):
    """Model update inside the call's undo mark (annotations, tables, PMI)."""
    errors = tx.session.UpdateManager.DoUpdate(tx.mark)
    if errors:
        fail("NX_UPDATE_FAILED", f"{errors} native update errors after the change")


def attribute(tx, obj, name):
    """A string user attribute of ``obj`` or None."""
    try:
        if obj.HasUserAttribute(name, tx.nx.NXObject.AttributeType.String, -1):
            return str(obj.GetStringAttribute(name))
    except AttributeError:  # objects without attributes
        return None
    return None


def geometry(tx, ref, kinds, field):
    """An edge or face of the work part, or an occurrence in the work assembly."""
    obj = select.resolve(tx, ref, *kinds, field=field)
    if getattr(obj, "IsOccurrence", False):
        tags = {int(c.Tag) for c, _ in parts.components(tx.part)}
        owner = getattr(obj, "OwningComponent", None)
        if owner is None or int(owner.Tag) not in tags:
            fail("NX_WRONG_OWNER", "Select geometry in the work assembly", field=field)
    elif getattr(obj, "OwningPart", None) != tx.part:
        fail("NX_WRONG_OWNER", "Select geometry owned by the work part", field=field)
    return obj


def kind_of_geometry(obj):
    return "edge" if hasattr(obj, "SolidEdgeType") or type(obj).__name__ == "Edge" else "face"


def edge_facts(tx, edge, field, want=None):
    """Curve facts of a dimensioned edge; ``want`` = "line"/"arc" refuses others."""
    found = facts.edge(tx, edge)
    if want == "arc" and found["kind"] != "arc":
        fail("NX_WRONG_GEOMETRY", "Select a circular edge or arc", field=field)
    if want == "line" and found["kind"] != "line":
        fail("NX_WRONG_GEOMETRY", "Select a straight edge", field=field)
    return found


def true_shape(view, info, field):
    """Refuse an arc whose plane is not parallel to the drawing plane of ``view``."""
    normal = info.get("normal")
    if normal is None or not drawing.seen_true_shape(normal, drawing.axes(view)[2]):
        fail(
            "NX_UNSUPPORTED_GEOMETRY",
            "The circle is seen edge-on or as an ellipse in this view: use a view that looks "
            "along its axis",
            field=field,
        )


def sheet_xy(sh, obj):
    """An annotation's origin on its sheet, ``[x, y]`` in sheet units."""
    return sh.to_sheet(obj.AnnotationOrigin)


def verify_origin(sh, obj, point, check="origin"):
    actual = sheet_xy(sh, obj)
    tol = ORIGIN_TOL_MM / (25.4 if sh.units == "in" else 1.0)
    if any(
        abs(a - float(b)) > tol * max(1.0, abs(float(b)))
        for a, b in zip(actual, point, strict=True)
    ):
        fail(
            "NX_VERIFICATION_FAILED",
            f"NX put the annotation at {actual}, not at {list(point)}",
            details={"check": check, "actual": actual, "requested": list(point)},
        )
    return actual


def set_origin(tx, origin_builder, sh, point):
    """Place an annotation origin on the sheet (no inference from geometry)."""
    origin_builder.SetInferRelativeToGeometry(False)
    origin_builder.Plane.PlaneMethod = tx.nx.Annotations.PlaneBuilder.PlaneMethodType.XyPlane
    origin_builder.Origin.SetValue(None, None, sh.point(point))


def add_leader(tx, leader_builder, obj, view):
    """Append one filled-arrow leader attached to ``obj`` as shown in ``view``.

    Returns the attachment point on the sheet (sheet units). The selection point is
    a model point (part units) on the edge or inside the face, clear of holes.
    """
    A = tx.nx.Annotations
    model = facts.on_object(tx, obj)
    data = tx.part.Annotations.CreateLeaderData()
    data.StubSize = LEADER_STUB_MM * (1.0 if _part_mm(tx) else 1 / 25.4)
    data.Arrowhead = A.LeaderData.ArrowheadType.FilledArrow
    data.VerticalAttachment = A.LeaderVerticalAttachment.Center
    leader_builder.Leaders.Append(data)
    data.StubSide = A.LeaderSide.Inferred
    data.Leader.SetValue(obj, view, tx.nx.Point3d(*model))
    return drawing.to_sheet(tx, view, model)


def _part_mm(tx):
    return tx.part.PartUnits == tx.nx.BasePart.Units.Millimeters


def check_leader(reader, obj, view):
    """Read the first leader of a reopened builder and verify its attachment."""
    leaders = reader.Leader.Leaders
    attached = leaders.FindItem(0).Leader.GetValue() if leaders.Length else (None, None, None)
    first, seen = attached[0], attached[1]
    if (
        first is None
        or int(first.Tag) != int(obj.Tag)
        or seen is None
        or int(seen.Tag) != int(view.Tag)
    ):
        fail(
            "NX_VERIFICATION_FAILED",
            "The leader is not attached to the requested object in the view",
            details={"check": "leader"},
        )


def commit(builder, what):
    """Validate and commit an annotation builder; None from NX is a failure."""
    if hasattr(builder, "Validate") and not builder.Validate():
        fail("NX_BUILD_FAILED", f"NX rejected the {what} inputs")
    obj = builder.Commit()
    if obj is None:
        fail("NX_BUILD_FAILED", f"NX returned no {what}")
    return obj


def associated(obj):
    """Objects an annotation is natively associated with (read back)."""
    out = []
    count = int(getattr(obj, "NumberOfAssociativities", 0) or 0)
    for i in range(1, count + 1):
        try:
            first = obj.GetAssociativity(i).FirstObject
        except Exception:  # an associativity NX cannot report
            continue
        if first is not None:
            out.append(first)
    return out


def leader_row(tx, obj, view, point):
    return {
        "object": tx.ref(obj, kind_of_geometry(obj)),
        "view": tx.ref(view, "drawing_view"),
        "point": point,
    }


def distance_to_line(point, origin, direction):
    """Distance from a 2D point to the infinite line through ``origin`` along ``direction``."""
    length = geom.norm([direction[0], direction[1], 0.0])
    if length == 0:
        return geom.dist([point[0], point[1], 0.0], [origin[0], origin[1], 0.0])
    cross = (point[0] - origin[0]) * direction[1] - (point[1] - origin[1]) * direction[0]
    return abs(cross) / length


def table_tag(tx, obj):
    """The tabular-note tag behind a table object (parts list, bend table, section, title
    block), or None for other objects."""
    name = type(obj).__name__
    lists = {int(p.Tag) for p in tx.part.Annotations.PartsLists}
    if name in ("PartsList", "BendTable", "HoleTable") or int(obj.Tag) in lists:
        return obj.Tag
    if name == "TitleBlock":
        handle = attribute(tx, obj, NATIVE_TABLE)
        return int(tx.uf.Tag.AskTagOfHandle(handle)) if handle else None
    if name == "TableSection":
        return tx.uf.Tabnot.AskTabularNoteOfSection(obj.Tag)
    return None


def table_kind(tx, obj):
    """``parts_list``, ``bend``, ``hole``, ``title_block``, ``revision``, ``table`` or
    None."""
    name = type(obj).__name__
    tags = {int(p.Tag) for p in tx.part.Annotations.PartsLists}
    if int(obj.Tag) in tags:
        return "parts_list"
    if name == "BendTable":
        return "bend"
    if name == "HoleTable":
        return "hole"
    managed = attribute(tx, obj, TABLE_KIND)
    if managed in ("title_block", "revision"):
        return managed
    if name == "TitleBlock":
        return "title_block"
    if name in ("TableSection", "Table"):
        return "table"
    return None


def annotation_kind(tx, obj):
    """The reference kind of a drafting object: ``dimension``, ``drawing_table`` or
    ``annotation``."""
    if int(obj.Tag) in {int(d.Tag) for d in tx.part.Dimensions}:
        return "dimension"
    return "drawing_table" if table_kind(tx, obj) else "annotation"
