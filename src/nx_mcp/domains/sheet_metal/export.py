"""``nx_export_flat_pattern``: a Flat Pattern feature to a new DXF or Trumpf GEO file.

NX 2406 findings encoded here:

- ``ExportFlatPatternBuilder`` writes the developed outline in part units at the Flat
  Pattern's orientation (its X-axis edge); the outline length across a bend is the
  neutral-axis length, so it checks the bend allowance.
- DXF revision, inner and outer mold lines are DXF-only; GEO ignores them, so they are
  refused for GEO. The default revision is R2018 (``AC1032``).
- Nothing is written where a file exists; the file appears only after its contents
  were read back (entity counts, extents).
- ``extents`` are the file's own ``[dx, dy]``; a tester saw them in the other order than
  the feature's ``flat_extents`` (``[along edge, across]``) for one flat pattern, so the
  docstring promises only the file's axes (``flat_pattern_axes`` checks it live).
- Without ``format`` the path's suffix picks it (``.dxf`` or ``.geo``).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, PathTo, RefTo, forbid_extra
from nx_mcp.domains.sheet_metal import common
from nx_mcp.domains.sheet_metal import dxf as outline
from nx_mcp.kit import args, builders, files, result, select, units

REVISIONS = {
    "r12": "R12",
    "r13": "R13",
    "r14": "R14",
    "r2000": "R2000",
    "r2004": "R2004",
    "r2005": "R2005",
    "r2007": "R2007",
    "r2010": "R20102012",
    "r2013": "R20132016",
    "r2018": "R2018",
}
INCLUDE = {
    "bend_up": "BendUp",
    "bend_down": "BendDown",
    "bend_tangent": "BendTangent",
    "interior_cutout": "InteriorCutout",
    "interior_feature": "InteriorFeature",
    "added_top": "AddedTop",
    "added_bottom": "AddedBottom",
}
DEFAULT_INCLUDE = ["bend_up", "bend_down", "interior_cutout"]
DXF_HEADERS = (b"0\r\nSECTION", b"0\nSECTION", outline.BINARY_DXF)
Include = Literal[
    "bend_up",
    "bend_down",
    "bend_tangent",
    "interior_cutout",
    "interior_feature",
    "added_top",
    "added_bottom",
]


class DxfOptions(TypedDict, total=False):
    revision: Literal["r12", "r13", "r14", "r2000", "r2004", "r2005", "r2007", "r2010", "r2013",
                      "r2018"]  # fmt: skip
    mold: Annotated[Literal["inner", "outer", "both"], Doc("Also write mold lines")]


forbid_extra(DxfOptions)


class ExportData(TypedDict):
    path: str
    format: str
    size: int
    sha256: str
    flat_pattern: result.Row
    revision: str | None
    include: list[str]
    tolerance: float
    entities: dict[str, int]
    extents: list[float] | None
    min: list[float] | None
    max: list[float] | None
    area: float | None
    thickness: float | None


def flat_pattern_feature(tx, ref, field="/flat_pattern"):
    feature = select.resolve(tx, ref, "feature", field=field, work_part=True)
    if str(getattr(feature, "FeatureType", "")) != "FLAT_PATTERN":
        fail("NX_WRONG_KIND", "Select a Flat Pattern feature", field=field,
             hint="Create one with nx_create_sheet_metal_feature(operation=flat_pattern)")  # fmt: skip
    return feature


def _valid_dxf(data: bytes) -> bool:
    head = data[:64].lstrip(b" \r\n\t")
    return any(head.startswith(h.lstrip(b" \r\n")) for h in DXF_HEADERS)


@tool(
    effect="file",
    label="Export flat pattern",
    summary=lambda a, say: f"Export flat pattern to {say('path')}",
    keywords=("dxf", "trumpf geo", "flat pattern export", "blank", "laser cutting", "unfold"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    example={"flat_pattern": "f:12", "path": "out/bracket.dxf"},
)
def nx_export_flat_pattern(
    tx: Tx,
    *,
    flat_pattern: Annotated[str, RefTo("feature", doc="Ref f:N: Flat Pattern")],
    path: Annotated[str, PathTo("new", ".dxf", ".geo")],
    format: Annotated[Literal["dxf", "geo"], Doc("Default: path suffix")]  # noqa: A002
    | None = None,
    dxf: DxfOptions | None = None,
    include: Annotated[
        list[Include], Items(0, 7), Doc("Default: bend_up, bend_down, interior_cutout")
    ]
    | None = None,
    tolerance: Length | None = None,
) -> ExportData:
    """Export a Flat Pattern feature to a new DXF or Trumpf GEO file. Returns checksum,
    entity counts and developed extents [dx, dy] in file axes; GEO adds area and thickness."""
    if format is None:
        format = "geo" if str(path).lower().endswith(".geo") else "dxf"  # noqa: A001
    if not str(path).lower().endswith("." + format):
        fail("NX_INVALID_ARGUMENT", f"Use a .{format} path for format {format}", field="/path")
    if format == "geo" and dxf:
        fail("NX_INVALID_ARGUMENT", "DXF options do not apply to GEO", field="/dxf")
    chosen = DEFAULT_INCLUDE if include is None else args.distinct("/include", include, "options")
    if tolerance is not None and float(tolerance) <= 0:
        fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field="/tolerance")
    options = dxf or {}
    revision = options.get("revision", "r2018") if format == "dxf" else None
    mold = options.get("mold")
    with tx.checks():
        feature = flat_pattern_feature(tx, flat_pattern)
        manager = common.manager(tx)
    tol = float(tolerance) if tolerance is not None else units.part_tolerance(tx, 0.01)
    sm = common.namespace(tx).ExportFlatPatternBuilder
    temp = tx.files.stage(path)
    with builders.build(tx, manager.CreateExportFlatPatternBuilder) as b:
        b.FlatPattern.Value = feature
        b.OutputFile = str(temp)
        b.Type = sm.FileType.Dxf if format == "dxf" else sm.FileType.TrumpfGeo
        b.ExportLocation = sm.ExportLocationOptions.Native
        if revision is not None:
            b.DxfRevision = getattr(sm.DxfRevisionType, REVISIONS[revision])
        b.DeviationalTolerance = tol
        for name, prop in INCLUDE.items():
            setattr(b, prop, name in chosen)
        b.InnerMold = mold in ("inner", "both")
        b.OuterMold = mold in ("outer", "both")
        b.commit()
    data = temp.read_bytes() if temp.is_file() else b""
    if not data:
        fail("NX_EXPORT_FAILED", "NX did not write the flat-pattern file")
    if format == "dxf" and not _valid_dxf(data):
        fail("NX_EXPORT_FAILED", "NX's output is not a DXF file", details={"size": len(data)})
    facts = outline.dxf_facts(data) if format == "dxf" else outline.geo_facts(data)
    if facts["extents"] is None and facts.get("version") != "binary":
        fail("NX_EXPORT_FAILED", "The exported file contains no outline geometry",
             details={"entities": facts["entities"]})  # fmt: skip
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    return {
        "path": meta["path"],
        "format": format,
        "size": meta["size"],
        "sha256": meta["sha256"],
        "flat_pattern": tx.ref(feature, "feature"),
        "revision": revision,
        "include": list(chosen),
        "tolerance": tol,
        "entities": facts["entities"],
        "extents": facts["extents"],
        "min": facts["min"],
        "max": facts["max"],
        "area": facts.get("area"),
        "thickness": facts.get("thickness"),
    }
