"""Inter-part (top-down) design: WAVE links between components and extracted geometry.

NX 2406 findings encoded here:

- ``BaseFeatures.CreateWaveLinkBuilder(None)`` is one builder for every link kind; its
  ``Type`` picks the sub-builder that is read: ``ExtractFaceBuilder`` (body and face
  links, ``ParentPart = OtherPart``), ``CompositeCurveBuilder`` (curves and edges),
  ``WaveDatumBuilder`` (datum planes, axes and CSYS) and ``WaveSketchBuilder``.
- Sources are occurrences in the displayed assembly; a prototype object plus its
  ``component`` maps to the occurrence with ``component.FindOccurrence`` (without
  ``component``, the one component of the displayed assembly that shows it). A body
  link also takes a component: every solid body it shows. The target is the work
  part, a component part of that assembly (``Parts.WorkComponent``); a ``target``
  component is made the work component for the call (``SetWorkComponent``) and the
  previous work part restored after it, inside the call's undo mark.
- Face links, like face extracts, need one edge-connected set of faces.
- Occurrences have no journal identifier (their ``JournalIdentifier`` is an NX handle,
  ``HANDLE R-722``): source rows are named ``<component>/<prototype name>``.
- ``Features.CreateExtractFaceBuilder(None)`` extracts bodies, faces or a region of
  faces (seed face inside boundary faces) inside the work part. Faces that do not form
  one edge-connected set fail at commit ("Selected faces will create more than one
  output body"): they are refused before the build.
- Results are read back in assembly coordinates: a linked body keeps the source
  volume, a linked face the source area, linked curves the source length, a linked
  datum lies on the source datum (the work component's pose maps it back).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.target import file_name
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ComponentRef, Doc, FaceRef, Items, Point3, RefTo, Vec3
from nx_mcp.domains.assembly import common, structure
from nx_mcp.kit import builders, display, geom, measure, readback, result, select
from nx_mcp.kit import parts as kparts
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
SourceRef = Annotated[str, RefTo("body", "face", "curve", "edge", "datum", "sketch", "component")]
ExtractRef = Annotated[str, RefTo("body", "face")]
REL = 1e-6  # relative band for volume, area and length read-backs
CONTEXT_HINT = (
    "Keep the assembly displayed and make the target component's part the work part: "
    "nx_activate_part(part=<target part ref>, display=false)"
)
SOURCE_HINT = (
    "nx_find_geometry(owner=<component ref>, kind='face'|'edge') lists a component's faces "
    "and edges; kind 'body' also takes the component ref itself (all its solid bodies)"
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


def _plain_name(obj) -> str:
    name = getattr(obj, "Name", "") if obj is not None else ""
    return str(name or getattr(obj, "JournalIdentifier", "") or "")


def _source_rows(tx, kind, occurrences, owners):
    """Source rows named ``<component>/<prototype name>``: an occurrence's own name is
    an NX handle (``HANDLE R-722``)."""
    rows = _link_rows(tx, kind, occurrences)
    for row, occurrence, owner in zip(rows, occurrences, owners, strict=True):
        inner = _plain_name(getattr(occurrence, "Prototype", None))
        name = "/".join(n for n in (_plain_name(owner), inner) if n)
        if name:
            row["name"] = name
    return rows


# ---------------------------------------------------------------------------
# WAVE links


def _targets(tx, shown) -> list[dict]:
    """``[{component, name, part}]`` of the displayed assembly's component parts (at
    most 10, one row per part): the work parts a WAVE link can go into."""
    rows: list[dict] = []
    seen: set[int] = set()
    for component, _names in kparts.components(shown):
        prototype = getattr(component, "Prototype", None)
        if prototype is None or display.same(prototype, shown) or int(prototype.Tag) in seen:
            continue
        seen.add(int(prototype.Tag))
        rows.append(
            {
                "component": tx.ref(component, "component")["id"],
                "name": str(getattr(component, "Name", "") or ""),
                "part": tx.ref(prototype, "part")["id"],
            }
        )
        if len(rows) == 10:
            break
    return rows


def _target(tx):
    """``(work part, work component)``: a component part of the displayed assembly."""
    part = tx.part
    parts = tx.session.Parts
    work_component = getattr(parts, "WorkComponent", None)
    if display.same(parts.Display, part) or work_component is None:
        targets = _targets(tx, parts.Display)
        if targets:
            first = targets[0]
            hint = (
                "Name the receiving component with target (details.targets lists them), e.g. "
                f"target='{first['component']}' for {first['name'] or first['component']}; "
                "or make its part the work part with nx_activate_part(display=false)"
            )
        else:
            hint = (
                "Display the assembly (nx_activate_part(part=<assembly ref>)), then name the "
                "receiving component with target='o:N' (nx_list_components)"
            )
        fail(
            "NX_WRONG_PART_CONTEXT",
            "Name the component whose part receives the WAVE link with target (the work part "
            "is not a component part of the displayed assembly)",
            field="/target",
            hint=hint,
            details={"targets": targets} if targets else None,
        )
    return part, work_component


def _is_component(obj):
    return hasattr(obj, "FindOccurrence") and hasattr(obj, "Prototype")


def _in_work_part(ref, field, part):
    """Refuse a source of ``part``, the part that receives the link."""
    fail(
        "NX_INVALID_ARGUMENT",
        f"{ref} belongs to {file_name(part)}, the part that receives the link; a part "
        "cannot WAVE-link its own geometry",
        field=field,
        hint="Link into a component of another part: name it with target, or make its part "
        "the work part (nx_activate_part, display=false); nx_create_extract copies geometry "
        "inside one part",
    )


def _resolve_source(tx, ref, kind, field):
    kinds = SOURCE_KINDS[kind] + (("component",) if kind == "body" else ())
    try:
        return select.resolve(tx, ref, *kinds, field=field)
    except NXError as error:
        if error.code == "NX_WRONG_KIND" and not error.hint:
            error.hint = SOURCE_HINT
        raise


def _component_bodies(ref, component, field):
    """``[(occurrence, component)]`` of every solid body ``component`` shows."""
    prototype = getattr(component, "Prototype", None)
    out = []
    with native():
        bodies = list(getattr(prototype, "Bodies", None) or [])
    for body in bodies:
        if not getattr(body, "IsSolidBody", True):
            continue
        with native():
            occurrence = component.FindOccurrence(body)
        if occurrence is not None:
            out.append((occurrence, component))
    if not out:
        fail(
            "NX_NOT_FOUND",
            f"Component {ref} shows no solid body",
            field=field,
            hint="Load it fully and show its bodies in its reference set (Entire Part)",
        )
    return out


def _showing(tx, ref, obj, field):
    """``(occurrence, component)`` of the one component of the displayed assembly that
    shows the part object ``obj``."""
    owning = getattr(obj, "OwningPart", None)
    found = []
    for component, _names in kparts.components(tx.session.Parts.Display):
        if not display.same(getattr(component, "Prototype", None), owning):
            continue
        with native():
            occurrence = component.FindOccurrence(obj)
        if occurrence is not None:
            found.append((occurrence, component))
    if not found:
        fail(
            "NX_NOT_FOUND",
            f"No component of the displayed assembly shows {ref}",
            field=field,
            hint="Add its part as a component, or show the object in the component's "
            "reference set (Entire Part)",
        )
    if len(found) > 1:
        refs = [tx.ref(c, "component")["id"] for _o, c in found[:10]]
        fail(
            "NX_INVALID_ARGUMENT",
            f"{ref} is shown by {len(found)} components; pass component= one of " + ", ".join(refs),
            field="/component",
            details={"components": refs},
        )
    return found[0]


def _source(tx, ref, kind, component, field, part):
    """``[(occurrence, component)]`` of ``ref`` in the displayed assembly: one pair, or
    every solid body of a component ref (body links). ``part`` receives the link."""
    obj = _resolve_source(tx, ref, kind, field)
    if _is_component(obj):
        if component is not None and not display.same(obj, component):
            fail("NX_WRONG_OWNER", f"{ref} is not the component given", field=field)
        return _component_bodies(ref, obj, field)
    if getattr(obj, "IsOccurrence", False):
        owner = getattr(obj, "OwningComponent", None)
        if component is not None and not display.same(owner, component):
            fail("NX_WRONG_OWNER", f"{ref} is not shown by the component", field=field)
        return [(obj, owner)]
    if display.same(getattr(obj, "OwningPart", None), part):
        _in_work_part(ref, field, part)
    if component is None:
        return [_showing(tx, ref, obj, field)]
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
    return [(occurrence, component)]


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


def _link_part(services, args):
    """Gate 3t: the assembly a WAVE link works in (its sources are other parts' objects):
    the one holding ``target``, ``component`` or an occurrence source, else the displayed
    assembly that shows the part-object sources (None: the usual rules)."""
    for ref in [args.get("target"), args.get("component"), *(args.get("sources") or [])]:
        if not isinstance(ref, str) or ref.startswith("$"):
            continue
        try:
            obj = services.references.resolve(ref, ())
        except Exception:
            continue
        if getattr(obj, "IsOccurrence", False):  # a component or an occurrence
            return services.owning_part(obj)
    return getattr(getattr(services.session, "Parts", None), "Display", None)


@tool(
    target=_link_part,
    switch="display",
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
    sources: Annotated[
        list[SourceRef],
        Items(1, 50),
        Doc("nx_find_geometry owner=<component> lists its faces and edges"),
    ],
    component: Annotated[ComponentRef, Doc("Shows part-object sources (found if only one does)")]
    | None = None,
    target: Annotated[ComponentRef, Doc("Component whose part gets the link")] | None = None,
    associative: Annotated[bool, Doc("false: a dumb copy, no link")] = True,
    at_timestamp: Annotated[bool, Doc("Fix the link at the current timestamp")] = False,
) -> LinkData:
    """WAVE link bodies, faces, curves, datums or sketches of one component into another's part.
    From the displayed assembly, target names the receiving component (else the work part, a
    component part, gets it). Faces: one edge-connected set. Sources: occurrences, part
    objects or (body) components."""
    if at_timestamp and not associative:
        fail("NX_INVALID_ARGUMENT", "at_timestamp needs an associative link", field="/at_timestamp")
    with tx.checks():
        if target is None:
            part, work_component = _target(tx)
        else:
            work_component = common.in_assembly(tx, target, "/target")
            part = work_component.Prototype
        comp = None
        if component is not None:
            comp = select.resolve(tx, component, "component", field="/component")
        occurrences, owners, refs = [], [], []
        seen = set()
        for i, ref in enumerate(sources):
            field = f"/sources/{i}"
            for occ, owner in _source(tx, ref, kind, comp, field, part):
                if int(occ.Tag) in seen:
                    fail("NX_INVALID_ARGUMENT", f"{ref} is listed twice", field=field)
                seen.add(int(occ.Tag))
                if owner is None or display.same(getattr(owner, "Prototype", None), part):
                    _in_work_part(ref, field, part)
                occurrences.append(occ)
                owners.append(owner)
                refs.append(ref)
        if kind == "face":
            one_connected_set(occurrences, refs, "/sources")
    if target is None:
        return _link(tx, kind, part, work_component, occurrences, owners, associative,
                     at_timestamp)  # fmt: skip
    tx.touch_part(part)  # the target's part gets the link: the kernel reads it back too
    with common.working_in(tx, work_component):
        return _link(tx, kind, part, work_component, occurrences, owners, associative,
                     at_timestamp)  # fmt: skip


def _link(
    tx, kind, part, work_component, occurrences, owners, associative, at_timestamp
) -> LinkData:
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
        "sources": _source_rows(tx, kind, occurrences, owners),
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


def _face_groups(faces) -> list[int]:
    """The connected-set index of each face: faces sharing an edge share a set."""
    parent = list(range(len(faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[int, int] = {}
    for i, face in enumerate(faces):
        with native():
            edges = list(face.GetEdges() or [])
        for edge in edges:
            j = first.setdefault(int(edge.Tag), i)
            parent[find(i)] = find(j)
    return [find(i) for i in range(len(faces))]


def one_connected_set(faces, refs, field="/objects"):
    """Refuse faces that would make several extracted or linked bodies (NX fails at commit
    with "Selected faces will create more than one output body")."""
    groups = _face_groups(faces)
    apart = [i for i, g in enumerate(groups) if g != groups[0]]
    if apart:
        i = apart[0]
        fail(
            "NX_INVALID_ARGUMENT",
            f"{refs[i]} is not connected to {refs[0]} by shared edges: the faces must form "
            "one connected set (one output body)",
            field=f"{field}/{i}",
            hint="Pass each connected set in its own call"
            + (", or use kind 'body' or 'region'" if field == "/objects" else ""),
            details={"sets": len(set(groups))},
        )


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
    boundary: Annotated[list[FaceRef], Items(1, 50), Doc("region only: faces that stop it")]
    | None = None,
    associative: Annotated[bool, Doc("false: a dumb copy")] = True,
    at_timestamp: Annotated[bool, Doc("Ignore later features of the source")] = False,
) -> ExtractData:
    """Extract bodies, faces or a region of faces of the work part into a new body of the work
    part. face: one edge-connected set; region: grows from a seed face up to boundary faces.
    Associative extracts follow the source."""
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
            fail(
                "NX_INVALID_ARGUMENT",
                "region needs boundary faces",
                field="/boundary",
                hint="Pass boundary=[face refs] that stop the region growing from the seed "
                "face (they are not copied); kind 'face' copies just the listed faces",
            )
    elif boundary is not None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{kind} takes no boundary",
            field="/boundary",
            hint="boundary is for kind 'region' only",
        )
    with tx.checks():
        for i, ref in enumerate(objects):
            obj = select.resolve(tx, ref, "body", "face", field=f"/objects/{i}")
            if getattr(obj, "IsOccurrence", False):
                fail("NX_WRONG_OWNER", f"{ref} is geometry of a component, not of the work part",
                     field=f"/objects/{i}", hint="nx_create_wave_link copies a component's "
                     "geometry into a part; nx_create_extract copies the work part's own")  # fmt: skip
        pick = select.bodies if kind == "body" else select.faces
        values = pick(tx, objects, "/objects")
        if kind == "face":
            one_connected_set(values, objects)
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
