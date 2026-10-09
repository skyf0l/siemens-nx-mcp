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
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import REF_PATTERN, Doc, Items, Quantity, Range, RefKind, Text
from nx_mcp.domains.sheet_metal import common
from nx_mcp.kit import builders, curve, drawing, geom, measure, model, readback, result, select

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
]


def _ref(kind, doc):
    return Annotated[str, Text(128, REF_PATTERN), RefKind(kind), Doc(doc)]


def _length(doc):
    return Annotated[float | str, Quantity("length"), Doc(doc)]


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


class Parameters(TypedDict, total=False):
    section: _ref("sketch", "Ref: finished sketch (profile, bend line, cutout or form)")  # type: ignore[valid-type]
    face: _ref("face", "Ref: target, stationary, reference or flat-pattern upward face")  # type: ignore[valid-type]
    faces: Annotated[list[_ref("face", "Ref: bend face")], Items(1, 50)]  # type: ignore[valid-type]
    edges: Annotated[list[_ref("edge", "Ref: sheet edge")], Items(1, 50)]  # type: ignore[valid-type]
    edge: _ref("edge", "Ref: flat-pattern X axis, a straight edge of the upward face")  # type: ignore[valid-type]
    body: _ref("body", "Ref: sheet-metal body (normal_cutout, secondary tab)")  # type: ignore[valid-type]
    thickness: _length("tab, contour_flange: sheet thickness")
    length: _length("flange: inside length; contour_flange: width; hem: flange")
    angle: Annotated[float | str, Quantity("angle"), Doc("Bend, flange or jog angle")]
    height: _length("jog height; dimple, louver, drawn_cutout depth")
    width: _length("louver width")
    radius: _length("Inner bend radius (default: part's), or break-corner size")
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
        Doc("hem type (default open), or break_corner fillet/chamfer"),
    ]


Parameters.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Op:
    factory: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()


OPERATIONS: dict[str, Op] = {
    "tab": Op("CreateTabFeatureBuilder", ("section",), ("thickness", "body", "reverse")),
    "flange": Op(
        "CreateMultiFlangeBuilder", ("edges", "length"), ("angle", "radius", "neutral_factor")
    ),
    "contour_flange": Op(
        "CreateContourFlangeFeatureBuilder",
        ("section", "thickness", "length"),
        ("radius", "neutral_factor", "reverse"),
    ),
    "bend": Op(
        "CreateBendFeatureBuilder",
        ("section", "face", "angle"),
        ("radius", "neutral_factor", "reverse"),
    ),
    "jog": Op(
        "CreateJogFeatureBuilder", ("section", "face", "height"), ("angle", "radius", "reverse")
    ),
    "hem": Op("CreateHemFlangeFeatureBuilder", ("edges", "length"), ("style", "radius")),
    "normal_cutout": Op("CreateNormalCutoutFeatureBuilder", ("section", "body")),
    "dimple": Op("CreateDimpleFeatureBuilder", ("section", "height"), ("reverse",)),
    "louver": Op("CreateLouverFeatureBuilder", ("section", "height", "width"), ("reverse",)),
    "drawn_cutout": Op("CreateDrawnCutoutFeatureBuilder", ("section", "height"), ("reverse",)),
    "break_corner": Op("CreateBreakCornerFeatureBuilder", ("edges", "radius"), ("style",)),
    "resize_bend_angle": Op("CreateResizeBendAngleBuilder", ("faces", "face", "angle")),
    "resize_bend_radius": Op("CreateResizeBendRadiusFeatureBuilder", ("faces", "face", "radius")),
    "resize_neutral_factor": Op("CreateResizeNeutralFactorBuilder", ("faces", "neutral_factor")),
    "unbend": Op("CreateUnbendFeatureBuilder", ("faces", "face")),
    "rebend": Op("CreateRebendFeatureBuilder", ("faces", "face")),
    "flat_pattern": Op("CreateFlatPatternBuilder", ("face", "edge")),
}
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
    """Validate ``parameters`` for ``operation`` (fields, signs, styles); pure."""
    spec = OPERATIONS[operation]
    allowed = set(spec.required) | set(spec.optional)
    for key in p:
        if key not in allowed:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{operation} does not use {key}; it takes "
                + ", ".join(spec.required + tuple(k + "?" for k in spec.optional)),
                field="/parameters/" + key,
            )
    missing = [k for k in spec.required if p.get(k) is None]
    if operation == "tab" and p.get("body") is None and p.get("thickness") is None:
        missing.append("thickness")
    if missing:
        fail("NX_INVALID_ARGUMENT", f"{operation} needs {', '.join(missing)}",
             field="/parameters/" + missing[0])  # fmt: skip
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
    if operation == "resize_bend_angle" and len(p["faces"]) != 1:
        fail("NX_INVALID_ARGUMENT", "resize_bend_angle takes one bend face",
             field="/parameters/faces")  # fmt: skip
    return p


# ---------------------------------------------------------------------------
# Inputs


class Inputs:
    """Resolved live objects of one call, with the body they act on."""

    def __init__(self, tx, p):
        self.tx, self.p = tx, p
        self.sketch = self.face = self.edge = self.body = None
        self.faces, self.edges = [], []

    def resolve(self, operation):
        tx, p = self.tx, self.p
        where = "/parameters/"
        if p.get("section"):
            self.sketch = select.resolve(tx, p["section"], "sketch", field=where + "section",
                                         work_part=True)  # fmt: skip
        if p.get("body"):
            self.body = common.sheet_body(tx, p["body"], where + "body")
        if p.get("faces"):
            self.faces = select.faces(tx, p["faces"], where + "faces")
            self.body = self._one_body(self.faces, where + "faces")
        if p.get("edges"):
            self.edges = select.edges(tx, p["edges"], where + "edges")
            self.body = self._one_body(self.edges, where + "edges")
        if p.get("face"):
            self.face = select.one(tx, p["face"], "face", field=where + "face")
            body = common.owner_body(tx, self.face, where + "face")
            if self.body is not None and int(body.Tag) != int(self.body.Tag):
                fail("NX_WRONG_OWNER", "face must belong to the same sheet-metal body",
                     field=where + "face")  # fmt: skip
            self.body = body
        if p.get("edge"):
            self.edge = select.one(tx, p["edge"], "edge", field=where + "edge")
        if operation in ("resize_bend_angle", "resize_bend_radius", "resize_neutral_factor",
                         "unbend", "rebend"):  # fmt: skip
            bend_faces = {int(f.Tag) for f, _ in common.bends(tx, self.body)}
            for i, face in enumerate(self.faces):
                if int(face.Tag) not in bend_faces:
                    fail("NX_WRONG_GEOMETRY", "Select inner bend faces (nx_get_sheet_metal)",
                         field=f"{where}faces/{i}")  # fmt: skip
            if self.face is not None and int(self.face.Tag) in bend_faces:
                fail("NX_INVALID_ARGUMENT", "face is the stationary face; a bend face moves",
                     field=where + "face")  # fmt: skip
        if operation == "flat_pattern":
            self._orientation()
        if operation == "tab" and self.body is not None and p.get("thickness") is not None:
            have = common.thickness(tx, self.body)
            if not geom.close(have, float(p["thickness"]), 1e-6):
                fail("NX_INVALID_ARGUMENT",
                     f"A secondary tab takes its target's thickness ({have:.6g})",
                     field=where + "thickness")  # fmt: skip
        return self

    def _one_body(self, objects, field):
        bodies = {int(o.GetBody().Tag): o.GetBody() for o in objects}
        if len(bodies) != 1:
            fail("NX_INVALID_ARGUMENT", "Select geometry of one sheet-metal body", field=field)
        return common.owner_body(self.tx, objects[0], field)

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
}
CREATES_BODY = ("tab", "contour_flange")
NEW_BENDS = {"flange": 1, "bend": 1, "jog": 2}
VOLUME = {
    "flange": "more",
    "hem": "more",
    "normal_cutout": "less",
    "break_corner": "less",
    "dimple": "differs",
    "louver": "differs",
    "drawn_cutout": "differs",
}


# ---------------------------------------------------------------------------
# Read-back


def _snapshot(tx, body):
    return {"volume": measure.volume(tx, body), "bends": common.bends(tx, body)}


def _candidates(tx, i, operation):
    """Before-snapshots of the bodies the feature may change, by tag."""
    if i.body is not None:
        bodies = [i.body]
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
    if operation in CREATES_BODY:
        if i.body is None:
            readback.expect_close(after["thickness"], float(p["thickness"]), "thickness",
                                  tol=1e-6 * max(1.0, float(p["thickness"])))  # fmt: skip
        else:
            _volume_change(before["volume"], after["volume"], "more")
    gained = len(after["bends"]) - len(before["bends"]) if before else len(after["bends"])
    if operation in NEW_BENDS:
        want = NEW_BENDS[operation] * (len(i.edges) or 1)
        readback.expect(gained == want, "bend_count", f"NX added {gained} bends, not {want}",
                        gained=gained, wanted=want)  # fmt: skip
    if operation in ("flange", "bend"):
        angle = float(p.get("angle", 90.0))
        matches = [d for _f, d in after["bends"] if abs(d["angle"] - angle) <= ANGLE_TOL]
        readback.expect(len(matches) >= gained, "angle", f"No new bend at {angle:g} degrees",
                        bends=[d for _f, d in after["bends"]])  # fmt: skip
    if p.get("radius") is not None and operation in ("flange", "bend", "jog", "contour_flange"):
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


def _flat_extents(tx, feature, i) -> list[float]:
    """``[along the X-axis edge, across it]`` extents of the Flat Pattern's curves.

    The curves lie on the upward face; lines are measured at their ends, other
    curves at 64 points (``NX_APPROXIMATED`` within 0.12 % of their radius).
    """
    _kind, _point, normal = select.face_data(tx, i.face)
    start, end = (geom.xyz(v) for v in i.edge.GetVertices())
    x = geom.unit(geom.sub(end, start))
    y = geom.unit(geom.cross(geom.unit(normal), x))
    points, sampled = [], False
    for c in feature.GetCurves():
        if hasattr(c, "StartPoint") and type(c).__name__ == "Line":
            points += [geom.xyz(c.StartPoint), geom.xyz(c.EndPoint)]
            continue
        sampled = True
        with curve.evaluator(tx, c) as e:
            points += [e.at(k / 64)[0] for k in range(65)]
    readback.expect(bool(points), "flat_pattern", "The Flat Pattern has no curves")
    if sampled:
        tx.warn("NX_APPROXIMATED", "Flat-pattern extents across arcs are sampled")
    return [
        max(geom.dot(q, axis) for q in points) - min(geom.dot(q, axis) for q in points)
        for axis in (x, y)
    ]


def _result(tx, operation, feature, body, after, view=None) -> dict:
    out = {
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
    """Create one sheet-metal feature. Parameters take the fields their descriptions assign
    to the operation; needs the Sheet Metal application (nx_activate_application)."""
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
    shown = drawing.in_modeling(tx) if leave else contextlib.nullcontext()
    with shown, builders.build(tx, factory, None) as b:
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
    if body is None:
        made = [b for b in feature.GetBodies() if common.is_sheet_metal(tx, b)]
        readback.expect(len(made) == 1, "body", f"NX built {len(made)} sheet-metal bodies")
        body = made[0]
    before = snapshots.get(int(body.Tag))
    if before is None and operation not in CREATES_BODY:
        readback.expect(False, "body", "The feature changed an unexpected body")
    after = {
        "volume": measure.volume(tx, body),
        "thickness": common.thickness(tx, body),
        "bends": common.bends(tx, body),
    }
    _verify(tx, operation, i, before, body, after)
    out = _result(tx, operation, feature, body, after, view)
    if operation == "flat_pattern":
        out["flat_extents"] = _flat_extents(tx, feature, i)
    return out
