"""``nx_create_sheet_metal_feature``: one tool, one variant per sheet-metal operation.

Each operation reads its fields from ``parameters`` (``OPERATIONS`` lists the required
and optional ones) and configures one native Sheet Metal builder; the result is read
back (thickness, bend count, angle, radius, neutral factor, volume) before the kernel
commits it.

NX 2406 findings encoded here:

- Builders run in the Sheet Metal application context (``SetApplicationContext``) and are
  committed only after ``Builder.Validate()``; the older ``ValidateBuilderData`` returned
  varying values for valid input.
- A Flat Pattern commit fails ("Cannot create feature") while a drawing sheet is shown:
  the modeling display is shown for the commit and the sheet reopened afterwards.
- The flange ``length`` is the inside length (``LengthReference = Inside``); its bend
  takes the part's default radius and neutral factor unless given.
- Resize Bend Angle takes the included angle (180 - bend angle) and by default keeps the
  developed length by changing the radius: the tool passes the bend angle like every
  other operation and keeps the radius fixed.
- A bend or jog folds the side of the bend line away from ``FixedSide``; the stationary
  face of a later unbend, rebend or resize must be on the side that stayed.
- Louver commits fail on this NX ("Cannot create feature", ValidateBuilderData 3675043)
  for every tested input, so the operation has no 2406 evidence.
- Remove Bends is not offered: its feature toggle is disabled on this NX installation.
- Lofted Flange needs ``StartSectionPoint``/``EndSectionPoint`` on corresponding free ends
  of its open profiles ("Point must lie on one end of the open section") and a bend
  segment count of 1..99 (the builder starts at 0).
- Gusset's ``DatumPlane`` takes an ``NXOpen.Plane`` (a ``DatumPlane`` is a TypeError); the
  tool places one gusset on the plane normal to the bend axis through its middle, which
  splits the bend face in two.
- Edge Rip with a sketch line on the web and ``Width`` removes a square-ended slot.
- Advanced Flange and Variational Flange need a license this seat lacks ("The check out
  of a feature failed because its token feature does not exist",
  ``NX_LICENSE_UNAVAILABLE``), so the operations have no 2406 evidence.
- Three Bend Corner takes two bends that meet around a third (convex corner: flanges on
  the web's and on a flange's side edges, both bending outward); two flanges of one web
  are refused (3675084, "A bend face pair is not valid"). Closed Corner takes those.
- Bridge Bend (``BridgeTransitionBuilder``) is a Zu transition between full edges of two
  separate sheet-metal bodies, which it merges into one.
- Bulge Relief is a feature (``SB_BulgeRelief``) on the outer arc edge at a bend's end;
  other edges are refused (3675678, "Not valid edge").
- A bead needs every size positive (radius 3675048, punched width 3675049); the tool makes
  circular, formed beads.
- Joggle needs a license this seat lacks: ``CreateJoggleBuilder`` fails with 948801
  (``NX_LICENSE_UNAVAILABLE``), so the operation has no 2406 evidence.
- Each operation's ``parameters`` form (``FORMS``) is its own schema, shown by
  ``nx_get_tool_schemas("nx_create_sheet_metal_feature#<operation>")``; the listed schema
  keeps a free-form object, so adding operations does not grow it.
- Convert to Sheet Metal takes only a base face of a constant-thickness solid; reading the
  builder's options before they are set fails with a license error on this seat. A sharp
  inner corner becomes a bend with the part's default radius.
- Sheet Metal from Solid needs its bend edges (``GetAutomaticBendEdges`` proposes the
  shared ones): without them the commit fails ("Cannot create feature"). It thickens the
  web faces into the solid and hides it, as a new body.
- Flat Solid adds a flat sheet-metal body (its bends ``flat``) beside the bent one.
- Lightening Cutout needs a license this seat lacks: ``CreateLighteningCutoutBuilder``
  fails with 948801 (``NX_LICENSE_UNAVAILABLE``), so the operation has no 2406 evidence.
- Solid Punch takes the tool body where it is (no CSYS needed) and NX's default punch
  type; the tool body stays visible.
"""

from __future__ import annotations

import contextlib
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ByVariant, Doc, Items, Json, Quantity, Range, RefTo, forbid_extra
from nx_mcp.core.validate import unwrap
from nx_mcp.domains.sheet_metal import common
from nx_mcp.kit import (
    builders,
    curve,
    drawing,
    geom,
    measure,
    model,
    parts,
    readback,
    result,
    select,
    sketch,
)

Operation = Literal[
    "tab",
    "flange",
    "contour_flange",
    "bend",
    "jog",
    "hem",
    "normal_cutout",
    "dimple",
    "louver",
    "drawn_cutout",
    "break_corner",
    "resize_bend_angle",
    "resize_bend_radius",
    "resize_neutral_factor",
    "unbend",
    "rebend",
    "flat_pattern",
    "lofted_flange",
    "advanced_flange",
    "variational_flange",
    "gusset",
    "edge_rip",
    "closed_corner",
    "three_bend_corner",
    "bridge_bend",
    "bend_taper",
    "joggle",
    "bulge_relief",
    "bead",
    "convert",
    "from_solid",
    "flat_solid",
    "lightening_cutout",
    "solid_punch",
]


HEMS = {
    "open": "OpenHemType",
    "closed": "ClosedHemType",
    "s_flange": "SFlangeHemType",
    "curl": "CurlHemType",
    "open_loop": "OpenLoopHemType",
    "closed_loop": "ClosedLoopHemType",
    "centered_loop": "CenteredLoopHemType",
}
CORNERS = {"fillet": "Fillet", "chamfer": "ChamferEqualSetback"}


class Fields(TypedDict, total=False):
    """Every ``parameters`` field; each operation's form takes the ones it uses."""

    section: Annotated[str, RefTo("sketch", doc="Ref: finished sketch")]
    end_section: Annotated[str, RefTo("sketch", doc="Ref: finished open end profile")]
    face: Annotated[str, RefTo("face", doc="Ref: face that stays put")]
    faces: Annotated[list[Annotated[str, RefTo("face", doc="Ref: bend face")]], Items(1, 50)]
    edges: Annotated[list[Annotated[str, RefTo("edge", doc="Ref: sheet edge")]], Items(1, 50)]
    edge: Annotated[str, RefTo("edge", doc="Ref: straight edge of face: the X axis")]
    body: Annotated[str, RefTo("body", doc="Ref: sheet-metal body")]
    tool: Annotated[str, RefTo("body", doc="Ref: solid tool body")]
    thickness: Annotated[float | str, Quantity("length"), Doc("Sheet thickness")]
    length: Annotated[float | str, Quantity("length"), Doc("Length")]
    angle: Annotated[float | str, Quantity("angle"), Doc("Bend angle")]
    height: Annotated[float | str, Quantity("length"), Doc("Form depth")]
    width: Annotated[float | str, Quantity("length"), Doc("Width")]
    diameter: Annotated[float | str, Quantity("length"), Doc("Hole diameter")]
    radius: Annotated[
        float | str, Quantity("length"), Doc("Inner bend radius (default: the part's)")
    ]
    neutral_factor: Annotated[float, Range(ge=0, le=1), Doc("Neutral factor 0..1")]
    reverse: Annotated[bool, Doc("Flip the thickness side, bend or form direction")]
    style: Annotated[
        Literal[
            "open",
            "closed",
            "s_flange",
            "curl",
            "open_loop",
            "closed_loop",
            "centered_loop",
            "fillet",
            "chamfer",
        ],
        Doc("Style"),
    ]


@dataclass(frozen=True)
class Op:
    factory: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    docs: Mapping[str, str] = MappingProxyType({})  # this operation's field descriptions


FLANGE_DOCS = {"length": "Inside length", "angle": "Bend angle (default 90)"}
OPERATIONS: dict[str, Op] = {
    "tab": Op(
        "CreateTabFeatureBuilder",
        ("section",),
        ("thickness", "body", "reverse"),
        {"body": "Ref: sheet-metal body of a secondary tab", "thickness": "Needed without body"},
    ),
    "flange": Op(
        "CreateMultiFlangeBuilder",
        ("edges", "length"),
        ("angle", "radius", "neutral_factor"),
        FLANGE_DOCS,
    ),
    "contour_flange": Op(
        "CreateContourFlangeFeatureBuilder",
        ("section", "thickness", "length"),
        ("radius", "neutral_factor", "reverse"),
        {"section": "Ref: finished open-profile sketch", "length": "Width"},
    ),
    "bend": Op(
        "CreateBendFeatureBuilder",
        ("section", "face", "angle"),
        ("radius", "neutral_factor", "reverse"),
        {"section": "Ref: sketch with the bend line"},
    ),
    "jog": Op(
        "CreateJogFeatureBuilder",
        ("section", "face", "height"),
        ("angle", "radius", "reverse"),
        {"section": "Ref: sketch with the jog line", "height": "Jog height"},
    ),
    "hem": Op(
        "CreateHemFlangeFeatureBuilder",
        ("edges", "length"),
        ("style", "radius"),
        {"length": "Hem flange length", "style": "Hem type (default open)"},
    ),
    "normal_cutout": Op("CreateNormalCutoutFeatureBuilder", ("section", "body")),
    "dimple": Op("CreateDimpleFeatureBuilder", ("section", "height"), ("reverse",)),
    "louver": Op("CreateLouverFeatureBuilder", ("section", "height", "width"), ("reverse",)),
    "drawn_cutout": Op("CreateDrawnCutoutFeatureBuilder", ("section", "height"), ("reverse",)),
    "break_corner": Op(
        "CreateBreakCornerFeatureBuilder",
        ("edges", "radius"),
        ("style",),
        {"radius": "Fillet radius or chamfer size", "style": "fillet (default) or chamfer"},
    ),
    "resize_bend_angle": Op("CreateResizeBendAngleBuilder", ("faces", "face", "angle")),
    "resize_bend_radius": Op("CreateResizeBendRadiusFeatureBuilder", ("faces", "face", "radius")),
    "resize_neutral_factor": Op("CreateResizeNeutralFactorBuilder", ("faces", "neutral_factor")),
    "unbend": Op("CreateUnbendFeatureBuilder", ("faces", "face")),
    "rebend": Op("CreateRebendFeatureBuilder", ("faces", "face")),
    "flat_pattern": Op(
        "CreateFlatPatternBuilder", ("face", "edge"), (), {"face": "Ref: planar web face, up"}
    ),
    "lofted_flange": Op(
        "CreateLoftedFlangeFeatureBuilder",
        ("section", "end_section", "thickness"),
        ("radius", "neutral_factor", "reverse"),
        {"section": "Ref: finished open start profile"},
    ),
    "advanced_flange": Op(
        "CreateAdvancedFlangeBuilder",
        ("edges", "length"),
        ("angle", "radius", "neutral_factor"),
        FLANGE_DOCS,
    ),
    "variational_flange": Op(
        "CreateVariationalFlangeBuilder",
        ("edges", "length"),
        ("angle", "radius", "neutral_factor"),
        FLANGE_DOCS,
    ),
    "gusset": Op(
        "CreateGussetBuilder",
        ("faces", "height", "width"),
        (),
        {"faces": "Ref: the one bend face", "height": "Gusset depth", "width": "Gusset width"},
    ),
    "edge_rip": Op(
        "CreateEdgeRipFeatureBuilder",
        ("section",),
        ("width",),
        {"section": "Ref: sketch with the rip line", "width": "Slot width (default NX's)"},
    ),
    "closed_corner": Op(
        "CreateClosedCornerFeatureBuilder", ("faces",), (), {"faces": "Ref: one of two bend faces"}
    ),
    "three_bend_corner": Op(
        "CreateThreeBendCornerFeatureBuilder",
        ("faces",),
        (),
        {"faces": "Ref: one of two bend faces"},
    ),
    "bridge_bend": Op(
        "CreateBridgeTransitionBuilder",
        ("edges",),
        (),
        {"edges": "Ref: start, then end edge, on two bodies"},
    ),
    "bend_taper": Op(
        "CreateBendTaperBuilder",
        ("faces", "face", "length"),
        (),
        {"faces": "Ref: the one bend face", "length": "Taper distance"},
    ),
    "joggle": Op(
        "CreateJoggleBuilder",
        ("faces", "section", "height"),
        ("radius", "reverse"),
        {"faces": "Ref: planar face", "section": "Ref: sketch with the start line"},
    ),
    "bulge_relief": Op(
        "CreateBulgeReliefBuilder",
        ("edges",),
        ("height", "width", "radius"),
        {"edges": "Ref: outer arc edge at a bend end", "height": "Relief depth"},
    ),
    "bead": Op(
        "CreateBeadFeatureBuilder",
        ("section", "height", "radius"),
        ("reverse",),
        {"section": "Ref: sketch with the bead path", "radius": "Cross-section radius"},
    ),
    "convert": Op(
        "CreateConvertToSheetmetalFeatureBuilder",
        ("face",),
        (),
        {"face": "Ref: base face of a constant-thickness solid"},
    ),
    "from_solid": Op(
        "CreateSheetMetalFromSolidBuilder",
        ("faces", "thickness"),
        ("edges", "radius", "neutral_factor", "reverse"),
        {
            "faces": "Ref: web face of a solid",
            "edges": "Ref: bend edge between webs (default: NX's)",
            "reverse": "Thicken to the other side of the webs",
        },
    ),
    "flat_solid": Op(
        "CreateFlatSolidFeatureBuilder", ("face", "edge"), (), {"face": "Ref: planar web face, up"}
    ),
    "lightening_cutout": Op(
        "CreateLighteningCutoutBuilder",
        ("section", "length"),
        ("angle", "radius", "reverse"),
        {
            "section": "Ref: sketch with the closed cutout profile",
            "length": "Formed flange length",
            "angle": "Flange angle (default 90)",
            "radius": "Die radius",
            "reverse": "Form the flange to the other side",
        },
    ),
    "solid_punch": Op(
        "CreateSolidPunchBuilder",
        ("face", "tool"),
        (),
        {"face": "Ref: sheet face the punch meets", "tool": "Ref: solid punch body, in place"},
    ),
}


def _hint(key, doc):
    """The shared field type of ``key``, described by ``doc`` when given."""
    hint = FIELD_HINTS[key]
    if doc is None:
        return hint
    base, markers = unwrap(hint)
    if typing.get_origin(base) is list:  # a list is described on its items
        item, item_markers = unwrap(typing.get_args(base)[0])
        kept = [m for t, m in item_markers.items() if t is not Doc]
        base = list[Annotated[(item, *kept, Doc(doc))]]
        return Annotated[(base, *markers.values())]
    kept = [m for t, m in markers.items() if t is not Doc]
    return Annotated[(base, *kept, Doc(doc))]


def _form(operation: str, op: Op) -> type:
    """The ``parameters`` TypedDict of one operation (``tool#operation`` shows it)."""
    name = "".join(w.capitalize() for w in operation.split("_")) + "Parameters"
    fields = {k: _hint(k, op.docs.get(k)) for k in op.required}
    required = TypedDict("_" + name, fields)  # type: ignore[misc]  # built per operation
    optional = {k: _hint(k, op.docs.get(k)) for k in op.optional}
    form = types.new_class(
        name, (required,), {"total": False}, lambda ns: ns.update(__annotations__=optional)
    )
    forbid_extra(form)
    return form


FIELD_HINTS = typing.get_type_hints(Fields, include_extras=True)
FORMS = {operation: _form(operation, op) for operation, op in OPERATIONS.items()}
Parameters = Annotated[
    dict,
    Json(max_keys=len(FIELD_HINTS)),
    ByVariant("operation", FORMS),
    Doc("Fields per operation: nx_get_tool_schemas(tool#operation)"),
]


POSITIVE = ("thickness", "length", "height", "width")
ANGLE_TOL = 1e-4  # degrees
NF_TOL = 1e-6


class FeatureData(TypedDict, total=False):
    feature: result.Row
    operation: str
    body: result.Row
    volume: float
    thickness: float
    bend_count: int
    bends: list[common.BendRow]
    view: str
    flat_extents: list[float]


# ---------------------------------------------------------------------------
# Arguments


def _q(a, key, say, dim="length"):
    value = a["parameters"].get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    unit = "deg" if dim == "angle" else say.units.get("length") or ""
    return f"{value:.6g} {unit}".strip()


def _summary(a, say) -> str:
    op, p = a["operation"], a["parameters"]
    count = len(p.get("edges") or p.get("faces") or [])
    if op == "tab":
        return "Secondary tab" if p.get("body") else f"Tab {_q(a, 'thickness', say)} thick"
    if op == "flange":
        return f"Flange {count} edge(s) {_q(a, 'length', say)} at {_q(a, 'angle', say, 'angle') or '90 deg'}"
    if op in ("bend", "resize_bend_angle"):
        return f"{op.replace('_', ' ').capitalize()} to {_q(a, 'angle', say, 'angle')}"
    if op == "jog":
        return f"Jog {_q(a, 'height', say)}"
    if op == "contour_flange":
        return f"Contour flange {_q(a, 'thickness', say)} thick"
    if op == "flat_pattern":
        return "Flat pattern"
    if count:
        return f"{op.replace('_', ' ').capitalize()} on {count} object(s)"
    return op.replace("_", " ").capitalize()


def check(operation, p) -> dict:
    """Check ``parameters`` of ``operation`` (signs, styles, counts) beyond its form; pure.

    Gate 1 already checked the fields against the operation's form (``FORMS``)."""
    if operation == "tab" and p.get("body") is None and p.get("thickness") is None:
        fail("NX_INVALID_ARGUMENT", "tab needs thickness (or body for a secondary tab)",
             field="/parameters/thickness")  # fmt: skip
    for key in POSITIVE:
        if p.get(key) is not None and p[key] <= 0:
            fail("NX_INVALID_ARGUMENT", f"{key} must be positive", field="/parameters/" + key)
    if p.get("radius") is not None and p["radius"] < 0:
        fail("NX_INVALID_ARGUMENT", "radius cannot be negative", field="/parameters/radius")
    if operation == "break_corner" and p["radius"] <= 0:
        fail("NX_INVALID_ARGUMENT", "radius must be positive", field="/parameters/radius")
    angle = p.get("angle")
    if angle is not None and not 0 < angle < 180:
        fail("NX_INVALID_ARGUMENT", "angle must lie between 0 and 180 degrees",
             field="/parameters/angle")  # fmt: skip
    style = p.get("style")
    if style is not None:
        styles = HEMS if operation == "hem" else CORNERS
        if style not in styles:
            fail("NX_INVALID_ARGUMENT", f"{operation} style is one of {', '.join(styles)}",
                 field="/parameters/style")  # fmt: skip
    count = FACE_COUNT.get(operation)
    if count and len(p["faces"]) != count:
        fail("NX_INVALID_ARGUMENT", f"{operation} takes {count} bend face(s)",
             field="/parameters/faces")  # fmt: skip
    if operation == "bridge_bend" and len(p["edges"]) != 2:
        fail("NX_INVALID_ARGUMENT", "bridge_bend takes a start and an end edge",
             field="/parameters/edges")  # fmt: skip
    return p


FACE_COUNT = {"resize_bend_angle": 1, "gusset": 1, "closed_corner": 2, "three_bend_corner": 2,
              "bend_taper": 1}  # fmt: skip


# ---------------------------------------------------------------------------
# Inputs


class Inputs:
    """Resolved live objects of one call, with the body they act on."""

    def __init__(self, tx, p):
        self.tx, self.p = tx, p
        self.sketch = self.end_sketch = self.face = self.edge = self.body = None
        self.faces, self.edges, self.bodies = [], [], []
        self.plane = None  # joggle: (origin, normal) of its start plane
        self.tool = None  # solid_punch: the solid tool body

    def resolve(self, operation):
        tx, p = self.tx, self.p
        where = "/parameters/"
        owner = common.solid_owner if operation in FROM_SOLID else common.owner_body
        if p.get("section"):
            self.sketch = select.resolve(tx, p["section"], "sketch", field=where + "section",
                                         work_part=True)  # fmt: skip
        if p.get("end_section"):
            self.end_sketch = select.resolve(tx, p["end_section"], "sketch",
                                             field=where + "end_section", work_part=True)  # fmt: skip
        if p.get("body"):
            self.body = common.sheet_body(tx, p["body"], where + "body")
        if p.get("faces"):
            self.faces = select.faces(tx, p["faces"], where + "faces")
            self.body = self._one_body(self.faces, where + "faces", owner)
        if p.get("edges"):
            self.edges = select.edges(tx, p["edges"], where + "edges")
            if operation == "bridge_bend":
                self.bodies = self._two_bodies(self.edges, where + "edges")
            else:
                body = self._one_body(self.edges, where + "edges", owner)
                if self.body is not None and int(body.Tag) != int(self.body.Tag):
                    fail("NX_WRONG_OWNER", "edges must belong to the body of the faces",
                         field=where + "edges")  # fmt: skip
                self.body = body
        if p.get("face"):
            self.face = select.one(tx, p["face"], "face", field=where + "face")
            body = owner(tx, self.face, where + "face")
            if self.body is not None and int(body.Tag) != int(self.body.Tag):
                fail("NX_WRONG_OWNER", "face must belong to the same sheet-metal body",
                     field=where + "face")  # fmt: skip
            self.body = body
        if p.get("edge"):
            self.edge = select.one(tx, p["edge"], "edge", field=where + "edge")
        if operation in ("resize_bend_angle", "resize_bend_radius", "resize_neutral_factor",
                         "unbend", "rebend", "gusset", "closed_corner", "three_bend_corner",
                         "bend_taper"):  # fmt: skip
            bend_faces = {int(f.Tag) for f, _ in common.bends(tx, self.body)}
            for i, face in enumerate(self.faces):
                if int(face.Tag) not in bend_faces:
                    fail("NX_WRONG_GEOMETRY", "Select inner bend faces (nx_get_sheet_metal)",
                         field=f"{where}faces/{i}")  # fmt: skip
            if self.face is not None and int(self.face.Tag) in bend_faces:
                fail("NX_INVALID_ARGUMENT", "face is the stationary face; a bend face moves",
                     field=where + "face")  # fmt: skip
        if p.get("tool"):
            self.tool = select.one(tx, p["tool"], "body", field=where + "tool")
            if not common.is_plain_solid(tx, self.tool):
                fail("NX_WRONG_GEOMETRY", "The tool must be a solid body, not sheet metal",
                     field=where + "tool")  # fmt: skip
        if operation in ("flat_pattern", "flat_solid"):
            self._orientation()
        if operation == "joggle":
            self._joggle_plane()
        if operation == "tab" and self.body is not None and p.get("thickness") is not None:
            have = common.thickness(tx, self.body)
            if not geom.close(have, float(p["thickness"]), 1e-6):
                fail("NX_INVALID_ARGUMENT",
                     f"A secondary tab takes its target's thickness ({have:.6g})",
                     field=where + "thickness")  # fmt: skip
        return self

    def _one_body(self, objects, field, owner=common.owner_body):
        bodies = {int(o.GetBody().Tag): o.GetBody() for o in objects}
        if len(bodies) != 1:
            fail("NX_INVALID_ARGUMENT", "Select geometry of one body", field=field)
        return owner(self.tx, objects[0], field)

    def _two_bodies(self, edges, field):
        bodies = [common.owner_body(self.tx, e, f"{field}/{k}") for k, e in enumerate(edges)]
        if int(bodies[0].Tag) == int(bodies[1].Tag):
            fail("NX_WRONG_GEOMETRY", "A bridge bend joins edges of two sheet-metal bodies",
                 field=field)  # fmt: skip
        return bodies

    def _joggle_plane(self):
        """The joggle starts on the plane through the section's line, normal to the face."""
        field = "/parameters/section"
        lines = [c for c in sketch.geometry(self.sketch) if type(c).__name__ == "Line"]
        if len(lines) != 1:
            fail("NX_WRONG_GEOMETRY", "A joggle section holds one line", field=field)
        start, end = geom.xyz(lines[0].StartPoint), geom.xyz(lines[0].EndPoint)
        kind, _point, normal = select.face_data(self.tx, self.faces[0])
        if kind != select.FACE_PLANE:
            fail("NX_WRONG_GEOMETRY", "A joggle offsets planar faces", field="/parameters/faces/0")
        axis = geom.cross(geom.unit(geom.sub(end, start), field), geom.unit(normal))
        self.plane = (start, geom.unit(axis, field))

    def _orientation(self):
        tx, where = self.tx, "/parameters/"
        kind, _point, _normal = select.face_data(tx, self.face)
        if kind != select.FACE_PLANE:
            fail("NX_WRONG_GEOMETRY", "The upward face must be a planar web face",
                 field=where + "face")  # fmt: skip
        if int(self.edge.Tag) not in {int(e.Tag) for e in self.face.GetEdges()}:
            fail("NX_WRONG_GEOMETRY", "The X-axis edge must be an edge of the upward face",
                 field=where + "edge",
                 hint="List them with nx_list_objects(kind=edge, owner=<face>, type=line)")  # fmt: skip
        if self.edge.SolidEdgeType != tx.nx.Edge.EdgeType.Linear:
            fail("NX_WRONG_GEOMETRY", "The X-axis edge must be straight", field=where + "edge")


# ---------------------------------------------------------------------------
# Builder configuration


def _enum(tx, name, member):
    return getattr(getattr(common.namespace(tx), name), member)


def _expression(target, value):
    target.RightHandSide = repr(float(value))


def _bend_options(opts, p):
    if p.get("radius") is not None:
        opts.UseGlobalBendRadius = False
        _expression(opts.BendRadius, p["radius"])
    if p.get("neutral_factor") is not None:
        opts.UseGlobalNeutralFactor = False
        _expression(opts.NeutralFactor, p["neutral_factor"])


def _collector(tx, objects, kind, target=None):
    rule = getattr(tx.part.ScRuleFactory, f"CreateRule{kind}Dumb")(list(objects))
    if target is None:
        return builders.collector(tx, objects, kind)
    target.ReplaceRules([rule], False)
    return target


def _tab(tx, b, i):
    b.Section = builders.section(tx, i.sketch)
    if i.body is not None:
        b.IsSecondary = True
        b.TargetBody = i.body
    else:
        _expression(b.Thickness, i.p["thickness"])
    if i.p.get("reverse"):
        b.ThicknessSide = _enum(tx, "TabBuilderThicknessSideOptions", "SectionReverseNormalSide")


def _flange(tx, b, i):
    p = i.p
    flanges = b.FlangePropertiesList
    flanges.FeatureBendPropertiesList.Clear(tx.nx.ObjectList.DeleteOption.Delete)
    entry = flanges.CreateFlangeBendProperties()
    flanges.FeatureBendPropertiesList.Append(entry)
    _collector(tx, i.edges, "Edge", entry.Edges)
    entry.LengthReference = _enum(tx, "FlangeBendPropertiesBuilderLengthReferences", "Inside")
    _expression(entry.Length, p["length"])
    _expression(entry.Angle, p.get("angle", 90.0))
    _bend_options(entry.BendOptions, p)


def _contour_flange(tx, b, i):
    p = i.p
    b.Section = builders.section(tx, i.sketch)
    _expression(b.GetThickness(), p["thickness"])
    _expression(b.GetSweepDistance(), p["length"])
    if p.get("reverse"):
        b.ThicknessSide = _enum(tx, "ContourFlangeBuilderSectionSideOptions", "Right")
    _bend_options(b.BendOptions, p)


def _bend(tx, b, i):
    p = i.p
    b.Section = builders.section(tx, i.sketch)
    b.TargetFace = i.face
    _expression(b.GetBendAngle(), p["angle"])
    if p.get("reverse"):
        b.Direction = _enum(tx, "BendBuilderBendDirectionOptions", "SectionReverseNormalSide")
    _bend_options(b.BendOptions, p)


def _jog(tx, b, i):
    p = i.p
    b.Section = builders.section(tx, i.sketch)
    b.TargetFace = i.face
    _expression(b.GetHeight(), p["height"])
    _expression(b.Angle, p.get("angle", 90.0))
    if p.get("reverse"):
        b.DirectionType = _enum(tx, "JogBuilderDirectionTypeOptions", "SectionReverseNormalSide")
    _bend_options(b.BendOptions, p)


def _hem(tx, b, i):
    p = i.p
    _collector(tx, i.edges, "Edge", b.EdgeChain)
    b.Type = _enum(tx, "HemFlangeBuilderTypeOptions", HEMS[p.get("style", "open")])
    _expression(b.FirstFlangeLength, p["length"])
    if p.get("radius") is not None:
        _expression(b.FirstBendRadius, p["radius"])


def _normal_cutout(tx, b, i):
    b.Section = builders.section(tx, i.sketch)
    b.TargetBody = i.body
    b.DepthType = _enum(tx, "NormalCutoutBuilderDepthTypeOptions", "ThroughAll")
    b.DepthSide = _enum(tx, "NormalCutoutBuilderDepthSideOptions", "Symmetric")


def _side(tx, b, name, attribute, reverse):
    member = "SectionReverseNormalSide" if reverse else "SectionNormalSide"
    setattr(b, attribute, _enum(tx, name, member))


def _dimple(tx, b, i):
    b.Section = builders.section(tx, i.sketch)
    _expression(b.GetDepth(), i.p["height"])
    _side(tx, b, "DimpleBuilderDepthTypeOptions", "DepthType", i.p.get("reverse"))


def _louver(tx, b, i):
    b.Section = builders.section(tx, i.sketch)
    _expression(b.Depth, i.p["height"])
    _expression(b.Width, i.p["width"])
    _side(tx, b, "LouverBuilderDepthSideOptions", "DepthSide", i.p.get("reverse"))


def _drawn_cutout(tx, b, i):
    b.Section = builders.section(tx, i.sketch)
    _expression(b.CutoutDepth, i.p["height"])
    _side(tx, b, "DrawnCutoutBuilderDepthTypeOptions", "DepthType", i.p.get("reverse"))


def _break_corner(tx, b, i):
    b.SetEdges(list(i.edges))
    b.Type = _enum(tx, "BreakCornerBuilderTypeOptions", CORNERS[i.p.get("style", "fillet")])
    _expression(b.Value, i.p["radius"])


def _resize_angle(tx, b, i):
    b.BendFace.Value = i.faces[0]
    b.ReferenceFace.Value = i.face
    b.KeepRadiusFixed = True  # otherwise NX keeps the developed length and moves the radius
    _expression(b.Angle, 180.0 - float(i.p["angle"]))  # NX: the included angle


def _resize_radius(tx, b, i):
    _collector(tx, i.faces, "Face", b.BendFaces)
    b.ReferenceEntity.Value = i.face
    _expression(b.BendRadius, i.p["radius"])


def _resize_factor(tx, b, i):
    _collector(tx, i.faces, "Face", b.BendFaces)
    b.UseGlobal = False
    _expression(b.NeutralFactor, i.p["neutral_factor"])


def _unbend(tx, b, i):
    b.FaceCollector = builders.collector(tx, i.faces, "Face")
    b.ReferenceEntity = i.face


def _flat_pattern(tx, b, i):
    b.UpwardFace.Value = i.face
    b.XAxisEdge.Value = i.edge


def _open_end(tx, sk, field, near=None):
    """A free end of an open sketch profile: the one nearest ``near``, else the lowest."""
    counts: dict = {}
    for c in sketch.geometry(sk):
        if type(c).__name__ == "Point":
            continue
        for q in curve.ends(tx, c):
            key = tuple(round(float(v), 6) for v in q)
            counts[key] = counts.get(key, 0) + 1
    free = [q for q, n in counts.items() if n == 1]
    if not free:
        fail("NX_WRONG_GEOMETRY", "A lofted_flange profile must be open", field=field)
    return min(free, key=lambda q: geom.dist(q, near)) if near else min(free)


def _lofted_flange(tx, b, i):
    p = i.p
    b.StartSection = builders.section(tx, i.sketch)
    b.EndSection = builders.section(tx, i.end_sketch)
    # NX: each point must lie on one end of its open section; the ends correspond.
    start = _open_end(tx, i.sketch, "/parameters/section")
    b.StartSectionPoint = tx.nx.Point3d(*start)
    end = _open_end(tx, i.end_sketch, "/parameters/end_section", start)
    b.EndSectionPoint = tx.nx.Point3d(*end)
    b.BendDivideParameter = _enum(
        tx, "LoftedFlangeBuilderBendDivideParameters", "NumberOfBendSegments"
    )
    b.NumberOfBendSegments = 8  # NX keeps 0 otherwise: "must be between 1 and 99"
    _expression(b.GetThickness(), p["thickness"])
    if p.get("reverse"):
        b.ThicknessSide = _enum(tx, "LoftedFlangeBuilderSectionSideOptions", "Right")
    _bend_options(b.BendOptions, p)


def _advanced_flange(tx, b, i):
    p = i.p
    _collector(tx, i.edges, "Edge", b.Edges)
    b.Type = _enum(tx, "AdvancedFlangeBuilderTypes", "ByValue")
    b.LengthReference = _enum(tx, "AdvancedFlangeBuilderLengthReferences", "Inside")
    _expression(b.Length, p["length"])
    _expression(b.Angle, p.get("angle", 90.0))
    _bend_options(b.BendOptions, p)


def _variational_flange(tx, b, i):
    p = i.p
    _collector(tx, i.edges, "Edge", b.Edges)
    b.LengthReference = _enum(tx, "VariationalFlangeBuilderLengthReferences", "Inside")
    b.LengthLawType = b.AngleLawType = _enum(tx, "VariationalFlangeBuilderLawTypes", "Constant")
    _expression(b.Length, p["length"])
    _expression(b.Angle, p.get("angle", 90.0))
    if p.get("radius") is not None:
        _expression(b.Radius, p["radius"])
    if p.get("neutral_factor") is not None:
        _expression(b.NeutralFactor, p["neutral_factor"])


def _gusset(tx, b, i):
    p, face = i.p, i.faces[0]
    b.BendFace.Value = face
    b.Type = _enum(tx, "GussetBuilderTypes", "AutomaticProfile")
    b.PlacementType = _enum(tx, "GussetBuilderPlacementTypes", "Single")
    b.WidthSide = _enum(tx, "GussetBuilderWidthSides", "Symmetric")
    # One gusset across the middle of the bend: a plane normal to the bend axis.
    _kind, point, axis = select.face_data(tx, face)
    low, high = measure.bounds(tx, [face])
    middle = geom.scale(geom.add(low, high), 0.5)
    axis = geom.unit(axis)
    origin = geom.add(point, geom.scale(axis, geom.dot(geom.sub(middle, point), axis)))
    nx = tx.nx  # GussetBuilder.DatumPlane takes an NXOpen.Plane, not a DatumPlane
    b.DatumPlane = tx.part.Planes.CreatePlane(
        nx.Point3d(*origin), nx.Vector3d(*axis), nx.SmartObject.UpdateOption.WithinModeling
    )
    _expression(b.Depth, p["height"])
    _expression(b.Width, p["width"])


def _edge_rip(tx, b, i):
    b.Section = builders.section(tx, i.sketch)
    if i.p.get("width") is not None:
        b.UseSystemWidth = False
        _expression(b.Width, i.p["width"])


def _closed_corner(tx, b, i):
    b.AddFacePair(i.faces[0], i.faces[1])


def _bridge_bend(tx, b, i):
    b.StartEdge.Value, b.EndEdge.Value = i.edges
    b.Type = _enum(tx, "BridgeTransitionBuilderTypeOptions", "Zu")
    b.WidthType = _enum(tx, "BridgeTransitionBuilderWidthOptions", "FullBothEdges")


def _bend_taper(tx, b, i):
    _collector(tx, i.faces, "Face", b.BendTaperSelectBendFace)
    b.StationaryEntity = i.face
    b.TaperSides = _enum(tx, "BendTaperBuilderBendTaperSides", "Both")
    distance = _enum(tx, "BendTaperBuilderBendTaperInputMethod", "Distance")
    b.BendTaperInputMethod1 = b.BendTaperInputMethod2 = distance
    _expression(b.TaperDistance1, i.p["length"])
    _expression(b.TaperDistance2, i.p["length"])


def _joggle(tx, b, i):
    p, nx = i.p, tx.nx
    b.InputList.Clear(nx.ObjectList.DeleteOption.Delete)
    item = b.CreateJoggleInputListItem()
    b.InputList.Append(item)
    _collector(tx, i.faces, "Face", item.Faces)
    _expression(item.Depth, p["height"])
    item.ReverseDirection = bool(p.get("reverse"))
    origin, normal = i.plane
    b.StartPlane = tx.part.Planes.CreatePlane(
        nx.Point3d(*origin), nx.Vector3d(*normal), nx.SmartObject.UpdateOption.WithinModeling
    )
    radius = p.get("radius", common.thickness(tx, i.body))
    side = b.Side1Options
    _expression(side.Runout, 2 * float(p["height"]))
    _expression(side.StationaryRadius, radius)
    _expression(side.OffsetRadius, radius)


def _bulge_relief(tx, b, i):
    p = i.p
    _collector(tx, i.edges, "Edge", b.BendEdges)
    if p.get("height") is not None:
        _expression(b.Depth, p["height"])
    if p.get("width") is not None:
        b.WidthType = _enum(tx, "BulgeReliefBuilderWidthTypes", "Value")
        _expression(b.Width, p["width"])
    if p.get("radius") is not None:
        _expression(b.Radius, p["radius"])


def _bead(tx, b, i):
    p = i.p
    b.Section = builders.section(tx, i.sketch)
    b.CrossSectionType = _enum(tx, "BeadBuilderCrossSectionTypeOptions", "Circular")
    b.EndType = _enum(tx, "BeadBuilderEndTypeOptions", "Formed")
    _expression(b.Height, p["height"])
    _expression(b.Radius, p["radius"])
    _expression(b.PunchedWidth, 2 * float(p["radius"]))  # unused by formed ends; NX needs > 0
    _side(tx, b, "BeadBuilderHeightSideOptions", "HeightSide", p.get("reverse"))


def _convert(tx, b, i):
    b.BaseFace = i.face


def _from_solid(tx, b, i):
    p = i.p
    _collector(tx, i.faces, "Face", b.WebFaces)
    if not i.edges:  # NX proposes the edges two web faces share
        i.edges = list(b.GetAutomaticBendEdges())
        if not i.edges:
            fail("NX_WRONG_GEOMETRY", "The web faces share no bend edge; give edges",
                 field="/parameters/faces")  # fmt: skip
    _collector(tx, i.edges, "Edge", b.BendEdges)
    b.UseGlobalThickness = False
    _expression(b.Thickness, p["thickness"])
    b.ReverseDirection = bool(p.get("reverse"))
    _bend_options(b.BendOptions, p)


def _flat_solid(tx, b, i):
    b.StationaryFace.Value = i.face
    b.XAxisEdge.Value = i.edge


def _lightening_cutout(tx, b, i):
    p = i.p
    b.Type = _enum(tx, "LighteningCutoutBuilderCutoutType", "UserDefined")
    b.UserDefinedSection = builders.section(tx, i.sketch)
    _expression(b.Length, p["length"])
    _expression(b.Angle, p.get("angle", 90.0))
    if p.get("radius") is not None:
        _expression(b.DieRadius, p["radius"])
    b.ReverseBendDirection = bool(p.get("reverse"))


def _solid_punch(tx, b, i):
    b.TargetFace.Value = i.face  # NX's default punch type; the tool stays where it is
    b.ToolBody.Value = i.tool


CONFIGURE = {
    "tab": _tab,
    "flange": _flange,
    "contour_flange": _contour_flange,
    "bend": _bend,
    "jog": _jog,
    "hem": _hem,
    "normal_cutout": _normal_cutout,
    "dimple": _dimple,
    "louver": _louver,
    "drawn_cutout": _drawn_cutout,
    "break_corner": _break_corner,
    "resize_bend_angle": _resize_angle,
    "resize_bend_radius": _resize_radius,
    "resize_neutral_factor": _resize_factor,
    "unbend": _unbend,
    "rebend": _unbend,
    "flat_pattern": _flat_pattern,
    "lofted_flange": _lofted_flange,
    "advanced_flange": _advanced_flange,
    "variational_flange": _variational_flange,
    "gusset": _gusset,
    "edge_rip": _edge_rip,
    "closed_corner": _closed_corner,
    "three_bend_corner": _closed_corner,
    "bridge_bend": _bridge_bend,
    "bend_taper": _bend_taper,
    "joggle": _joggle,
    "bulge_relief": _bulge_relief,
    "bead": _bead,
    "convert": _convert,
    "from_solid": _from_solid,
    "flat_solid": _flat_solid,
    "lightening_cutout": _lightening_cutout,
    "solid_punch": _solid_punch,
}
CREATES_BODY = ("tab", "contour_flange", "lofted_flange")
FROM_SOLID = ("convert", "from_solid")  # inputs on a solid that is not sheet metal
MAKES_BODY = ("from_solid", "flat_solid")  # a new body next to the input body
FLANGES = ("flange", "advanced_flange", "variational_flange")
NEW_BENDS = {"flange": 1, "advanced_flange": 1, "variational_flange": 1, "bend": 1, "jog": 2,
             "bridge_bend": 2}  # fmt: skip
KEEPS_BENDS = ("three_bend_corner", "bend_taper", "bulge_relief", "bead", "solid_punch")
VOLUME = {
    "advanced_flange": "more",
    "variational_flange": "more",
    "gusset": "differs",
    "edge_rip": "less",
    "closed_corner": "differs",
    "three_bend_corner": "more",
    "bridge_bend": "more",
    "bend_taper": "less",
    "joggle": "differs",
    "bulge_relief": "less",
    "bead": "differs",
    "flange": "more",
    "hem": "more",
    "normal_cutout": "less",
    "break_corner": "less",
    "dimple": "differs",
    "louver": "differs",
    "drawn_cutout": "differs",
    "lightening_cutout": "less",
    "solid_punch": "differs",
}


# ---------------------------------------------------------------------------
# Read-back


def _snapshot(tx, body):
    bends = common.bends(tx, body) if common.is_sheet_metal(tx, body) else []
    return {"volume": measure.volume(tx, body), "bends": bends}


def _candidates(tx, i, operation):
    """Before-snapshots of the bodies the feature may change, by tag."""
    if i.body is not None:
        bodies = [i.body]
    elif i.bodies:
        bodies = i.bodies
    elif operation in CREATES_BODY:
        bodies = []
    else:  # a form or cutout lands on the body its sketch lies on
        bodies = [b for b in tx.part.Bodies if common.is_sheet_metal(tx, b)]
    return {int(b.Tag): _snapshot(tx, b) for b in bodies}


def _volume_change(before, after, how):
    band = 1e-9 * max(1.0, abs(before), abs(after))
    ok = {
        "more": after > before + band,
        "less": after < before - band,
        "differs": abs(after - before) > band,
    }[how]
    readback.expect(ok, "volume", f"NX left the volume {before:.6g} -> {after:.6g}",
                    before=before, after=after)  # fmt: skip


def _verify(tx, operation, i, before, body, after):
    p = i.p
    if operation == "from_solid":
        readback.expect_close(after["thickness"], float(p["thickness"]), "thickness",
                              tol=1e-6 * max(1.0, float(p["thickness"])))  # fmt: skip
        readback.expect(len(after["bends"]) == len(i.edges), "bend_count",
                        f"NX made {len(after['bends'])} bends, not {len(i.edges)}")  # fmt: skip
        if p.get("radius") is not None:
            radius = float(p["radius"])
            readback.expect(all(abs(d["radius"] - radius) <= 1e-6 * max(1.0, radius)
                                for _f, d in after["bends"]), "radius")  # fmt: skip
        return
    if operation == "flat_solid":
        have = common.thickness(tx, i.body)
        readback.expect_close(after["thickness"], have, "thickness", tol=1e-6 * max(1.0, have))
        return
    if operation in CREATES_BODY:
        if i.body is None:
            readback.expect_close(after["thickness"], float(p["thickness"]), "thickness",
                                  tol=1e-6 * max(1.0, float(p["thickness"])))  # fmt: skip
        else:
            _volume_change(before["volume"], after["volume"], "more")
    gained = len(after["bends"]) - len(before["bends"]) if before else len(after["bends"])
    if operation in NEW_BENDS:
        want = NEW_BENDS[operation] * (len(i.edges) if operation in FLANGES else 1)
        readback.expect(gained == want, "bend_count", f"NX added {gained} bends, not {want}",
                        gained=gained, wanted=want)  # fmt: skip
    if operation in KEEPS_BENDS:
        readback.expect(gained == 0, "bend_count", f"NX changed the bend count by {gained}",
                        gained=gained)  # fmt: skip
    if operation in (*FLANGES, "bend"):
        angle = float(p.get("angle", 90.0))
        matches = [d for _f, d in after["bends"] if abs(d["angle"] - angle) <= ANGLE_TOL]
        readback.expect(len(matches) >= gained, "angle", f"No new bend at {angle:g} degrees",
                        bends=[d for _f, d in after["bends"]])  # fmt: skip
    if p.get("radius") is not None and operation in (*FLANGES, *CREATES_BODY[1:], "bend", "jog"):
        radius = float(p["radius"])
        tol = 1e-6 * max(1.0, radius)
        readback.expect(any(abs(d["radius"] - radius) <= tol for _f, d in after["bends"]),
                        "radius", f"No bend has radius {radius:g}")  # fmt: skip
    if operation in VOLUME:
        _volume_change(before["volume"], after["volume"], VOLUME[operation])
    if operation.startswith("resize_") or operation in ("unbend", "rebend"):
        _verify_bends(tx, operation, i, before, after)


def _verify_bends(tx, operation, i, before, after):
    p, n = i.p, len(i.faces)
    found = [d for _f, d in after["bends"]]
    readback.expect(len(found) == len(before["bends"]), "bend_count",
                    "The bend count changed", before=len(before["bends"]), after=len(found))  # fmt: skip
    if operation == "resize_bend_angle":
        hits = [d for d in found if abs(d["angle"] - float(p["angle"])) <= ANGLE_TOL]
    elif operation == "resize_bend_radius":
        radius = float(p["radius"])
        hits = [d for d in found if abs(d["radius"] - radius) <= 1e-6 * max(1.0, radius)]
    elif operation == "resize_neutral_factor":
        hits = [d for d in found if abs(d["neutral_factor"] - p["neutral_factor"]) <= NF_TOL]
    else:
        state = "flat" if operation == "unbend" else "bent"
        hits = [d for d in found if d["state"] == state]
    readback.expect(len(hits) >= n, operation, f"{operation} did not change {n} bend(s)",
                    bends=found)  # fmt: skip


def _flat_extents(tx, curves, i, what="flat_pattern") -> list[float]:
    """``[along the X-axis edge, across it, along the upward face's normal]`` extents of
    the Flat Pattern's curves or the Flat Solid's edges.

    Lines and straight edges are measured at their ends, other curves at 64 points
    (``NX_APPROXIMATED`` within 0.12 % of their radius).
    """
    _kind, _point, normal = select.face_data(tx, i.face)
    start, end = (geom.xyz(v) for v in i.edge.GetVertices())
    x = geom.unit(geom.sub(end, start))
    z = geom.unit(normal)
    y = geom.unit(geom.cross(z, x))
    points, sampled = [], False
    for c in curves:
        if hasattr(c, "StartPoint") and type(c).__name__ == "Line":
            points += [geom.xyz(c.StartPoint), geom.xyz(c.EndPoint)]
            continue
        if getattr(c, "SolidEdgeType", None) == tx.nx.Edge.EdgeType.Linear:
            points += [geom.xyz(v) for v in c.GetVertices()]
            continue
        sampled = True
        with curve.evaluator(tx, c) as e:
            points += [e.at(k / 64)[0] for k in range(65)]
    readback.expect(bool(points), what, f"The {what} has no curves")
    if sampled:
        tx.warn("NX_APPROXIMATED", f"{what} extents across arcs are sampled")
    return [
        max(geom.dot(q, axis) for q in points) - min(geom.dot(q, axis) for q in points)
        for axis in (x, y, z)
    ]


def _result(tx, operation, feature, body, after, view=None) -> FeatureData:
    out: FeatureData = {
        "feature": tx.ref(feature, "feature"),
        "operation": operation,
        "body": tx.ref(body, "body"),
        "volume": after["volume"],
        "thickness": after["thickness"],
        "bend_count": len(after["bends"]),
        "bends": common.bend_rows(tx, after["bends"]),
    }
    if view is not None:
        out["view"] = view
    return out


# ---------------------------------------------------------------------------
# The tool


@tool(
    effect="model",
    label="Create sheet-metal feature",
    summary=_summary,
    keywords=("tab", "flange", "bend", "hem", "jog", "cutout", "unbend", "flat pattern"),
    requires=("work_part", "no_active_sketch", "application:sheet_metal"),
    variants="operation",
    cost="heavy",
    example={"operation": "tab", "parameters": {"section": "s:3", "thickness": "2 mm"}},
)
def nx_create_sheet_metal_feature(
    tx: Tx,
    *,
    operation: Operation,
    parameters: Parameters,
) -> FeatureData:
    """Create one sheet-metal feature. Each operation's parameters are listed by
    nx_get_tool_schemas("nx_create_sheet_metal_feature#<operation>"); needs the Sheet Metal
    application (nx_activate_application)."""
    p = check(operation, dict(parameters))
    spec = OPERATIONS[operation]
    with tx.checks():
        manager = common.manager(tx)
        i = Inputs(tx, p).resolve(operation)
        snapshots = _candidates(tx, i, operation)
        factory = getattr(manager, spec.factory, None)
        if factory is None:
            fail("NX_UNSUPPORTED_OPTION", f"This NX has no {operation} builder",
                 field="/operation")  # fmt: skip
    sm = common.namespace(tx)
    view = None
    leave = operation == "flat_pattern"
    # The flat pattern commits with no sheet shown; should NX leave Sheet Metal on the
    # way, it is re-entered (and recorded) before the sheet reopens.
    shown = drawing.in_modeling(tx) if leave else contextlib.nullcontext()
    kept = parts.keeping_application(tx) if leave else contextlib.nullcontext()
    with shown, kept, builders.build(tx, factory, None) as b:
        if b.has("SetApplicationContext"):
            b.SetApplicationContext(sm.ApplicationContext.NxSheetMetal)
        CONFIGURE[operation](tx, b, i)
        feature = b.commit(validate=True)
        if operation == "flat_pattern":
            view = str(b.FlatPatternViewName or "")
    model.update(tx)
    if operation == "flat_pattern":
        readback.expect(str(feature.FeatureType) == "FLAT_PATTERN", "feature_type",
                        got=str(feature.FeatureType))  # fmt: skip
    body = i.body
    if body is None or operation in MAKES_BODY:
        made = [b for b in feature.GetBodies() if common.is_sheet_metal(tx, b)]
        readback.expect(len(made) == 1, "body", f"NX built {len(made)} sheet-metal bodies")
        body = made[0]
    before = snapshots.get(int(body.Tag))
    if operation == "bridge_bend":  # the two sheets become one body
        before = {"volume": sum(s["volume"] for s in snapshots.values()),
                  "bends": [b for s in snapshots.values() for b in s["bends"]]}  # fmt: skip
    if operation in MAKES_BODY:
        before = None
    elif before is None and operation not in CREATES_BODY:
        readback.expect(False, "body", "The feature changed an unexpected body")
    if operation == "convert":
        readback.expect(common.is_sheet_metal(tx, body), "sheet_metal",
                        "The body is not sheet metal after the conversion")  # fmt: skip
    after = {
        "volume": measure.volume(tx, body),
        "thickness": common.thickness(tx, body),
        "bends": common.bends(tx, body),
    }
    _verify(tx, operation, i, before, body, after)
    out = _result(tx, operation, feature, body, after, view)
    if operation == "flat_pattern":
        out["flat_extents"] = _flat_extents(tx, feature.GetCurves(), i)[:2]
    if operation == "flat_solid":  # flat: as thick as the sheet across the upward face
        extents = _flat_extents(tx, body.GetEdges(), i, "flat_solid")
        readback.expect_close(extents[2], after["thickness"], "flat",
                              tol=1e-6 * max(1.0, after["thickness"]))  # fmt: skip
        out["flat_extents"] = extents[:2]
    return out
