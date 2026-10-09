"""Import neutral and CAD files (tool-surface 3.2, 4): ``nx_import_model``.

Every format reads a staged copy (``imports/<operation_id>/input<suffix>``, where the
translator also writes its log and, for STEP, the prototypes of an unflattened
assembly) into the work part; ``new_part`` first creates and activates an agent-owned
part. The source file is checked in Python before NX sees it (a bad DXF otherwise
opens a modal NX dialog), and the result is read back against the file's own facts.

NX 2406 findings:

- STEP (``Step214Importer``) and IGES (``IgesImporter``) need ``OutputFile`` even for
  a work-part import. IGES brings a solid as trimmed surfaces: ``sew`` (default) makes
  them one solid again, without it each face is a sheet body.
- Parasolid (``ImportManager.CreateParasolidImporter``) reads text .x_t and binary .x_b
  into the work part with exact geometry.
- STL (``ImportManager.CreateStlImporter``) creates NX facet bodies
  (``NXOpen.Facet.FacetedBody``, not convergent bodies): in ``part.FacetedBodies`` with
  no ``AssociatedBody`` (the display facets of every solid are listed there too), not in
  ``part.Bodies``; the undo mark removes them. ``FileFormat`` must match the file (ASCII
  is the default; a binary file read as ASCII is "Parsing error in file").
- DXF/DWG (``DxfdwgImporter``, 3D workflow, model data to Modeling) creates curves at
  the file's X/Y. A file the reader rejects opens the modal dialog "Failed to read
  dxf/dwg file"; a DXF R13 or later without entity handles and an OBJECTS section is
  rejected that way, an R12 DXF needs neither. ``target`` sketch moves the curves into
  an existing sketch (file X/Y = sketch X/Y).
- CATIA V5 (``Catiav5Importer``) needs its own translator license: without it Commit
  raises 3755014 "There are no licenses available for the NX_CATIAV5 translator".
- No JT, SolidWorks or Creo-by-design importer is offered: NX 2406 has no NXOpen JT or
  SolidWorks import builder, and Creo/Solid Edge could not be proven without sample
  files.
"""

from __future__ import annotations

import contextlib
import math
import re
import shutil
import struct
import time
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import classify, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ByVariant, Doc, Json, PathTo, RefTo
from nx_mcp.domains.parts_files import _saving
from nx_mcp.domains.parts_files.exchange import Bounds, _settings, _tail
from nx_mcp.domains.parts_files.parts import new_part
from nx_mcp.kit import files, measure, neutral, parts, readback, result
from nx_mcp.kit import sketch as ksketch
from nx_mcp.kit import units as kit_units

SUFFIXES = {
    "step": (".step", ".stp"),
    "iges": (".igs", ".iges"),
    "parasolid": (".x_t", ".x_b"),
    "stl": (".stl",),
    "dxf_dwg": (".dxf", ".dwg"),
    "catia_v5": (".catpart", ".catproduct"),
}
ALL_SUFFIXES = tuple(dict.fromkeys(s for group in SUFFIXES.values() for s in group))
TRANSLATORS = {
    "step": "Step214Importer",
    "iges": "IgesImporter",
    "parasolid": "ParasolidImporter",
    "stl": "STLImporter",
    "dxf_dwg": "DxfdwgImporter",
    "catia_v5": "Catiav5Importer",
}
CATIA_LICENSE_CODE = 3755014  # "There are no licenses available for the NX_CATIAV5 translator"
FILE_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8, "um": 0.001}
# DXF $INSUNITS
DXF_UNITS = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m", 13: "um"}
DXF_CURVES = ("LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE")
MAX_ROWS = 200


class NoOptions(TypedDict, total=False):
    """This format has no options."""


class IgesOptions(TypedDict, total=False):
    sew: Annotated[bool, Doc("Sew the surfaces into solids where they close (default true)")]


class StlOptions(TypedDict, total=False):
    units: Annotated[
        Literal["mm", "in", "m"], Doc("Length unit of the file's numbers (default part units)")
    ]


class DxfOptions(TypedDict, total=False):
    units: Annotated[
        Literal["mm", "in"], Doc("Length unit of one drawing unit (default part units)")
    ]


OPTION_FORMS = {
    "step": NoOptions,
    "iges": IgesOptions,
    "parasolid": NoOptions,
    "stl": StlOptions,
    "dxf_dwg": DxfOptions,
    "catia_v5": NoOptions,
}
Options = Annotated[
    dict,
    Json(max_keys=2),
    ByVariant("format", OPTION_FORMS),
    Doc("Per-format options (iges, stl, dxf_dwg): nx_get_tool_schemas(tool#format)"),
]


class ImportData(TypedDict):
    part: result.Row
    target: str
    format: str
    bodies: list[result.Row]
    body_count: int
    solids: int
    sheets: int
    facet_bodies: int
    curves: list[result.Row]
    curve_count: int
    sketch: result.Row | None
    volume: float | None
    bounds: Bounds | None
    file_units: str | None
    components_created: int
    flatten: bool
    staging: str


# --- source checks (before NX sees the file) ------------------------------------------


def _bad(message: str, hint: str | None = None):
    fail("NX_INVALID_ARGUMENT", message, field="/path",
         hint=hint or "Upload the complete file; no translator was started")  # fmt: skip


def _check_step(data: bytes) -> dict:
    text = data.decode("latin-1")
    framing = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL).strip("﻿ \t\r\n")
    if not re.match(r"ISO-10303-21\s*;", framing, re.IGNORECASE) or not re.search(
        r"END-ISO-10303-21\s*;\s*$", framing, re.IGNORECASE
    ):
        _bad("The STEP file lacks its ISO-10303-21 opening or closing marker (truncated?)")
    try:
        units = neutral.step_facts(data).get("units")
    except ValueError:
        units = None
    return {"text": text, "units": units}


def _check_neutral(reader, name):
    def check(data: bytes) -> dict:
        try:
            return reader(data)
        except ValueError as error:
            _bad(f"The file is not a readable {name} file: {error}")

    return check


def _check_parasolid(data: bytes, suffix: str) -> dict:
    facts = _check_neutral(neutral.parasolid_facts, "Parasolid")(data)
    wanted = "binary" if suffix == ".x_b" else "text"
    if facts["format"] != wanted:
        _bad(f"The file is a {facts['format']} Parasolid file; {suffix} needs {wanted}",
             hint="Name a text transmit file .x_t and a binary one .x_b")  # fmt: skip
    return facts


def _check_catia(data: bytes) -> dict:
    if not data.startswith(b"V5_CFV2"):
        _bad("The file is not a CATIA V5 document (no V5_CFV2 header)")
    return {}


def _dxf_pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    if len(lines) % 2:
        lines = lines[:-1] if not lines[-1].strip() else lines
    if len(lines) % 2:
        raise ValueError("odd number of lines (truncated group)")
    try:
        return [(int(lines[i].strip()), lines[i + 1].strip()) for i in range(0, len(lines), 2)]
    except ValueError:
        raise ValueError("a group code is not an integer") from None


def _arc_box(cx, cy, r, a0=0.0, a1=360.0) -> list[tuple[float, float]]:
    """Points whose box is the arc's box (ends plus the quadrant points it passes)."""
    a0, a1 = a0 % 360.0, a1 % 360.0
    span = (a1 - a0) % 360.0 or 360.0
    angles = [a0, a0 + span] + [q for q in (0, 90, 180, 270, 360, 450, 540, 630)
                                if a0 < q < a0 + span]  # fmt: skip
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
            for a in angles]  # fmt: skip


def dxf_facts(data: bytes) -> dict:
    """Version, units, sections, entity counts and (when exact) the X/Y extent of an ASCII
    DXF file. ``ValueError`` when it is not one, or one NX 2406 cannot read."""
    if data.startswith(b"AutoCAD Binary DXF"):
        raise ValueError("binary DXF is not supported; save it as ASCII DXF")
    pairs = _dxf_pairs(data.decode("latin-1").lstrip("﻿"))
    if not pairs or pairs[-1] != (0, "EOF"):
        raise ValueError("no EOF marker (truncated?)")
    sections, entities, header = set(), {}, {}
    handles, exact, points, z = 0, True, [], []
    section, var, entity, values = None, None, None, {}

    def close():
        nonlocal exact
        if entity is None:
            return
        get = values.get
        if entity == "LINE":
            points.extend([(get(10, 0.0), get(20, 0.0)), (get(11, 0.0), get(21, 0.0))])
            z.extend([get(30, 0.0), get(31, 0.0)])
        elif entity == "CIRCLE":
            points.extend(_arc_box(get(10, 0.0), get(20, 0.0), get(40, 0.0)))
            z.append(get(30, 0.0))
        elif entity == "ARC":
            points.extend(_arc_box(get(10, 0.0), get(20, 0.0), get(40, 0.0), get(50, 0.0),
                                   get(51, 360.0)))  # fmt: skip
            z.append(get(30, 0.0))
        elif entity in DXF_CURVES or entity == "INSERT":  # block contents: not read
            exact = False

    for code, value in pairs:
        if code == 0:
            if section == "ENTITIES":
                close()
            entity, values = None, {}
            if value == "SECTION":
                section = "?"
            elif value == "ENDSEC":
                section = None
            elif section == "ENTITIES":
                entity = value
                entities[value] = entities.get(value, 0) + 1
            continue
        if code == 2 and section == "?":
            section = value
            sections.add(value)
        elif section == "HEADER":
            if code == 9:
                var = value
            elif var in ("$ACADVER", "$INSUNITS"):
                header[var] = value
        elif section == "ENTITIES" and entity is not None:
            if code == 5:
                handles += 1
            elif code in (10, 20, 30, 11, 21, 31, 40, 50, 51):
                values[code] = float(value)
    version = header.get("$ACADVER", "AC1009")
    if "ENTITIES" not in sections or not entities:
        raise ValueError("no ENTITIES section with entities")
    modern = version > "AC1009"
    if modern and ("OBJECTS" not in sections or handles < sum(entities.values())):
        raise ValueError(
            f"a {version} DXF without entity handles or an OBJECTS section (NX reads R13+ "
            "DXF only as AutoCAD writes it; save it from a CAD program or as R12)"
        )
    units = DXF_UNITS.get(int(header.get("$INSUNITS", "0") or 0))
    low = high = None
    if exact and points:
        low = [min(p[0] for p in points), min(p[1] for p in points), min(z or [0.0])]
        high = [max(p[0] for p in points), max(p[1] for p in points), max(z or [0.0])]
    curves = sum(n for kind, n in entities.items() if kind in DXF_CURVES)
    return {"version": version, "units": units, "entities": entities, "curves": curves,
            "min": low, "max": high}  # fmt: skip


def dwg_facts(data: bytes) -> dict:
    """Version of a DWG file whose header's section addresses lie inside the file (NX 2406
    answers a truncated DWG with a modal dialog). ``ValueError`` otherwise."""
    match = re.match(rb"AC1(\d{3})", data[:6])
    if not match or len(data) < 0x100:
        raise ValueError("no AC1xxx DWG header")
    version = data[:6].decode("ascii")
    size = len(data)
    if version in ("AC1012", "AC1014", "AC1015"):  # R13-R2000: section locator records
        count = struct.unpack_from("<i", data, 0x15)[0]
        if not 0 < count < 16:
            raise ValueError("unreadable section locators")
        for i in range(count):
            seeker, length = struct.unpack_from("<ii", data, 0x19 + 9 * i + 1)
            if seeker < 0 or length < 0 or seeker + length > size:
                raise ValueError("a section lies beyond the end of the file (truncated)")
    elif version >= "AC1018" and version != "AC1021":  # R2004 header, XOR-encrypted at 0x80
        seed, plain = 1, bytearray()
        for byte in data[0x80:0xEC]:
            seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
            plain.append(byte ^ ((seed >> 16) & 0xFF))
        if not plain.startswith(b"AcFssFcAJMB"):
            raise ValueError("unreadable R2004+ file header")
        last_end = struct.unpack_from("<q", plain, 0x2C)[0]
        page_map = struct.unpack_from("<q", plain, 0x54)[0] + 0x100
        if last_end > size or page_map >= size:
            raise ValueError("the section map lies beyond the end of the file (truncated)")
    return {"version": version, "units": None, "min": None, "max": None}


def _check_dxf_dwg(data: bytes, suffix: str) -> dict:
    reader, name = (dwg_facts, "DWG") if suffix == ".dwg" else (dxf_facts, "DXF")
    try:
        facts = reader(data)
    except (ValueError, struct.error) as error:
        _bad(
            f"The {name} file cannot be imported: {error}",
            hint="Upload the complete file, saved by a CAD program; no translator was started",
        )
    if suffix == ".dwg":
        return facts
    if not facts["curves"]:
        _bad("The DXF file holds no curves (LINE, ARC, CIRCLE, POLYLINE, SPLINE, ELLIPSE)",
             hint="Only model-space curves are imported")  # fmt: skip
    return facts


# --- translators ----------------------------------------------------------------------


def _dex_common(builder, staged, folder):
    builder.InputFile = str(staged)
    # NX 2406 validates OutputFile even when importing into the work part.
    builder.OutputFile = str(folder / "output.prt")
    builder.FileOpenFlag = False
    builder.ProcessHoldFlag = True


def _step(tx, part, staged, folder, opts, flatten):
    builder = tx.session.DexManager.CreateStep214Importer()
    yield builder
    builder.SettingsFile = str(_settings("STEP214UG", "step214ug.def", code="NX_IMPORT_FAILED"))
    _dex_common(builder, staged, folder)
    builder.ImportTo = tx.nx.Step214Importer.ImportToOption.WorkPart
    builder.FlattenAssembly = flatten
    builder.SimplifyGeometry = False
    builder.ObjectTypes.Solids = True
    builder.ObjectTypes.Surfaces = True
    builder.ObjectTypes.Curves = True


def _iges(tx, part, staged, folder, opts, flatten):
    builder = tx.session.DexManager.CreateIgesImporter()
    yield builder
    builder.SettingsFile = str(_settings("IGES", "igesimport.def", code="NX_IMPORT_FAILED"))
    _dex_common(builder, staged, folder)
    builder.ImportTo = tx.nx.IgesImporter.ImportToEnum.WorkPart
    builder.SewSurfaces = bool(opts.get("sew", True))
    builder.SimplifyGeometry = False
    builder.ObjectTypes.Solids = True
    builder.ObjectTypes.Surfaces = True
    builder.ObjectTypes.Curves = True


def _parasolid(tx, part, staged, folder, opts, flatten):
    builder = part.ImportManager.CreateParasolidImporter()
    yield builder
    builder.FileName = str(staged)


def _stl(tx, part, staged, folder, opts, flatten):
    builder = part.ImportManager.CreateStlImporter()
    yield builder
    kinds = tx.nx.STLImporter
    builder.FileName = str(staged)
    builder.FileFormat = (
        kinds.FileFormatType.Binary if opts["_binary"] else kinds.FileFormatType.Ascii
    )
    builder.FileUnits = {
        "mm": kinds.FileUnitsType.Millimeters,
        "in": kinds.FileUnitsType.Inches,
        "m": kinds.FileUnitsType.Meters,
    }[opts["_units"]]
    builder.DisplayInformation = False  # no information window


def _dxf_dwg(tx, part, staged, folder, opts, flatten):
    builder = tx.session.DexManager.CreateDxfdwgImporter()
    yield builder
    kinds = tx.nx.DxfdwgImporter
    builder.SettingsFile = str(_settings("DXFDWG", "dxfdwg.def", code="NX_IMPORT_FAILED"))
    _dex_common(builder, staged, folder)
    builder.ImportTo = kinds.ImportToEnum.Work
    builder.Workflow = kinds.WorkflowAs.ThreeD
    builder.ConvModelData = True
    builder.SendModelDataTo = kinds.SendModelDataAs.Modeling
    builder.Units = kinds.UnitsEnum.Metric if opts["_units"] == "mm" else kinds.UnitsEnum.English


def _catia_v5(tx, part, staged, folder, opts, flatten):
    builder = tx.session.DexManager.CreateCatiav5Importer()
    yield builder
    builder.SettingsFile = str(_settings("CATIAV5", "catiav5.def", code="NX_IMPORT_FAILED"))
    _dex_common(builder, staged, folder)
    builder.ImportTo = tx.nx.Catiav5Importer.ImportToOption.WorkPart


IMPORTERS = {
    "step": _step,
    "iges": _iges,
    "parasolid": _parasolid,
    "stl": _stl,
    "dxf_dwg": _dxf_dwg,
    "catia_v5": _catia_v5,
}


def _diagnostics(folder: Path, root, format: str) -> dict:
    out = []
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if f.is_file() and not f.name.startswith("input."):
            row = {"path": files.relative(f, root), "size": f.stat().st_size}
            if f.suffix.lower() in (".log", ".err", ".txt"):
                row["text"] = _tail(f)
            out.append(row)
    return {"translator": TRANSLATORS[format], "files": out}


def _product_names(text: str) -> set[str]:
    names = set()
    pattern = r"PRODUCT\s*\(\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'"
    for pair in re.findall(pattern, text, re.IGNORECASE):
        names.update(Path(v.replace("''", "'")).stem.casefold() for v in pair)
    return names


def _check_options(format, flatten, target, sketch, new_part_path, source_suffix):
    if (target == "new_part") != (new_part_path is not None):
        fail("NX_INVALID_ARGUMENT", "Give new_part_path exactly when target is new_part",
             field="/new_part_path")  # fmt: skip
    if (target == "sketch") != (sketch is not None):
        fail("NX_INVALID_ARGUMENT", "Give sketch exactly when target is sketch", field="/sketch")
    if target == "sketch" and format != "dxf_dwg":
        fail("NX_INVALID_ARGUMENT", "Only a DXF/DWG file is imported into a sketch",
             field="/target")  # fmt: skip
    if not flatten and format != "step":
        fail("NX_INVALID_ARGUMENT", "Only a STEP import keeps assembly structure",
             field="/flatten", hint="Leave flatten true for this format")  # fmt: skip
    if source_suffix not in SUFFIXES[format]:
        fail("NX_UNSUPPORTED_FILE_TYPE",
             f"A {format} import reads {' or '.join(SUFFIXES[format])}, not {source_suffix}",
             field="/path")  # fmt: skip


@tool(
    effect="model",
    label="Import model",
    summary=lambda a, say: f"Import {str(a.get('format', '')).upper()} {say('path')}",
    keywords=(
        "import step",
        "import iges",
        "import parasolid",
        "import stl",
        "import dxf dwg",
        "catia v5",
        "read neutral file",
        "translate in",
    ),  # fmt: skip
    variants="format",
    requires=("no_active_sketch",),
    cost="long",
    invalidates="deleted",
    new_part="new_part_path",
    example={"path": "inbox/bracket.step", "format": "step"},
)
def nx_import_model(
    tx: Tx,
    *,
    path: PathTo("read", *ALL_SUFFIXES),  # type: ignore[valid-type]
    format: Literal["step", "iges", "parasolid", "stl", "dxf_dwg", "catia_v5"],
    target: Literal["work_part", "new_part", "sketch"] = "work_part",
    new_part_path: PathTo("new", ".prt") | None = None,  # type: ignore[valid-type]
    sketch: Annotated[RefTo("sketch"), Doc("target sketch: its X/Y take the file's X/Y")]  # type: ignore[valid-type]
    | None = None,
    flatten: Annotated[bool, Doc("STEP: false keeps the assembly structure as components")] = True,
    options: Options = {},  # noqa: B006 - never mutated; None would be Optional on Python 3.10
) -> ImportData:
    """Import a CAD file into the work part, or into a new part (new_part_path) made work part.
    STEP, IGES, Parasolid .x_t/.x_b, STL (facet bodies), DXF/DWG (curves; target sketch puts
    them in a finished sketch), CATIA V5."""
    source = Path(path)
    suffix = source.suffix.lower()
    _check_options(format, flatten, target, sketch, new_part_path, suffix)
    data = source.read_bytes()
    facts = {
        "step": _check_step,
        "iges": _check_neutral(neutral.iges_facts, "IGES"),
        "stl": _check_neutral(neutral.stl_facts, "STL"),
        "catia_v5": _check_catia,
    }.get(format, lambda d: None)(data)
    if format == "parasolid":
        facts = _check_parasolid(data, suffix)
    elif format == "dxf_dwg":
        facts = _check_dxf_dwg(data, suffix)
    facts = facts or {}
    root = Path(tx.workspace.root)
    with tx.checks():
        if not flatten and "NEXT_ASSEMBLY_USAGE_OCCURRENCE" in facts.get("text", "").upper():
            names = _product_names(facts["text"])
            loaded = [files.relative(parts.file_of(p), root) for p in tx.session.Parts
                      if Path(parts.file_of(p)).stem.casefold() in names]  # fmt: skip
            if loaded:
                fail("NX_NAME_CONFLICT", "Parts with the STEP product names are already loaded",
                     field="/path", hint="Close those parts, or import with flatten true",
                     details={"loaded_parts": loaded})  # fmt: skip
        if new_part_path is not None:
            parts.check_new_name(tx, new_part_path, field="/new_part_path")
        elif tx.session.Parts.Work is None:
            fail("NX_NO_WORK_PART", "No work part is open", hint="Use target new_part")
        target_sketch = ksketch.resolve(tx, sketch) if sketch is not None else None
    loaded_before = {int(p.Tag) for p in tx.session.Parts}
    previous = (tx.session.Parts.Work, tx.session.Parts.Display)
    folder = root / "imports" / (tx.operation_id or "import")
    try:
        if new_part_path is not None:
            new_part(tx, Path(new_part_path), "mm")
        part = tx.part
        units = kit_units.part_units(tx)
        opts = dict(options or {})
        declared = facts.get("units") if format == "dxf_dwg" else None
        opts["_units"] = opts.get("units") or (declared if declared in ("mm", "in") else units)
        opts["_binary"] = bool(facts.get("binary"))
        before = _inventory(part)
        folder.mkdir(parents=True, exist_ok=False)
        staged = folder / ("input" + suffix)
        shutil.copyfile(source, staged)
        _translate(tx, format, part, staged, folder, opts, flatten, root)
        made = _new_objects(part, before)
        if not any(made.values()):
            fail("NX_INCOMPLETE_RESULT", "The translator imported nothing",
                 details=_diagnostics(folder, root, format))  # fmt: skip
        file_units = opts["_units"] if format in ("stl", "dxf_dwg") else facts.get("units")
        report = _verify(tx, format, facts, made, file_units, units)
        sketch_row = None
        if target_sketch is not None:
            sketch_row = _into_sketch(tx, target_sketch, made["curves"], report)
    except BaseException:
        _undo_import(tx, loaded_before if new_part_path else None, previous, folder)
        raise
    bodies, curves = made["bodies"], made["curves"]
    return {
        "part": tx.ref(part, "part"),
        "target": target,
        "format": format,
        "bodies": [tx.ref(b, "body") for b in bodies[:MAX_ROWS]],
        "body_count": len(bodies),
        "solids": sum(1 for b in bodies if b.IsSolidBody),
        "sheets": sum(1 for b in bodies if not b.IsSolidBody),
        "facet_bodies": len(made["facets"]),
        "curves": [tx.ref(c, "curve") for c in curves[:MAX_ROWS]],
        "curve_count": len(curves),
        "sketch": sketch_row,
        "volume": report["volume"],
        "bounds": report["bounds"],
        "file_units": file_units,
        "components_created": len(made["components"]),
        "flatten": flatten,
        "staging": files.relative(folder, root),
    }


def _facet_bodies(part) -> list:
    """Standalone facet bodies (STL); a solid's display facets have an AssociatedBody."""
    found = []
    for f in getattr(part, "FacetedBodies", ()):
        try:
            owner = f.AssociatedBody
        except Exception:
            owner = None
        if owner is None or not int(getattr(owner, "Tag", 0) or 0):
            found.append(f)
    return found


def _inventory(part) -> dict:
    return {
        "bodies": {int(b.Tag) for b in part.Bodies},
        "facets": {int(f.Tag) for f in _facet_bodies(part)},
        "curves": {int(c.Tag) for c in part.Curves},
        "components": {int(c.Tag) for c, _ in parts.components(part)},
    }


def _new_objects(part, before) -> dict:
    return {
        "bodies": [b for b in part.Bodies if int(b.Tag) not in before["bodies"]],
        "facets": [
            f for f in _facet_bodies(part) if int(f.Tag) not in before["facets"]
        ],  # fmt: skip
        "curves": [c for c in part.Curves if int(c.Tag) not in before["curves"]],
        "components": [
            c for c, _ in parts.components(part) if int(c.Tag) not in before["components"]
        ],  # fmt: skip
    }


def _translate(tx, format, part, staged, folder, opts, flatten, root) -> None:
    codes = {CATIA_LICENSE_CODE: "NX_LICENSE_UNAVAILABLE"} if format == "catia_v5" else None
    steps = IMPORTERS[format](tx, part, staged, folder, opts, flatten)
    builder = None
    try:
        with native(codes, default="NX_IMPORT_FAILED"):
            builder = next(steps)  # destroyed in finally even if configuring fails
            for _ in steps:
                pass
            builder.Commit()
    except Exception as error:
        failure = classify(error, codes, default="NX_IMPORT_FAILED")
        failure.details.update(_diagnostics(folder, root, format))
        raise failure from error
    finally:
        if builder is not None:
            with contextlib.suppress(Exception):
                builder.Destroy()


def _verify(tx, format, facts, made, file_units, units) -> dict:
    """Volume and extent of what was imported; the extent against the file's own."""
    bodies, facets, curves = made["bodies"], made["facets"], made["curves"]
    solids = [b for b in bodies if b.IsSolidBody]
    volume = measure.volume(tx, solids) if solids else None
    if format == "iges" and facts.get("surfaces") and not bodies:
        fail("NX_INCOMPLETE_RESULT", "The IGES surfaces were not imported as bodies")
    shapes = bodies + facets + curves
    if not shapes:
        return {"volume": volume, "bounds": None}
    low, high = measure.bounds(tx, shapes)
    if facts.get("min") is not None and file_units in FILE_MM:
        k = FILE_MM[file_units] / FILE_MM[units]
        file_low = [v * k for v in facts["min"]]
        file_high = [v * k for v in facts["max"]]
        size = max(h - lo for lo, h in zip(file_low, file_high, strict=True))
        tol = kit_units.part_tolerance(tx) + 1e-6 * size
        if format == "iges":  # control points enclose the surfaces
            ok = all(a >= b - tol for a, b in zip(low, file_low, strict=True)) and all(
                a <= b + tol for a, b in zip(high, file_high, strict=True)
            )
        else:  # STL vertices and DXF curves are exact
            pairs = zip(low + high, file_low + file_high, strict=True)
            ok = all(abs(a - b) <= tol for a, b in pairs)
        readback.expect(ok, "extent", f"The imported {format} geometry's extent does not match "
                        "the file", model_min=low, model_max=high, file_min=file_low,
                        file_max=file_high, tolerance=tol)  # fmt: skip
    return {"volume": volume, "bounds": {"min": low, "max": high}}


def _into_sketch(tx, sk, curves, report) -> result.Row:
    """Move the imported X/Y curves into the sketch's plane and add them to it."""
    if not curves:
        fail("NX_INCOMPLETE_RESULT", "The file brought no curves for the sketch")
    low, high = report["bounds"]["min"], report["bounds"]["max"]
    tol = kit_units.part_tolerance(tx)
    if abs(low[2]) > tol or abs(high[2]) > tol:
        fail("NX_INVALID_ARGUMENT", "The file's curves are not in its X/Y plane (Z is not 0)",
             field="/target", hint="Import into the work part instead")  # fmt: skip
    f = ksketch.frame(sk)
    with native(default="NX_IMPORT_FAILED"):
        # NX 2406: the matrix maps the second CSYS onto the first, so the sketch frame
        # comes first to carry absolute X/Y into the sketch.
        frame = [float(v) for v in f["origin"] + f["x_axis"] + f["y_axis"]]
        matrix, status = tx.uf.Trns.CreateCsysMappingMatrix(
            frame, [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        )
        readback.expect(status == 0, "sketch_frame", status=status)
        # UF returns a 3x4 matrix; TransformEntities takes it as 4x4 (NX 2406).
        matrix = [float(v) for v in matrix] + ([0.0, 0.0, 0.0, 1.0] if len(matrix) == 12 else [])
        tx.uf.Modl.TransformEntities(len(curves), [c.Tag for c in curves], matrix)
    with ksketch.editing(tx, sk) as editing:
        known = ksketch.tags(editing)
        with native(default="NX_IMPORT_FAILED"):
            for curve in curves:
                ksketch.add(tx, editing, curve)
            ksketch.update(tx, editing)
        added = ksketch.new_geometry(editing, known)
    readback.expect(len(added) == len(curves), "sketch_geometry", added=len(added),
                    imported=len(curves))  # fmt: skip
    # Every moved curve lies in the sketch plane, inside the moved file extent.
    corners = [ksketch.to_part(f, [x, y]) for x in (low[0], high[0]) for y in (low[1], high[1])]
    box_low = [min(c[i] for c in corners) - tol for i in range(3)]
    box_high = [max(c[i] for c in corners) + tol for i in range(3)]
    got_low, got_high = measure.bounds(tx, curves)
    readback.expect(
        all(a >= b for a, b in zip(got_low, box_low, strict=True))
        and all(a <= b for a, b in zip(got_high, box_high, strict=True)),
        "sketch_extent", got_min=got_low, got_max=got_high, want_min=box_low, want_max=box_high,
    )  # fmt: skip
    for curve in curves:
        start = getattr(curve, "StartPoint", None)
        if start is not None:
            readback.expect(abs(ksketch.height(f, start)) <= tol, "sketch_plane")
    report["bounds"] = {"min": got_low, "max": got_high}
    return tx.ref(sk, "sketch")


def _undo_import(tx, loaded_before: set | None, previous: tuple, folder: Path) -> None:
    """After a failed import: close a new part (and what it loaded), reactivate the
    previous parts and remove the translator files. Into the work part, the kernel's
    undo removes the bodies, curves and components; prototypes it loaded stay loaded."""
    problems = []
    if loaded_before is not None:
        for p in reversed(list(tx.session.Parts)):
            if int(p.Tag) not in loaded_before:
                try:
                    _saving.close_part(tx, p, save=False)
                except Exception as error:
                    problems.append(f"close {parts.file_of(p)}: {error}")
        work, display = previous
        with contextlib.suppress(Exception):
            if display is not None:
                parts.activate(tx, display, work=False)
            if work is not None:
                parts.activate(tx, work, display=False)
    in_use = [
        parts.file_of(p)
        for p in tx.session.Parts
        if parts.file_of(p) and Path(parts.file_of(p)).resolve().is_relative_to(folder.resolve())
    ]
    if in_use:
        problems.append(f"translator parts in {folder.name} are still loaded")
    else:
        # NX may still hold a translator log for a moment; a leftover log changes no model.
        for _ in range(5):
            shutil.rmtree(folder, ignore_errors=True)
            if not folder.exists():
                break
            time.sleep(0.2)
    if problems:
        tx.non_undoable("; ".join(problems))
