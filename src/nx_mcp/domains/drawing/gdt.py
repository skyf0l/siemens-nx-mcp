"""GD&T: PMI on model faces, or drafting annotations on geometry shown in a drawing view.

PMI kinds (``datum_feature``, ``fcf``) attach to faces at a model position. View kinds
(``view_datum_feature``, ``view_fcf``, ``view_datum_target``) are 2D drafting annotations
on a sheet with one leader to an edge or face as shown in the view; their definition,
leader association and sheet origin are read back from a reopened builder.

NX 2406 findings encoded here:

- Without the PMI datum license ``CreatePmiDatumFeatureSymbolBuilder`` raises 948801
  ("License Error ... token feature does not exist") on every call: datum feature
  symbols, and frames that reference datum letters, report NX_LICENSE_UNAVAILABLE. The
  ``datum_feature`` variant therefore has no 2406 evidence and is refused there.
- NX moves a frame onto its annotation plane silently: the read-back position is
  returned with the requested one and a warning.
- The drafting builders (``CreateDraftingFeatureControlFrameBuilder``,
  ``Datums.CreateDraftingDatumFeatureSymbolBuilder``, ``CreateDraftingDatumTargetBuilder``)
  need no PMI license; a drafting frame reads the letters of drafting datum symbols.
- Datum target sizes are builder expressions in part units; NX ignores ``AreaSize`` text.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Length, Point3, Range, RefTo, SheetPoint, Text
from nx_mcp.domains.drawing.common import (
    add_leader,
    check_leader,
    commit,
    geometry,
    leader_row,
    set_origin,
    update,
    verify_origin,
)
from nx_mcp.kit import args, builders, drawing, geom, readback, result, select

CHARACTERISTICS = {
    "straightness": "Straightness",
    "flatness": "Flatness",
    "circularity": "Circularity",
    "cylindricity": "Cylindricity",
    "profile_of_a_line": "ProfileOfALine",
    "profile_of_a_surface": "ProfileOfASurface",
    "angularity": "Angularity",
    "perpendicularity": "Perpendicularity",
    "parallelism": "Parallelism",
    "position": "Position",
    "concentricity": "Concentricity",
    "symmetry": "Symmetry",
    "circular_runout": "CircularRunout",
    "total_runout": "TotalRunout",
    "axis_intersection": "AxisIntersection",
}
FORM = frozenset({"straightness", "flatness", "circularity", "cylindricity"})
MATERIAL = {
    "none": "NotSet",
    "mmc": "MaximumMaterialCondition",
    "lmc": "LeastMaterialCondition",
    "rfs": "RegardlessOfFeatureSize",
}
ZONES = {
    "none": "NotSet",
    "diameter": "Diameter",
    "spherical_diameter": "SphericalDiameter",
    "square": "Square",
}
DATUM_FIELDS = ("PrimaryDatumReference", "SecondaryDatumReference", "TertiaryDatumReference")
Modifier = Literal["mmc", "lmc", "rfs"]


class _Frame(TypedDict):
    characteristic: Literal[tuple(CHARACTERISTICS)]  # type: ignore[valid-type]
    tolerance: Annotated[float, Range(gt=0, le=10000)]


class FrameSpec(_Frame, total=False):
    datums: Annotated[list[RefTo("annotation")], Items(1, 3)]  # type: ignore[valid-type]
    material: Modifier
    zone: Literal["diameter", "spherical_diameter", "square"]
    datum_material: Annotated[list[Literal["none", "mmc", "lmc", "rfs"]], Items(1, 3)]
    projected_height: Annotated[float, Range(gt=0, le=10000)]
    tangent_plane: bool
    free_state: bool


FrameSpec.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


TARGETS = {
    "point": "Point",
    "line": "Line",
    "circular": "Circular",
    "rectangular": "Rectangular",
    "annular": "Annular",
}
SIZES = {  # target type -> {TargetSpec size field: builder expression}
    "point": {},
    "line": {},
    "circular": {"diameter": "OuterDiameter"},
    "rectangular": {"width": "Width", "height": "Height"},
    "annular": {"diameter": "OuterDiameter", "inner_diameter": "InnerDiameter"},
}
PMI_KINDS = ("datum_feature", "fcf")
Kind = Literal["datum_feature", "fcf", "view_datum_feature", "view_fcf", "view_datum_target"]


class _Target(TypedDict):
    type: Literal[tuple(TARGETS)]  # type: ignore[valid-type]


class TargetSpec(_Target, total=False):
    index: Annotated[int, Range(ge=1, le=99)]
    diameter: Length
    inner_diameter: Length
    width: Length
    height: Length


TargetSpec.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


class GdtData(TypedDict, total=False):
    annotation: result.Row
    position: list[float]
    requested_position: list[float]
    text: str
    sheet: result.Row
    origin: list[float]
    leader: dict
    frame: dict
    letter: str
    target: dict


def _is(tx, obj, name):
    cls = getattr(tx.nx.Annotations, name, None)
    return isinstance(obj, cls) if isinstance(cls, type) else type(obj).__name__ == name


def check_frame(frame, field="/frame"):
    """Cross-field rules of a feature control frame (before NX)."""
    form = frame["characteristic"] in FORM
    datums = frame.get("datums") or []
    if form and datums:
        fail("NX_INVALID_ARGUMENT", "Form tolerances take no datums", field=f"{field}/datums")
    if form and (frame.get("material") or frame.get("projected_height") is not None):
        fail("NX_INVALID_ARGUMENT", "Form tolerances take no material modifier or projection",
             field=f"{field}/material")  # fmt: skip
    if datums:
        args.distinct(f"{field}/datums", datums, "datums")
    material = frame.get("datum_material")
    if material is not None and len(material) != len(datums):
        fail("NX_INVALID_ARGUMENT", "Give one datum_material per datum",
             field=f"{field}/datum_material")  # fmt: skip


def _letters(tx, refs):
    """Datum letters of PMI datum feature symbols (needs the PMI datum license)."""
    letters = []
    datums = tx.part.Annotations.Datums
    for i, ref in enumerate(refs):
        obj = select.resolve(tx, ref, "annotation", field=f"/frame/datums/{i}", work_part=True)
        if not _is(tx, obj, "Datum"):
            fail("NX_WRONG_KIND", "datums must be datum feature symbols",
                 field=f"/frame/datums/{i}")  # fmt: skip
        with builders.build(tx, datums.CreatePmiDatumFeatureSymbolBuilder, obj) as reader:
            letters.append(str(reader.Letter))
    return letters


def _configure_frame(tx, b, frame, letters):
    A = tx.nx.Annotations
    fcf = A.FeatureControlFrameBuilder
    data = A.FeatureControlFrameDataBuilder
    b.Characteristic = getattr(fcf.FcfCharacteristic, CHARACTERISTICS[frame["characteristic"]])
    b.FrameStyle = fcf.FcfFrameStyle.SingleFrame
    row = b.FeatureControlFrameDataList.FindItem(0)
    row.ToleranceValue = repr(float(frame["tolerance"]))
    row.MaterialModifier = getattr(
        data.ToleranceMaterialModifier, MATERIAL[frame.get("material", "none")]
    )
    row.ZoneShape = getattr(data.ToleranceZoneShape, ZONES[frame.get("zone", "none")])
    projected = frame.get("projected_height")
    row.Projected = projected is not None
    row.ProjectedValue = repr(float(projected or 0))
    row.TangentPlane = bool(frame.get("tangent_plane", False))
    row.FreeState = bool(frame.get("free_state", False))
    material = frame.get("datum_material") or ["none"] * len(letters)
    condition = A.DatumReferenceBuilder.DatumReferenceMaterialCondition
    for i, name in enumerate(DATUM_FIELDS):
        datum = getattr(row, name)
        datum.Letter = letters[i] if i < len(letters) else ""
        datum.MaterialCondition = getattr(
            condition, MATERIAL[material[i] if i < len(material) else "none"]
        )


def _commit(tx, b, faces, position):
    b.AssociatedObjects.Nxobjects.Clear()
    b.AssociatedObjects.Nxobjects.Add(faces)
    b.Origin.SetInferRelativeToGeometry(True)
    b.Origin.Origin.SetValue(None, None, tx.nx.Point3d(*map(float, position)))
    obj = b.commit(validate=True)
    if obj is None:
        fail("NX_BUILD_FAILED", "NX returned no PMI object")
    return obj


def _text(obj):
    try:
        return " ".join(str(s) for s in obj.GetText())
    except Exception:  # PMI objects without a text block
        return ""


def _placed(tx, obj, position):
    actual = geom.xyz(obj.AnnotationOrigin)
    if geom.dist(actual, list(position)) > 1e-6 * max(1.0, *map(abs, position)):
        tx.warn(
            "NX_UPDATE_WARNING",
            "NX placed the annotation on its annotation plane; position is the read-back origin",
        )
    return actual


@tool(
    effect="model",
    label="Create GD&T",
    summary={
        "datum_feature": "Datum {letter} on {faces}",
        "fcf": "Feature control frame on {faces}",
        "view_datum_feature": "Datum {letter} on {object} in {view}",
        "view_fcf": "Feature control frame on {object} in {view}",
        "view_datum_target": "Datum target {letter} on {object} in {view}",
    },
    keywords=(
        "gd&t",
        "feature control frame",
        "datum symbol",
        "pmi",
        "tolerance frame",
        "datum target",
        "drafting gd&t",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={
        "kind": "fcf",
        "faces": ["fc:3"],
        "position": [10, 20, 30],
        "frame": {"characteristic": "flatness", "tolerance": 0.05},
    },
)
def nx_create_gdt(
    tx: Tx,
    *,
    kind: Kind,
    faces: Annotated[list[RefTo("face")], Items(1, 20)] | None = None,  # type: ignore[valid-type]
    position: Point3 | None = None,
    view: RefTo("drawing_view") | None = None,  # type: ignore[valid-type]
    object: RefTo("edge", "face") | None = None,  # type: ignore[valid-type]  # noqa: A002
    origin: SheetPoint | None = None,
    letter: Annotated[str, Text(3, r"^[A-Z]{1,3}$")] | None = None,
    frame: FrameSpec | None = None,
    target: TargetSpec | None = None,
) -> GdtData:
    """Create GD&T as PMI on faces, or as a drafting annotation with a leader to view geometry.
    PMI kinds take faces and position (NX may move it onto its annotation plane); view_* kinds
    take view, object (edge or face) and origin on the sheet; letter labels a datum or target."""
    _check_shape(kind, faces, position, view, object, origin, letter, frame, target)
    if kind not in PMI_KINDS:
        return _in_view(tx, kind, view, object, origin, letter, frame, target)
    with tx.checks():
        targets = select.faces(tx, faces)
    annotations = tx.part.Annotations
    with drawing.in_modeling(tx):  # PMI on a displayed sheet would become a sheet note
        obj = _create(tx, annotations, kind, letter, frame, targets, position)
    update(tx)
    return {
        "annotation": tx.ref(obj, "annotation"),
        "position": _placed(tx, obj, position),
        "requested_position": [float(v) for v in position or ()],
        "text": _text(obj),
    }


def _check_shape(kind, faces, position, view, obj, origin, letter, frame, target):
    """Each kind takes exactly its own fields (checked before NX)."""
    pmi = kind in PMI_KINDS
    if pmi:
        args.need(faces=faces, position=position)
        others = {"view": view, "object": obj, "origin": origin}
    else:
        args.need(view=view, object=obj, origin=origin)
        others = {"faces": faces, "position": position}
    wants = {
        "letter": kind in ("datum_feature", "view_datum_feature", "view_datum_target"),
        "frame": kind in ("fcf", "view_fcf"),
        "target": kind == "view_datum_target",
    }
    given = {"letter": letter, "frame": frame, "target": target}
    for name, wanted in wants.items():
        if wanted:
            args.need(**{name: given[name]})
        else:
            others[name] = given[name]
    for name, value in others.items():
        if value is not None:
            fail("NX_INVALID_ARGUMENT", f"{name} is not used by kind {kind}", field=f"/{name}",
                 hint="PMI kinds take faces and position; view kinds take view, object and "
                 "origin")  # fmt: skip
    if frame is not None:
        check_frame(frame)
    if target is not None:
        check_target(target)


def check_target(target, field="/target"):
    """A datum target has exactly the sizes of its type, all positive; annular inner < outer."""
    sizes = SIZES[target["type"]]
    for name in ("diameter", "inner_diameter", "width", "height"):
        value = target.get(name)
        if name in sizes and value is None:
            fail("NX_INVALID_ARGUMENT", f"A {target['type']} target needs {name}",
                 field=f"{field}/{name}")  # fmt: skip
        if name not in sizes and value is not None:
            fail("NX_INVALID_ARGUMENT", f"A {target['type']} target takes no {name}",
                 field=f"{field}/{name}")  # fmt: skip
        if value is not None and not float(value) > 0:
            fail("NX_INVALID_ARGUMENT", f"{name} must be positive", field=f"{field}/{name}")
    if target["type"] == "annular" and not target["inner_diameter"] < target["diameter"]:
        fail("NX_INVALID_ARGUMENT", "inner_diameter must be less than diameter",
             field=f"{field}/inner_diameter")  # fmt: skip


def _create(tx, annotations, kind, letter, frame, targets, position):
    if kind == "datum_feature":
        with builders.build(tx, annotations.Datums.CreatePmiDatumFeatureSymbolBuilder, None) as b:
            b.Letter = letter
            obj = _commit(tx, b, targets, position)
    else:
        letters = _letters(tx, frame.get("datums") or [])
        factory = annotations.CreatePmiFeatureControlFrameBuilder
        with builders.build(tx, factory, None) as b:
            _configure_frame(tx, b, frame, letters)
            obj = _commit(tx, b, targets, position)
    return obj


# ---- drafting GD&T on view geometry ---------------------------------------------------------


def _drafting_letters(tx, refs):
    """Letters of the referenced datum feature symbols (a PMI one needs the PMI license)."""
    letters = []
    datums = tx.part.Annotations.Datums
    for i, ref in enumerate(refs):
        field = f"/frame/datums/{i}"
        obj = select.resolve(tx, ref, "annotation", field=field, work_part=True)
        if _is(tx, obj, "DraftingDatum"):
            factory = datums.CreateDraftingDatumFeatureSymbolBuilder
        elif _is(tx, obj, "Datum"):
            factory = datums.CreatePmiDatumFeatureSymbolBuilder
        else:
            fail("NX_WRONG_KIND", "datums must be datum feature symbols", field=field)
        with builders.build(tx, factory, obj) as reader:
            letters.append(str(reader.Letter))
    return letters


def _key(value, enum, names):
    """Our key for a native enum value (``names``: key -> member name)."""
    found = result.enum_name(value, enum, list(names.values()))
    return next((k for k, v in names.items() if v == found), found)


def _read_frame(tx, reader):
    A = tx.nx.Annotations
    data = A.FeatureControlFrameDataBuilder
    condition = A.DatumReferenceBuilder.DatumReferenceMaterialCondition
    row = reader.FeatureControlFrameDataList.FindItem(0)
    datums = [getattr(row, name) for name in DATUM_FIELDS]
    letters = [str(d.Letter) for d in datums if str(d.Letter)]
    out = {
        "characteristic": _key(
            reader.Characteristic, A.FeatureControlFrameBuilder.FcfCharacteristic, CHARACTERISTICS
        ),
        "tolerance": float(str(row.ToleranceValue)),
        "material": _key(row.MaterialModifier, data.ToleranceMaterialModifier, MATERIAL),
        "zone": _key(row.ZoneShape, data.ToleranceZoneShape, ZONES),
        "datums": letters,
        "datum_material": [
            _key(d.MaterialCondition, condition, MATERIAL) for d in datums[: len(letters)]
        ],
    }
    if bool(row.Projected):
        out["projected_height"] = float(str(row.ProjectedValue))
    return out


def _wanted_frame(frame, letters):
    out = {
        "characteristic": frame["characteristic"],
        "tolerance": float(frame["tolerance"]),
        "material": frame.get("material", "none"),
        "zone": frame.get("zone", "none"),
        "datums": list(letters),
        "datum_material": list(frame.get("datum_material") or ["none"] * len(letters)),
    }
    if frame.get("projected_height") is not None:
        out["projected_height"] = float(frame["projected_height"])
    return out


def _configure_target(tx, b, letter, target):
    b.Type = getattr(tx.nx.Annotations.DraftingDatumTargetBuilder.Types, TARGETS[target["type"]])
    b.Label = letter
    b.Index = int(target.get("index", 1))
    for name, member in SIZES[target["type"]].items():
        getattr(b, member).RightHandSide = repr(float(target[name]))


def _read_target(tx, reader, kind):
    types = tx.nx.Annotations.DraftingDatumTargetBuilder.Types
    out = {
        "type": _key(reader.Type, types, TARGETS),
        "label": str(reader.Label),
        "index": int(reader.Index),
    }
    for name, member in SIZES[kind].items():
        out[name] = float(getattr(reader, member).Value)
    return out


def _wanted_target(letter, target):
    out = {"type": target["type"], "label": letter, "index": int(target.get("index", 1))}
    out.update({name: float(target[name]) for name in SIZES[target["type"]]})
    return out


def _same(actual, wanted):
    """Read-back equals the request (floats within a relative 1e-9)."""
    if not isinstance(wanted, dict):
        return actual == wanted
    if set(actual) != set(wanted):
        return False
    return all(
        abs(float(actual[k]) - v) <= 1e-9 * max(1.0, abs(v))
        if isinstance(v, float)
        else actual[k] == v
        for k, v in wanted.items()
    )


def _in_view(tx, kind, view_ref, obj_ref, origin, letter, frame, target):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        obj = geometry(tx, obj_ref, ("edge", "face"), "/object")
        letters = _drafting_letters(tx, frame.get("datums") or []) if frame else []
    annotations = tx.part.Annotations
    if kind == "view_fcf":
        factory, what = annotations.CreateDraftingFeatureControlFrameBuilder, "frame"
    elif kind == "view_datum_feature":
        factory, what = annotations.Datums.CreateDraftingDatumFeatureSymbolBuilder, "datum symbol"
    else:
        factory, what = annotations.CreateDraftingDatumTargetBuilder, "datum target"
    with drawing.on_sheet(tx, sheet) as sh:
        with builders.build(tx, factory, None) as b:
            if kind == "view_fcf":
                _configure_frame(tx, b, frame, letters)
            elif kind == "view_datum_feature":
                b.Letter = letter
            else:
                _configure_target(tx, b, letter, target)
            anchor = add_leader(tx, b.Leader, obj, view)
            set_origin(tx, b.Origin, sh, origin)
            made = commit(b, what)
        update(tx)
        out = {
            "annotation": tx.ref(made, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": verify_origin(sh, made, origin),
            "text": _text(made),
            "leader": leader_row(tx, obj, view, anchor),
        }
        with builders.build(tx, factory, made) as reader:
            check_leader(reader, obj, view)
            if kind == "view_fcf":
                actual, wanted = _read_frame(tx, reader), _wanted_frame(frame, letters)
                out["frame"] = actual
            elif kind == "view_datum_feature":
                actual, wanted = str(reader.Letter), letter
                out["letter"] = actual
            else:
                actual = _read_target(tx, reader, target["type"])
                wanted = _wanted_target(letter, target)
                out["target"] = actual
        readback.expect(_same(actual, wanted), "definition", f"NX stored another {what}",
                        actual=actual, wanted=wanted)  # fmt: skip
        return out
