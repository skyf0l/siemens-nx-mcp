"""Helpers shared by the drawing tools (private to the drawing domain).

Attribute names never change: drawings saved by earlier NX MCP versions keep their
managed detail boundaries, table kinds and construction state.
"""

from __future__ import annotations

import re
from typing import Annotated

from nx_mcp.core.errors import fail
from nx_mcp.core.types import REF_PATTERN, Doc, Range, RefKind, Text
from nx_mcp.kit import drawing, facts, geom, parts, select, sketch
from nx_mcp.kit.args import choice as choice  # natural option spellings

DETAIL = "NX_MCP_DETAIL_V1"  # managed detail-view boundary (parent handle, center, radius)
TABLE_KIND = "NX_MCP_DRAWING_TABLE_V1"  # "title_block" / "revision"
NATIVE_TABLE = "NX_MCP_NATIVE_TABLE_V1"  # title block -> handle of its tabular note
CONSTRUCTION = "NX_MCP_DRAWING_CONSTRUCTION_V1"  # "show" / "hide" / "partial"
CONSTRUCTION_LAYERS = "NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1"
VIEW_PARENT = "NX_MCP_VIEW_PARENT_V1"  # derived view -> handle of the view it came from
LEADER_STUB_MM = 5.0
DEFAULT_ORIGIN_OFFSET_MM = 20.0  # an omitted symbol/GD&T origin, from the leader point
ORIGIN_TOL_MM = 1e-3

# ---- natural argument values ----------------------------------------------------------

# A free spelling next to a Literal (``Literal[...] | Loose``): the schema still lists the
# canonical values, and ``choice`` maps 'A3', 'Front' or 'first angle' onto them.
Loose = Annotated[str, Text(32)]
ScaleArg = Annotated[
    Annotated[float, Range(gt=0)] | Annotated[str, Text(16)], Doc("2 or '2:1'; 0.5 or '1:2'")
]
MAX_SCALE = 1000.0
SheetRef = Annotated[str, Text(128, REF_PATTERN), RefKind("drawing_sheet")]
SheetArg = Annotated[SheetRef | Annotated[str, Text(64)], Doc("Sheet ref or name")]
ViewRef = Annotated[str, Text(128, REF_PATTERN), RefKind("drawing_view")]
ViewArg = Annotated[ViewRef | Annotated[str, Text(64)], Doc("View ref v:N, or name 'TOP@1'")]


def scale_value(value, field="/scale"):
    """A scale given as a number or a ratio string ('1:2', '2/1', '0.5') as a float (None
    kept)."""
    if value is None:
        return None
    try:
        terms = [float(v) for v in re.split(r"[:/]", str(value))]
        ratio = terms[0] / terms[1] if len(terms) == 2 else terms[0] if len(terms) == 1 else 0.0
    except (ValueError, ZeroDivisionError):
        ratio = 0.0
    if not 0 < ratio <= MAX_SCALE:
        fail(
            "NX_INVALID_ARGUMENT",
            f"scale {value!r} must be a positive number or ratio up to {MAX_SCALE:g}, such "
            "as 0.5 or '1:2' (half size) and 2 or '2:1' (twice size)",
            field=field,
        )
    return ratio


def scale_terms(ratio):
    """``(numerator, denominator)`` NX shows for a scale: 0.5 as 1:2, 2 as 2:1, 0.3 as 0.3:1."""
    inverse = 1.0 / ratio
    if ratio < 1 and abs(inverse - round(inverse)) <= 1e-9 * inverse:
        return 1.0, float(round(inverse))
    return float(ratio), 1.0


def sheet_of(tx, value, field):
    """A drawing sheet of the work part from its ref or its name (any case)."""
    text = str(value).strip()
    if re.match(REF_PATTERN, text):
        return select.resolve(tx, text, "drawing_sheet", field=field, work_part=True)
    sheets = list(tx.part.DrawingSheets)
    found = [s for s in sheets if str(s.Name).casefold() == text.casefold()]
    if not found:
        fail(
            "NX_NOT_FOUND",
            f"The work part has no drawing sheet named {text!r}",
            field=field,
            hint="Pass a sheet ref (sh:N) or name listed in details.sheets",
            details={"sheets": [tx.ref(s, "drawing_sheet") for s in sheets]},
        )
    return found[0]


def view_of(tx, value, field="/view"):
    """``(view, sheet)`` for a drawing view of the work part from its ref or its name (any
    case; 'top' finds 'TOP@1' when one view has that name before the '@')."""
    text = str(value).strip()
    if re.match(REF_PATTERN, text):
        return drawing.view(tx, text, field)
    views = [v for s in tx.part.DrawingSheets for v in s.GetDraftingViews()]
    key = text.casefold()
    found = [v for v in views if str(v.Name).casefold() == key] or [
        v for v in views if str(v.Name).split("@")[0].casefold() == key
    ]
    if len(found) != 1:
        fail(
            "NX_NOT_FOUND" if not found else "NX_INVALID_ARGUMENT",
            f"{'More than one' if found else 'No'} drawing view is named {text!r}",
            field=field,
            hint="Pass a view ref (v:N) from nx_create_drawing_view or details.views",
            details={"views": [tx.ref(v, "drawing_view") for v in found or views]},
        )
    return drawing.view(tx, found[0], field)


def open_or_only_sheet(tx, field="/sheet"):
    """The work part's open drawing sheet, else its only sheet (an omitted sheet)."""
    part = tx.part
    current = drawing.current(part)
    if current is not None:
        return current
    sheets = list(part.DrawingSheets)
    if len(sheets) == 1:
        return sheets[0]
    fail(
        "NX_INVALID_ARGUMENT",
        "sheet is required: no drawing sheet is open"
        + (f" and the part has {len(sheets)}" if sheets else " and the part has none"),
        field=field,
        hint="Pass sheet, open one with nx_activate_drawing_sheet"
        if sheets
        else "Create one with nx_create_drawing_sheet",
        details={"sheets": [tx.ref(s, "drawing_sheet") for s in sheets]},
    )


def uf_types(tx):
    """The ``NXOpen.UF`` module (structs such as ``Drf.Object``)."""
    tx.uf  # noqa: B018 - imports NXOpen.UF on first use
    return tx.nx.UF


def quiet_sheet_sketch(tx):
    """Deactivate a drafting sketch NX re-activated on opening a sheet (NX 2406 activates a
    sheet's last sheet sketch whenever the sheet is opened from the modeling display); a
    model sketch is left alone. Returns whether one was deactivated."""
    return sketch.release_drafting_sketch(tx.session, tx.nx)


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


def default_origin(sh, anchor):
    """An omitted annotation origin: up and right of the leader's attachment point."""
    step = DEFAULT_ORIGIN_OFFSET_MM / (25.4 if sh.units == "in" else 1.0)
    return [float(anchor[0]) + step, float(anchor[1]) + step]


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


def shown_table(tx, obj):
    """The table NX shows for ``obj``: a bare tabular note (``Table``, the cells behind a
    table section or title block, listed by NX apart) maps to its section or title block;
    any other object is returned as is."""
    if type(obj).__name__ != "Table":
        return obj
    tag = int(obj.Tag)
    part = tx.part
    for section in getattr(part.Annotations, "TableSections", None) or []:
        try:
            if int(tx.uf.Tabnot.AskTabularNoteOfSection(section.Tag)) == tag:
                return section
        except Exception:  # a section NX cannot answer for
            continue
    for block in getattr(getattr(part, "DraftingManager", None), "TitleBlocks", None) or []:
        handle = attribute(tx, block, NATIVE_TABLE)
        try:
            if handle and int(tx.uf.Tag.AskTagOfHandle(handle)) == tag:
                return block
        except Exception:  # a stale handle
            continue
    return obj


def annotation_kind(tx, obj):
    """The reference kind of a drafting object: ``dimension``, ``drawing_table`` or
    ``annotation``."""
    if int(obj.Tag) in {int(d.Tag) for d in tx.part.Dimensions}:
        return "dimension"
    return "drawing_table" if table_kind(tx, obj) else "annotation"


def report_bounds(tx, out, obj, sh, what="annotation"):
    """Add ``bounds`` (the whole extent on the sheet, sheet units) to ``out`` and warn
    NX_VIEW_OVERLAP when the text block (a table: the whole extent) lies over a view's
    border or off the sheet."""
    bounds, text = drawing.annotation_box(tx, obj, sh)
    if bounds is None:
        return out
    out["bounds"] = bounds
    views, off = drawing.crowding(tx, sh.sheet, text or bounds)
    if views:
        names = ", ".join(str(r.get("name") or r.get("id")) for r in views[:5])
        tx.warn("NX_VIEW_OVERLAP", f"The {what} lies over view {names}: move it with "
                "nx_edit_annotation position (see bounds and the views' bounds)")  # fmt: skip
    if off:
        tx.warn("NX_VIEW_OVERLAP", f"The {what} runs off the sheet (bounds {bounds}): move "
                "it with nx_edit_annotation position")  # fmt: skip
    return out
