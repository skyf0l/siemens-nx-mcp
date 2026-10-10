"""Inter-part (top-down) design: WAVE links between components and extracted geometry.

NX 2406 findings encoded here:

- ``BaseFeatures.CreateWaveLinkBuilder(None)`` is one builder for every link kind; its
  ``Type`` picks the sub-builder that is read: ``ExtractFaceBuilder`` (body and face
  links, ``ParentPart = OtherPart``), ``CompositeCurveBuilder`` (curves and edges),
  ``WaveDatumBuilder`` (datum planes, axes and CSYS) and ``WaveSketchBuilder``.
- Sources are occurrences in the displayed assembly; a prototype object plus its
  ``component`` maps to the occurrence with ``component.FindOccurrence``. The target
  is the work part, a component part of that assembly (``Parts.WorkComponent``).
- ``Features.CreateExtractFaceBuilder(None)`` extracts bodies, faces or a region of
  faces (seed face inside boundary faces) inside the work part.
- Results are read back in assembly coordinates: a linked body keeps the source
  volume, a linked face the source area, linked curves the source length, a linked
  datum lies on the source datum (the work component's pose maps it back).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ComponentRef, Doc, FaceRef, Items, Point3, RefTo, Vec3
from nx_mcp.domains.assembly import structure
from nx_mcp.kit import builders, display, geom, measure, readback, result, select
from nx_mcp.kit.bodies import is_body

LinkKind = Literal["body", "face", "curve", "datum", "sketch"]
ExtractKind = Literal["body", "face", "region"]
SOURCE_KINDS = {
    "body": ("body",),
    "face": ("face",),
    "curve": ("curve", "edge"),
    "datum": ("datum_plane", "datum_axis", "datum_csys"),
    "sketch": ("sketch",),
}
LINK_TYPES = {
    "body": "BodyLink",
    "face": "FaceLink",
    "curve": "CurveLink",
    "datum": "DatumLink",
    "sketch": "SketchLink",
}
EXTRACT_TYPES = {"body": "Body", "face": "Face", "region": "RegionOfFaces"}
SourceRef = Annotated[str, RefTo("body", "face", "curve", "edge", "datum", "sketch")]
ExtractRef = Annotated[str, RefTo("body", "face")]
REL = 1e-6  # relative band for volume, area and length read-backs
CONTEXT_HINT = (
    "Keep the assembly displayed and make the target component's part the work part: "
    "nx_activate_part(part=<target>, work=true, display=false)"
)


class DatumPlacement(TypedDict, total=False):
    origin: Point3
    normal: Vec3
    direction: Vec3
    axes: list[Vec3]


class LinkData(TypedDict, total=False):
    features: list[result.Row]
    kind: str
    associative: bool
    at_timestamp: bool
    bodies: list[result.Row]
    objects: list[result.Row]
    sources: list[result.Row]
    volume: float
    area: float
    length: float
    datums: list[DatumPlacement]


class ExtractData(TypedDict, total=False):
    features: list[result.Row]
    kind: str
    associative: bool
    at_timestamp: bool
    bodies: list[result.Row]
    volume: float
    area: float
    face_count: int


# ---------------------------------------------------------------------------
# Shared read-backs


def _same_value(got, want, check, what):
    band = REL * max(1.0, abs(want))
    readback.expect(
        abs(got - want) <= band, check, f"The {what} differs from the source",
        actual=got, expected=want,
    )  # fmt: skip
    return got


def _faces(bodies):
    return [f for b in bodies for f in b.GetFaces()]


def _is_feature(obj):
    return obj is not None and hasattr(obj, "GetFeatureName")


def _outputs(b, feature):
    """``(features, objects)`` the builder committed: every committed feature (one per
    datum on a multi-datum link) and their bodies or entities, in order."""
    with native():
        committed = [o for o in (b.GetCommittedObjects() or []) if o is not None]
    features = [o for o in committed if _is_feature(o)]
    if _is_feature(feature) and all(int(f.Tag) != int(feature.Tag) for f in features):
        features.insert(0, feature)
    found = []
    for f in features:
        for get in ("GetBodies", "GetEntities"):
            fn = getattr(f, get, None)
            if fn is not None:
                with native():
                    found.extend(fn())
    found.extend(o for o in committed if not _is_feature(o))
    seen, out = set(), []
    for obj in found:
        if obj is None or int(obj.Tag) in seen:
            continue
        seen.add(int(obj.Tag))
        out.append(obj)
    return features, out


def _link_rows(tx, kind, objs):
    """Reference rows of sources or linked objects of a ``kind`` link."""
    return [tx.ref(o, kind=_ref_kind(kind, o)) for o in objs]


# ---------------------------------------------------------------------------
# WAVE links


def _target(tx):
    """``(work part, work component)``: a component part of the displayed assembly."""
    part = tx.part
    parts = tx.session.Parts
    work_component = getattr(parts, "WorkComponent", None)
    if display.same(parts.Display, part) or work_component is None:
        fail(
            "NX_WRONG_PART_CONTEXT",
            "A WAVE link goes into a component part of the displayed assembly",
            hint=CONTEXT_HINT,
        )
    return part, work_component


def _source(tx, ref, kind, component, field):
    """The occurrence of ``ref`` in the displayed assembly and its component."""
    obj = select.resolve(tx, ref, *SOURCE_KINDS[kind], field=field)
    if getattr(obj, "IsOccurrence", False):
        owner = getattr(obj, "OwningComponent", None)
        if component is not None and not display.same(owner, component):
            fail("NX_WRONG_OWNER", f"{ref} is not shown by the component", field=field)
        return obj, owner
    if component is None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{ref} is a part object; pass the component that shows it",
            field="/component",
        )
    if not display.same(getattr(obj, "OwningPart", None), component.Prototype):
        fail("NX_WRONG_OWNER", f"{ref} is not in the component's part", field=field)
    with native():
        occurrence = component.FindOccurrence(obj)
    if occurrence is None:
        fail(
            "NX_NOT_FOUND",
            f"The component does not show {ref}",
            field=field,
            hint="Show it in the component's reference set (Entire Part)",
        )
    return occurrence, component


def _absolute(pose, point=None, vector=None):
    t, r = pose
    if point is not None:
        return geom.add(geom.matvec(r, point), t)
    return geom.matvec(r, vector)


def _datum_kind(obj):
    if hasattr(obj, "Normal"):
        return "plane"
    if hasattr(obj, "Direction"):
        return "axis"
    return "csys" if hasattr(obj, "Orientation") else None


DATUM_REF_KINDS = {"plane": "datum_plane", "axis": "datum_axis", "csys": "datum_csys"}


def _ref_kind(kind, obj):
    """The reference kind of a source or linked object of a ``kind`` link."""
    if kind == "curve":
        return "edge" if hasattr(obj, "SolidEdgeType") else "curve"
    if kind == "datum":
        return DATUM_REF_KINDS[_datum_kind(obj)]
    return kind


def _datum_frame(obj):
    """``(origin, [directions])`` of a datum plane (normal), axis (direction) or CSYS."""
    if hasattr(obj, "Normal"):
        return geom.xyz(obj.Origin), [geom.unit(geom.xyz(obj.Normal))]
    if hasattr(obj, "Direction"):
        return geom.xyz(obj.Origin), [geom.unit(geom.xyz(obj.Direction))]
    m = obj.Orientation.Element
    axes = [[m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz], [m.Zx, m.Zy, m.Zz]]
    return geom.xyz(obj.Origin), [[float(v) for v in a] for a in axes]


def _check_datums(sources, linked, pose):
    """Every linked datum lies on its source; their placements in assembly coordinates."""
    readback.expect(
        len(linked) == len(sources), "datums", "NX linked another number of datums",
        actual=len(linked),
    )  # fmt: skip
    rows = []
    for i, (source, link) in enumerate(zip(sources, linked, strict=True)):
        origin, dirs = _datum_frame(source)
        got_origin, got_dirs = _datum_frame(link)
        got_origin = _absolute(pose, point=got_origin)
        got_dirs = [_absolute(pose, vector=d) for d in got_dirs]
        if hasattr(source, "Normal"):
            readback.expect_on_plane(got_origin, origin, dirs[0], f"datums/{i}/origin")
            readback.expect_parallel(got_dirs[0], dirs[0], f"datums/{i}/normal")
            rows.append({"origin": got_origin, "normal": got_dirs[0]})
        elif hasattr(source, "Direction"):
            offset = geom.sub(got_origin, origin)
            along = geom.scale(dirs[0], geom.dot(offset, dirs[0]))
            readback.expect_close(got_origin, geom.add(origin, along), f"datums/{i}/origin")
            readback.expect_parallel(got_dirs[0], dirs[0], f"datums/{i}/direction")
            rows.append({"origin": got_origin, "direction": got_dirs[0]})
        else:
            readback.expect_close(got_origin, origin, f"datums/{i}/origin")
            for j, (g, w) in enumerate(zip(got_dirs, dirs, strict=True)):
                readback.expect_close(g, w, f"datums/{i}/axes/{j}", tol=1e-6)
            rows.append({"origin": got_origin, "axes": got_dirs})
    return rows


def _configure_link(tx, b, kind, occurrences, associative, at_timestamp):
    nx = tx.nx
    features = nx.Features
    b.Type = getattr(features.WaveLinkBuilder.Types, LINK_TYPES[kind])
    b.option("Associative", associative)
    b.option("FixAtCurrentTimestamp", at_timestamp)
    if kind in ("body", "face"):
        e = b.ExtractFaceBuilder
        e.ParentPart = features.ExtractFaceBuilder.ParentPartType.OtherPart
        e.Associative = associative
        e.FixAtCurrentTimestamp = at_timestamp
        if kind == "body":
            builders.fill(tx, e.ExtractBodyCollector, occurrences, "Body")
        else:
            e.FaceOption = features.ExtractFaceBuilder.FaceOptionType.FaceChain
            builders.fill(tx, e.FaceChain, occurrences, "Face")
    elif kind == "curve":
        c = b.CompositeCurveBuilder
        c.Associative = associative
        if hasattr(c, "FixAtCurrentTimestamp"):
            c.FixAtCurrentTimestamp = at_timestamp
        builders.curve_section(tx, occurrences, into=c.Section)
    elif kind == "datum":
        d = b.WaveDatumBuilder
        d.ParentPart = features.WaveDatumBuilder.ParentPartType.OtherPart
        d.Associative = associative
        d.Datums.Add(occurrences)
    else:
        s = b.WaveSketchBuilder
        s.Associative = associative
        s.Sketches.Add(occurrences)


def _sketch_curves(sketches):
    """Curves of the source sketches (their prototypes: lengths do not depend on the pose)."""
    out = []
    for s in sketches:
        with native():
            prototype = getattr(s, "Prototype", None) or s
            out.extend(c for c in prototype.GetAllGeometry() if hasattr(c, "GetLength"))
    return out


@tool(
    effect="model",
    label="Create WAVE link",
    summary=lambda a, say: f"WAVE link {a.get('kind', 'body')} into the work part",
    keywords=(
        "wave link",
        "wave geometry linker",
        "linked body",
        "top-down design",
        "inter-part link",
        "link geometry",
        "associative copy",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="heavy",
    invalidates="deleted",
    example={"kind": "body", "sources": ["b:12"], "component": "o:4"},
)
def nx_create_wave_link(
    tx: Tx,
    *,
    kind: LinkKind,
    sources: Annotated[list[SourceRef], Items(1, 50)],
    component: Annotated[ComponentRef, Doc("Shows part-object sources")] | None = None,
    associative: Annotated[bool, Doc("false: a dumb copy, no link")] = True,
    at_timestamp: Annotated[bool, Doc("Fix the link at the current timestamp")] = False,
) -> LinkData:
    """WAVE link bodies, faces, curves, datums or sketches of another component into the work part.
    The work part is the target component's part in the displayed assembly; sources are
    occurrences, or part objects plus their component. Associative links follow the source."""
    if at_timestamp and not associative:
        fail("NX_INVALID_ARGUMENT", "at_timestamp needs an associative link", field="/at_timestamp")
    with tx.checks():
        part, work_component = _target(tx)
        comp = None
        if component is not None:
            comp = select.resolve(tx, component, "component", field="/component")
        occurrences, owners = [], []
        seen = set()
        for i, ref in enumerate(sources):
            field = f"/sources/{i}"
            occ, owner = _source(tx, ref, kind, comp, field)
            if int(occ.Tag) in seen:
                fail("NX_INVALID_ARGUMENT", f"{ref} is listed twice", field=field)
            seen.add(int(occ.Tag))
            if owner is None or display.same(getattr(owner, "Prototype", None), part):
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"{ref} is in the work part; link from another component "
                    "(nx_create_extract copies inside a part)",
                    field=field,
                )
            occurrences.append(occ)
            owners.append(owner)
    pose = structure.pose(work_component)
    # One datum or sketch per builder: NX 2406 links only the first of several.
    groups = [[o] for o in occurrences] if kind in ("datum", "sketch") else [occurrences]
    features, outputs, per_group = [], [], []
    for group in groups:
        tx.checkpoint()
        with builders.build(tx, part.BaseFeatures.CreateWaveLinkBuilder, None) as b:
            _configure_link(tx, b, kind, group, associative, at_timestamp)
            made, found = _outputs(b, b.commit())
        features.extend(made)
        outputs.extend(found)
        per_group.append(found)
    out: LinkData = {
        "features": result.rows(tx, features, "feature"),
        "kind": kind,
        "associative": associative,
        "at_timestamp": at_timestamp,
        "sources": _link_rows(tx, kind, occurrences),
    }
    readback.expect(bool(outputs), "objects", "NX linked nothing")
    if kind in ("body", "face"):
        bodies = [o for o in outputs if is_body(o)]
        readback.expect(bool(bodies), "bodies", "NX linked no body")
        out["bodies"] = result.rows(tx, bodies, "body")
        if kind == "body":
            out["volume"] = _same_value(
                measure.volume(tx, bodies), measure.volume(tx, occurrences), "volume", "volume"
            )
        else:
            out["area"] = _same_value(
                measure.area(tx, _faces(bodies)), measure.area(tx, occurrences), "area", "area"
            )
    elif kind == "curve":
        curves = [o for o in outputs if hasattr(o, "GetLength")]
        out["objects"] = result.rows(tx, curves, "curve")
        out["length"] = _same_value(
            measure.length(tx, curves), measure.length(tx, occurrences), "length", "length"
        )
    elif kind == "datum":
        linked = []
        for i, (source, found) in enumerate(zip(occurrences, per_group, strict=True)):
            same = [o for o in found if _datum_kind(o) == _datum_kind(source)]
            readback.expect(bool(same), f"datums/{i}", "NX linked no datum of that kind")
            linked.append(same[0])
        out["datums"] = _check_datums(occurrences, linked, pose)
        out["objects"] = _link_rows(tx, kind, linked)
    else:
        curves = [o for o in outputs if hasattr(o, "GetLength")]
        out["objects"] = result.rows(tx, curves, "curve")
        out["length"] = _same_value(
            measure.length(tx, curves), measure.length(tx, _sketch_curves(occurrences)),
            "length", "sketch length",
        )  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# Extract geometry


@tool(
    effect="model",
    label="Extract geometry",
    summary=lambda a, say: f"Extract {a.get('kind', 'body')} geometry",
    keywords=(
        "extract geometry",
        "extract body",
        "extract face",
        "extract region",
        "copy body",
        "associative copy",
        "region of faces",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="light",
    invalidates="deleted",
    example={"kind": "face", "objects": ["f:7"]},
)
def nx_create_extract(
    tx: Tx,
    *,
    kind: ExtractKind,
    objects: Annotated[
        list[ExtractRef],
        Items(1, 50),
        Doc("body: bodies; face: connected faces; region: the seed face"),
    ],
    boundary: Annotated[list[FaceRef], Items(1, 50), Doc("region: faces that bound it")]
    | None = None,
    associative: Annotated[bool, Doc("false: a dumb copy")] = True,
    at_timestamp: Annotated[bool, Doc("Ignore later features of the source")] = False,
) -> ExtractData:
    """Extract bodies, faces or a region of faces of the work part into a new body. A region
    grows from a seed face up to boundary faces. Associative extracts follow the source."""
    if at_timestamp and not associative:
        fail(
            "NX_INVALID_ARGUMENT",
            "at_timestamp needs an associative extract",
            field="/at_timestamp",
        )
    if kind == "region":
        if len(objects) != 1:
            fail("NX_INVALID_ARGUMENT", "region takes one seed face", field="/objects")
        if not boundary:
            fail("NX_INVALID_ARGUMENT", "region needs boundary faces", field="/boundary")
    elif boundary is not None:
        fail("NX_INVALID_ARGUMENT", f"{kind} takes no boundary", field="/boundary")
    with tx.checks():
        pick = select.bodies if kind == "body" else select.faces
        values = pick(tx, objects, "/objects")
        bounds = select.faces(tx, boundary, "/boundary") if boundary else []
        if bounds:
            body = values[0].GetBody()
            for i, f in enumerate(bounds):
                if int(f.Tag) == int(values[0].Tag):
                    fail(
                        "NX_INVALID_ARGUMENT",
                        "The seed face is a boundary face",
                        field=f"/boundary/{i}",
                    )
                if not display.same(f.GetBody(), body):
                    fail(
                        "NX_INVALID_ARGUMENT",
                        "Boundary faces belong to the seed's body",
                        field=f"/boundary/{i}",
                    )
    part = tx.part
    features = tx.nx.Features
    with builders.build(tx, part.Features.CreateExtractFaceBuilder, None) as b:
        b.ParentPart = features.ExtractFaceBuilder.ParentPartType.WorkPart
        b.Type = getattr(features.ExtractFaceBuilder.ExtractType, EXTRACT_TYPES[kind])
        b.Associative = associative
        b.FixAtCurrentTimestamp = at_timestamp
        if kind == "body":
            builders.fill(tx, b.ExtractBodyCollector, values, "Body")
        elif kind == "face":
            b.FaceOption = features.ExtractFaceBuilder.FaceOptionType.FaceChain
            builders.fill(tx, b.FaceChain, values, "Face")
        else:
            b.SeedFace.Value = values[0]
            b.BoundaryFaces.Add(bounds)
        feature = b.commit()
        features, outputs = _outputs(b, feature)
    bodies = [o for o in outputs if is_body(o)]
    readback.expect(bool(bodies), "bodies", "NX extracted no body")
    faces = _faces(bodies)
    out: ExtractData = {
        "features": result.rows(tx, features, "feature"),
        "kind": kind,
        "associative": associative,
        "at_timestamp": at_timestamp,
        "bodies": result.rows(tx, bodies, "body"),
        "face_count": len(faces),
    }
    if kind == "body":
        out["volume"] = _same_value(
            measure.volume(tx, bodies), measure.volume(tx, values), "volume", "volume"
        )
    elif kind == "face":
        out["area"] = _same_value(measure.area(tx, faces), measure.area(tx, values), "area", "area")
    else:
        area = measure.area(tx, faces)
        whole = measure.area(tx, list(values[0].GetBody().GetFaces()))
        seed = measure.area(tx, values)
        readback.expect(
            seed * (1 - REL) <= area <= whole * (1 + REL), "area",
            "The region is not between its seed face and the whole body", actual=area,
        )  # fmt: skip
        out["area"] = area
    return out
