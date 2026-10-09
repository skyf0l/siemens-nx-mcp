"""GD&T on model faces (PMI): datum feature symbols and feature control frames.

NX 2406 findings encoded here:

- Without the PMI datum license ``CreatePmiDatumFeatureSymbolBuilder`` raises 948801
  ("License Error ... token feature does not exist") on every call: datum feature
  symbols, and frames that reference datum letters, report NX_LICENSE_UNAVAILABLE. The
  ``datum_feature`` variant therefore has no 2406 evidence and is refused there.
- NX moves a frame onto its annotation plane silently: the read-back position is
  returned with the requested one and a warning.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Point3, Range, RefTo, Text
from nx_mcp.domains.drawing.common import update
from nx_mcp.kit import args, builders, drawing, geom, result, select

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


class GdtData(TypedDict, total=False):
    annotation: result.Row
    position: list[float]
    requested_position: list[float]
    text: str


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
    },
    keywords=("gd&t", "feature control frame", "datum symbol", "pmi", "tolerance frame"),
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
    kind: Literal["datum_feature", "fcf"],
    faces: Annotated[list[RefTo("face")], Items(1, 20)],  # type: ignore[valid-type]
    position: Point3,
    context: Literal["pmi"] = "pmi",
    letter: Annotated[str, Text(3, r"^[A-Z]{1,3}$")] | None = None,
    frame: FrameSpec | None = None,
) -> GdtData:
    """Create PMI GD&T on model faces: a datum feature symbol (letter) or a feature control
    frame. NX may move it onto its annotation plane; the read-back position is returned."""
    del context  # "view" (drawing GD&T) is reserved
    if kind == "datum_feature":
        args.need(letter=letter)
    else:
        args.need(frame=frame)
        check_frame(frame)
    with tx.checks():
        targets = select.faces(tx, faces)
    annotations = tx.part.Annotations
    with drawing.in_modeling(tx):  # PMI on a displayed sheet would become a sheet note
        obj = _create(tx, annotations, kind, letter, frame, targets, position)
    update(tx)
    return {
        "annotation": tx.ref(obj, "annotation"),
        "position": _placed(tx, obj, position),
        "requested_position": [float(v) for v in position],
        "text": _text(obj),
    }


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
