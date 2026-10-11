"""Threads: the installed NX thread table, cosmetic/detailed threads (tool-surface 3.6).

The thread table is read in place from ``UGII/modeling_standards/NX_Thread_Standard.xml``
of the running NX installation; the Hole and Thread commands use the same rows.

Names are matched loosely: a standard by its letters and digits or a common alias
('metric', 'ISO metric coarse', 'UNC'), a size by its letters and number values
('m6x1' is 'M6 x 1.0') or as the one size it starts ('M6' is 'M6 x 1.0' in Metric
Coarse). The installed table repeats every size per radial engagement (0.75, 0.5 or
Standard, and Custom); without one, 0.75 (else Standard) is used.

NX 2406 lessons kept here: a detailed internal thread on a bore at or above the major
diameter fails with "Tool body completely outside target body" (670030) and a reversed
direction with "Invalid thread axis direction" (671154); a symbolic one is accepted
silently, so both are refused before NX builds anything. A bore cut by subtracting a
cylinder threads only reversed from its top ("Thread depth must be greater than zero",
674805): that build is repeated with the direction flipped. ``LimitOption.Full`` threads the
whole face and follows it.
"""

from __future__ import annotations

import difflib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Doc,
    FaceRef,
    Items,
    Length,
    Offset,
    PageLimit,
    Range,
    Text,
    forbid_extra,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, expr, geom, measure, readback, result, select

if TYPE_CHECKING:
    from typing_extensions import TypeIs

MAX_TABLE_BYTES = 20_000_000
StandardText = Annotated[str, Text(64), Doc("e.g. 'Metric Coarse', 'UNC'")]
SizeText = Annotated[str, Text(64), Doc("e.g. 'M6 x 1.0' or 'M6'")]
# Metric sizes ('M6') without a standard are looked up in the first pair, others in the second.
DEFAULT_STANDARDS = (("Metric Coarse", "Metric Fine"), ("Inch UNC", "Inch UNF"))
# Natural names of common standards (keys: lower-case letters, digits and dots).
STANDARD_ALIASES = {
    "metric": "Metric Coarse",
    "iso": "Metric Coarse",
    "isometric": "Metric Coarse",
    "isometriccoarse": "Metric Coarse",
    "metricisocoarse": "Metric Coarse",
    "isometricfine": "Metric Fine",
    "metricisofine": "Metric Fine",
    "unifiedcoarse": "Inch UNC",
    "unifiednationalcoarse": "Inch UNC",
    "unifiedfine": "Inch UNF",
    "unifiednationalfine": "Inch UNF",
    "unifiedextrafine": "Inch UNEF",
    "trapezoidal": "Metric Trapezoidal",
    "tr": "Metric Trapezoidal",
}
# Radial engagements taken, in this order, when the call names none.
ENGAGEMENTS = ("0.75", "Standard")
# NX 2406 refusals of a thread build, with what to change.
# NX 2406 "Thread depth must be greater than zero": the thread runs out of the face from
# start_face. NX threads along the cylinder's own axis, which for a bore cut by a Boolean
# subtract points away from the start face; the build is repeated reversed.
THREAD_OUTWARD = 674805
BUILD_HINTS = {
    670030: "A detailed thread found no material to cut: an internal thread needs a bore below "
    "the major diameter (the tap drill), an external one a shaft at the major diameter",
    671154: "NX threads from start_face into the material: leave reverse false and pass the "
    "planar end face the thread starts at",
}


def thread_rows() -> list[dict]:
    """Every ``ThreadedHole`` row of the installed NX thread table."""
    base = os.environ.get("UGII_BASE_DIR")
    if not base:
        fail("NX_CATALOG_UNAVAILABLE", "The NX installation root (UGII_BASE_DIR) is not set")
    source = Path(base) / "UGII" / "modeling_standards" / "NX_Thread_Standard.xml"
    if not source.is_file():
        fail("NX_CATALOG_UNAVAILABLE", "The installed NX thread table was not found")
    if source.stat().st_size > MAX_TABLE_BYTES:
        fail("NX_CATALOG_UNAVAILABLE", "The NX thread table exceeds the supported size")
    return [dict(x.attrib) for x in ET.parse(source).getroot().iter("ThreadedHole")]


def _standard_key(text) -> str:
    return re.sub(r"[^a-z0-9.]", "", str(text).casefold())


def _size_key(text) -> str:
    """Case, spaces and number spelling ignored: 'M6 x 1.0', 'm6x1' and 'M6X1' agree."""
    key = str(text).casefold().replace("×", "x")
    key = re.sub(r"\d+(?:\.\d+)?", lambda m: f"{float(m.group()):g}", key)
    return re.sub(r"[\s_]+", "", key)


def standards(rows) -> list[str]:
    return sorted({r["Standard"] for r in rows if r.get("Standard")})


def resolve_standard(rows, text, field="/standard") -> str:
    """The table's name of a standard spelled loosely ('metric coarse', 'ISO metric',
    'UNC'); otherwise ``NX_NOT_FOUND`` with the close names."""
    names = standards(rows)
    if text in names:
        return text
    by_key = {_standard_key(n): n for n in names}
    key = _standard_key(text)
    alias = _standard_key(STANDARD_ALIASES.get(key, ""))
    for candidate in (key, alias, "inch" + key, "metric" + key, key.removeprefix("iso")):
        if candidate in by_key:
            return by_key[candidate]
    near = [by_key[k] for k in difflib.get_close_matches(key, list(by_key), 3, 0.5)]
    guess = f"; did you mean {near[0]!r}?" if near else ""
    fail(
        "NX_NOT_FOUND",
        f"Thread standard {text!r} is not in the installed NX thread table{guess}",
        field=field,
        hint="Common: 'Metric Coarse', 'Metric Fine', 'Inch UNC', 'Inch UNF'; list every "
        "standard with nx_list_thread_sizes()",
        details={"close_matches": near},
    )


def _sizes(rows, standard) -> list[str]:
    out: list[str] = []
    for r in rows:
        if r.get("Standard") == standard and r.get("Size") and r["Size"] not in out:
            out.append(r["Size"])
    return out


def match_sizes(rows, standard, text) -> list[str]:
    """Sizes of ``standard`` that ``text`` names: the size itself, the size spelled loosely
    ('m6x1'), else every size it starts ('M6' -> ['M6 x 1.0'])."""
    sizes = _sizes(rows, standard)
    if text in sizes:
        return [text]
    key = _size_key(text)
    same = [s for s in sizes if _size_key(s) == key]
    if same:
        return same[:1]
    return [
        s
        for s in sizes
        if _size_key(s).startswith(key) and _size_key(s)[len(key) : len(key) + 1] in ("x", "-", "(")
    ]


def resolve_size(rows, standard, text, field="/size") -> tuple[str, str]:
    """``(standard, size)`` of a size spelled loosely. Without a standard a metric size
    ('M6') is looked up in Metric Coarse then Fine, any other in Inch UNC then UNF."""
    if standard is not None:
        tried = [resolve_standard(rows, standard, field.rsplit("/", 1)[0] + "/standard")]
    else:
        metric = re.match(r"^\s*m\s*\d", str(text), re.IGNORECASE) is not None
        names = standards(rows)
        tried = [s for s in DEFAULT_STANDARDS[0 if metric else 1] if s in names]
    for name in tried:
        found = match_sizes(rows, name, text)
        if len(found) == 1:
            return name, found[0]
        if found:
            fail(
                "NX_INVALID_ARGUMENT",
                f"Thread size {text!r} matches {len(found)} sizes of {name!r}",
                field=field,
                hint=f"Give one of them, e.g. {found[0]!r}",
                details={"candidates": found[:20]},
            )
    keys = {_size_key(s): s for name in tried for s in _sizes(rows, name)}
    near = [keys[k] for k in difflib.get_close_matches(_size_key(text), list(keys), 3, 0.6)]
    guess = f"; did you mean {near[0]!r}?" if near else ""
    where = " or ".join(repr(t) for t in tried) or "the thread table"
    fail(
        "NX_NOT_FOUND",
        f"Thread size {text!r} is not in {where}{guess}",
        field=field,
        hint=(
            f"List sizes with nx_list_thread_sizes(standard={tried[0]!r}), or give the standard"
            if tried
            else "Give the standard; list them with nx_list_thread_sizes()"
        ),
        details={"close_matches": near},
    )


def _same_value(value, row_value) -> bool:
    """Equal as numbers (0.75 and '0.75'), else as case-insensitive text."""
    number = common.float_or_none(value)
    if number is not None:
        return common.float_or_none(row_value) == number
    return str(value).strip().casefold() == str(row_value).casefold()


def pick_row(rows, method=None, engagement=None, field="/spec") -> dict:
    """One table row of a size: ``method`` and ``engagement`` match loosely (0.75 and
    '0.75' agree); without an engagement 0.75 (else Standard) is taken, never Custom."""
    for key, value, column, default in (
        ("method", method, "Method", "omit method"),
        ("engagement", engagement, "RadialEngage", "omit engagement (0.75)"),
    ):
        if value is None:
            continue
        found = [r for r in rows if _same_value(value, r.get(column))]
        if not found:
            offered = sorted({str(r.get(column)) for r in rows})
            fail(
                "NX_NOT_FOUND",
                f"Thread {key} {value!r} is not offered for {rows[0].get('Size')!r}",
                field=f"{field}/{key}",
                hint=f"Use one of {', '.join(map(repr, offered))} or {default}",
                details={"offered": offered},
            )
        rows = found
    if engagement is not None:
        return rows[0]
    for preferred in ENGAGEMENTS:
        for r in rows:
            if r.get("RadialEngage") == preferred:
                return r
    return next((r for r in rows if r.get("RadialEngage") != "Custom"), rows[0])


def size_rows(rows, standard, size, field="/size") -> list[dict]:
    """The table rows of one size given loosely (``standard`` may be None)."""
    name, found = resolve_size(rows, standard, size, field)
    return [r for r in rows if r.get("Standard") == name and r.get("Size") == found]


def check_units(tx, row, field):
    """Metric standards need a mm part and inch standards an inch part."""
    from nx_mcp.kit import units

    metric = row.get("Unit") == "Metric"
    if metric != (units.part_units(tx) == "mm"):
        fail(
            "NX_UNSUPPORTED_OPTION",
            f"{row.get('Standard')} is {row.get('Unit')}; the work part is in "
            f"{units.part_units(tx)}",
            field=field,
        )


class ThreadRow(TypedDict, total=False):
    standard: str
    unit: str
    size: str
    method: str
    engagement: str
    engagements: list[str]
    callout: str
    major_diameter: float
    minor_diameter: float
    tap_drill: float
    pitch: float
    angle: float
    starts: int


class ThreadList(TypedDict):
    items: list[ThreadRow]
    page: dict[str, Any]


def _row(r, detail):
    out = {
        "standard": r.get("Standard"),
        "unit": r.get("Unit"),
        "size": r.get("Size"),
        "method": r.get("Method"),
        "engagement": r.get("RadialEngage"),
        "callout": r.get("Callout"),
    }
    if detail:
        for key, name in (
            ("major_diameter", "MajorDiameter"),
            ("minor_diameter", "MinorDiameter"),
            ("tap_drill", "TapDrillDia"),
            ("pitch", "Pitch"),
            ("angle", "Angle"),
        ):
            out[key] = common.float_or_none(r.get(name))
        starts = common.float_or_none(r.get("NumStarts"))
        out["starts"] = int(starts) if starts is not None else None
    return {k: v for k, v in out.items() if v is not None}


def _size_summaries(rows, standard) -> list[dict]:
    """One row per size of ``standard`` with the engagements the table offers for it."""
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("Standard") != standard or not r.get("Size"):
            continue
        row = out.setdefault(
            r["Size"],
            {
                "standard": standard,
                "size": r["Size"],
                "callout": r.get("Callout"),
                "major_diameter": common.float_or_none(r.get("MajorDiameter")),
                "pitch": common.float_or_none(r.get("Pitch")),
                "engagements": [],
            },
        )
        if r.get("RadialEngage") and r["RadialEngage"] not in row["engagements"]:
            row["engagements"].append(r["RadialEngage"])
    return [{k: v for k, v in row.items() if v is not None} for row in out.values()]


@tool(
    effect="read",
    label="List thread sizes",
    summary=lambda a, say: "Thread table " + (a.get("standard") or a.get("size") or "standards"),
    keywords=("thread table", "thread catalog", "tap drill", "thread standard", "metric coarse"),
    cost="light",
    example={"standard": "Metric Coarse", "size": "M6"},
)
def nx_list_thread_sizes(
    tx: Tx,
    *,
    standard: StandardText | None = None,
    size: SizeText | None = None,
    offset: Offset = 0,
    limit: PageLimit = 50,
) -> ThreadList:
    """List NX thread standards, a standard's sizes, or one size's rows (diameters, tap drill
    and pitch per engagement). A size alone is looked up in Metric Coarse/Fine or Inch UNC/UNF."""
    rows = thread_rows()
    if standard is None and size is None:
        found = [
            {"standard": n, "unit": next(r.get("Unit") for r in rows if r.get("Standard") == n)}
            for n in standards(rows)
        ]
    elif size is None:
        found = _size_summaries(rows, resolve_standard(rows, standard))
    else:
        found = [_row(r, True) for r in size_rows(rows, standard, size)]
    items, page = result.page(found, offset, limit)
    return {"items": items, "page": page}


class TableSpec(TypedDict, total=False):
    standard: StandardText
    size: SizeText
    method: Annotated[str, Text(16)]
    engagement: Annotated[float | Annotated[str, Text(16)], Doc("0.75 (default) or 0.5")]


class _ManualRequired(TypedDict):
    pitch: Length
    major_diameter: Length
    minor_diameter: Length


class ManualSpec(_ManualRequired, total=False):
    angle: Angle
    starts: Annotated[int, Range(ge=1, le=16)]


forbid_extra(TableSpec, ManualSpec)


def _is_table(spec: TableSpec | ManualSpec) -> TypeIs[TableSpec]:
    """A spec without a pitch is a table row (a manual spec always has one)."""
    return "pitch" not in spec


class ThreadReadback(TypedDict):
    internal: bool
    pitch: float
    major_diameter: float
    minor_diameter: float
    standard: str | None
    size: str | None


class ThreadItem(TypedDict):
    feature: result.Row
    face: result.Row
    length: float


class ThreadData(TypedDict, total=False):
    feature: result.Row
    bodies: list[result.Row]
    volume: float
    internal: bool
    pitch: float
    major_diameter: float
    minor_diameter: float
    standard: str | None
    size: str | None
    engagement: str | None
    length: float
    threads: Annotated[list[ThreadItem], Doc("Several faces: one thread feature each")]


def _table_row(spec: TableSpec) -> dict:
    if not spec.get("size"):
        fail(
            "NX_INVALID_ARGUMENT",
            "A table thread needs size",
            field="/spec/size",
            hint="e.g. spec {'size': 'M6'} or {'standard': 'Metric Fine', 'size': 'M8 x 1'}",
        )
    rows = size_rows(thread_rows(), spec.get("standard"), spec["size"], "/spec/size")
    return pick_row(rows, spec.get("method"), spec.get("engagement"), "/spec")


def _check_manual(spec: ManualSpec) -> None:
    if float(spec["minor_diameter"]) >= float(spec["major_diameter"]):
        fail("NX_INVALID_ARGUMENT", "minor_diameter must be below major", field="/spec")
    if min(float(spec["pitch"]), float(spec["minor_diameter"])) <= 0:
        fail("NX_INVALID_ARGUMENT", "pitch and diameters must be positive", field="/spec")
    if not 0 < float(spec.get("angle", 60.0)) < 180:
        fail("NX_INVALID_ARGUMENT", "angle must be between 0 and 180", field="/spec/angle")


def _seed_feature(face):
    """The feature that made a bore face, or None: the first feature of its body listing
    the face. NX 2406 lists a hole's face on both the HOLE PACKAGE ('SIMPLE HOLE(2)') and
    its instance ('SIMPLE HOLE(2:1A)'); the package comes first and holds the diameter
    nx_edit_feature changes."""
    try:
        for feature in face.GetBody().GetFeatures():
            if any(int(f.Tag) == int(face.Tag) for f in feature.GetFaces()):
                return feature
    except Exception:  # an object without the feature listing: no seed named
        return None
    return None


def _check_fit(tx, face, internal, diameter, fit, field="/face") -> None:
    """Refuse a thread with no material to thread: a bore at or above the major diameter
    (a clearance hole for that size), a shaft at or below the minor one."""
    major, minor, tap, size = fit
    tol = 1e-6 * max(1.0, diameter)
    if internal and major is not None and diameter >= major - tol:
        name = size or f"major diameter {major:.6g}"
        drill = tap if tap else minor
        seed = _seed_feature(face)
        details: dict[str, Any] = {"bore": diameter, "major_diameter": major}
        if drill:
            details["tap_drill"] = drill
        if seed is not None:
            details["seed_feature"] = tx.ref(seed, kind="feature")
        where = f" ({details['seed_feature']['id']})" if seed is not None else ""
        fail(
            "NX_WRONG_GEOMETRY",
            f"This bore is {diameter:.6g} across: a clearance hole for {name}, not a tapped "
            "hole; an internal thread needs the bore at the tap drill",
            field=field,
            hint="For a tapped hole, edit the hole that made this bore"
            + where
            + (f" to the tap drill {drill:.6g}" if drill else " to the tap drill")
            + " with nx_edit_feature (parameters [{'name': 'diameter', 'value': "
            + (f"{drill:.6g}" if drill else "<tap drill>")
            + "}]) and thread it then, or make it with nx_create_hole form 'threaded'. "
            "Keep a clearance hole unthreaded",
            details=details,
        )
    if not internal and minor is not None and diameter <= minor + tol:
        fail(
            "NX_WRONG_GEOMETRY",
            f"This shaft is {diameter:.6g} across; an external thread of minor diameter "
            f"{minor:.6g} needs a larger shaft (its major diameter)",
            field=field,
            hint="Pick a smaller size or thread a larger shaft",
        )


# A custom internal thread whose minor diameter is off the bore by more than this many
# pitches, or whose depth (major - bore) / 2 exceeds one pitch, is reported.
MINOR_OFF_PITCHES = 0.25


def _check_custom(tx, spec, plans) -> None:
    """Warn about a custom (manual) spec: no table row of the part's unit system has its
    major diameter and pitch, or an internal thread does not fit its bore."""
    from nx_mcp.kit import units

    major, minor, pitch = (float(spec[k]) for k in ("major_diameter", "minor_diameter", "pitch"))
    try:
        rows = thread_rows()
    except NXError:
        rows = []
    unit = "Metric" if units.part_units(tx) == "mm" else "Inch"

    def number(row, key):
        return common.float_or_none(row.get(key)) or -1.0

    sizes = [
        r
        for r in rows
        if r.get("Unit") == unit
        and abs(number(r, "MajorDiameter") - major) <= 1e-3 * max(1.0, major)
        and abs(number(r, "Pitch") - pitch) <= 1e-6 * max(1.0, pitch)
    ]
    # A standard minor diameter: a row's minor diameter or tap drill within this share of
    # the pitch (the rows differ by engagement).
    near_minor = MINOR_OFF_PITCHES * pitch
    if rows and not any(
        min(abs(number(r, "MinorDiameter") - minor), abs(number(r, "TapDrillDia") - minor))
        <= near_minor
        for r in sizes
    ):
        if sizes:
            row = sizes[0]
            known = (
                f"; {row.get('Size')} has minor diameter {number(row, 'MinorDiameter'):g} "
                f"and tap drill {number(row, 'TapDrillDia'):g}"
            )
        else:
            known = ""
        tx.warn(
            "NX_NOTE",
            f"Non-standard thread: no {unit.lower()} row of the NX thread table has major "
            f"diameter {major:g}, pitch {pitch:g} and minor diameter {minor:g}{known}; give "
            "spec {'size': ...} for a standard thread",
        )
    for plan in plans:
        if not plan["internal"]:
            continue
        bore = 2 * plan["radius"]
        if abs(minor - bore) > MINOR_OFF_PITCHES * pitch or (major - bore) / 2 > pitch:
            tx.warn(
                "NX_NOTE",
                f"The custom internal thread (major {major:g}, minor {minor:g}) does not fit "
                f"the {bore:.6g} bore: a tapped bore is about the minor diameter",
            )
            break


def _end_faces(tx, target, axis) -> list:
    """Planar faces next to the cylinder whose normal is along its axis (its ends)."""
    found: dict[int, Any] = {}
    for edge in target.GetEdges():
        for face in edge.GetFaces():
            if int(face.Tag) in found or int(face.Tag) == int(target.Tag):
                continue
            kind, _point, normal = select.face_data(tx, face)
            if kind == select.FACE_PLANE and geom.parallel(geom.unit(normal), axis, 1e-6):
                found[int(face.Tag)] = face
    return list(found.values())


def _start_face(tx, target, axis, field="/face"):
    ends = _end_faces(tx, target, axis)
    if len(ends) == 1:
        return ends[0]
    which = "" if field == "/face" else f" {field[1:]}"
    fail(
        "NX_INVALID_ARGUMENT",
        f"Give start_face: the cylinder{which} has {len(ends) or 'no'} planar end face"
        + ("s" if len(ends) != 1 else ""),
        field="/start_face",
        hint="Pass the planar face at the end the thread starts from"
        + (" (one of details.candidates)" if ends else ""),
        details={"candidates": [tx.ref(f, kind="face") for f in ends]},
    )


def _full_length(tx, target, start, axis) -> float:
    """Distance from the start face's plane to the far end of the cylinder face."""
    _kind, origin, _normal = select.face_data(tx, start)
    reach = [
        abs(geom.dot(geom.sub(geom.xyz(v), origin), axis))
        for edge in target.GetEdges()
        for v in edge.GetVertices()
    ]
    if not reach or max(reach) <= 0:
        fail("NX_INVALID_ARGUMENT", "Give length: the face length is unknown", field="/length")
    return max(reach)


def _plan(tx, ref, field, start_face, length, fit) -> dict:
    """What one face's thread needs: the face, its radius, start face and span."""
    target = select.one(tx, ref, "face", field=field)
    data = tx.uf.Modeling.AskFaceData(target.Tag)
    if int(data[0]) != select.FACE_CYLINDER:
        fail("NX_WRONG_GEOMETRY", "Select a cylindrical face", field=field)
    radius, axis = float(data[4]), geom.unit([float(v) for v in data[2]])
    internal = float(data[6]) < 0
    _check_fit(tx, target, internal, 2 * radius, fit, field=field)
    body = target.GetBody()
    if start_face is None:
        start = _start_face(tx, target, axis, field)
    else:
        start = select.one(tx, start_face, "face", field="/start_face")
        if int(start.GetBody().Tag) != int(body.Tag):
            fail("NX_WRONG_OWNER", "start_face must be on the face's body", field="/start_face")
    full = length is None or length == "full"
    span = _full_length(tx, target, start, axis) if full else float(length)
    return {"face": target, "radius": radius, "body": body, "start": start, "span": span,
            "full": full, "field": field, "internal": internal}  # fmt: skip


def _build(tx, plan, row, manual, length, options) -> tuple[Any, ThreadReadback]:
    """Commit one thread feature; returns ``(feature, read-back)``."""
    detailed, hand, reverse = options
    T = tx.nx.Features.ThreadBuilder
    diameter = 2 * plan["radius"]
    try:
        with builders.build(tx, tx.part.Features.CreateThreadBuilder, None) as b:
            b.SmartThread = False
            b.ThreadType = T.Type.Detailed if detailed else T.Type.Symbolic
            b.CylindricalFace.Value = plan["face"]
            b.StartObject.Value = plan["start"]
            b.ShaftDiameterExp.RightHandSide = repr(diameter)
            b.TapDrillDiameterExp.RightHandSide = repr(diameter)
            if row is not None:
                b.ThreadInput = T.Input.ThreadTable
                b.ThreadStandard = row["Standard"]
                b.ThreadSize = row["Size"]
                b.ThreadMethod = row["Method"]
                b.RadialEngage = row["RadialEngage"]
                b.MatchThreadSizeToCylinder = False
            elif manual is not None:
                b.ThreadInput = T.Input.Manual
                b.NumStarts = int(manual.get("starts", 1))
                expr.drive(b.PitchExp, manual["pitch"])
                expr.drive(b.MajorDiameterExp, manual["major_diameter"])
                expr.drive(b.MinorDiameterExp, manual["minor_diameter"])
                expr.drive(b.AngleExp, manual.get("angle", 60.0))
            b.ThreadLimit = T.LimitOption.Value
            expr.drive(b.ThreadLength, plan["span"] if plan["full"] else length)
            if plan["full"]:
                b.ThreadLimit = T.LimitOption.Full  # follows the face's length
            b.ThreadHandedness = T.Handedness.LeftHand if hand == "left" else T.Handedness.RightHand
            b.ReverseThreadDirection = bool(reverse)
            feature = b.commit(validate=True)
            got: ThreadReadback = {
                "internal": bool(b.IsInternalThread),
                "pitch": float(b.Pitch),
                "major_diameter": float(b.MajorDiameter),
                "minor_diameter": float(b.MinorDiameter),
                "standard": str(b.ThreadStandard) if row is not None else None,
                "size": str(b.ThreadSize) if row is not None else None,
            }
    except NXError as error:
        if error.code == "NX_BUILD_FAILED" and not error.hint:
            error.hint = BUILD_HINTS.get(error.nx_code or 0) or (
                "Check that start_face is a planar end of the cylinder and length fits the face"
            )
        raise
    if row is not None:
        readback.expect(
            (got["standard"], got["size"]) == (row["Standard"], row["Size"]),
            "table_row",
            "NX did not keep the selected standard and size",
            standard=got["standard"],
            size=got["size"],
        )
    elif manual is not None:
        for key in ("pitch", "major_diameter", "minor_diameter"):
            readback.expect_close(got[key], float(manual[key]), key)
    return feature, got


@tool(
    effect="model",
    label="Create thread",
    summary=lambda a, say: (
        "Thread "
        + (a["spec"].get("size") or f"pitch {a['spec'].get('pitch')}")
        + f" on {say('faces') if a.get('faces') else say('face')}"
    ),
    keywords=("thread", "tap", "cosmetic thread", "symbolic thread", "detailed thread", "screw"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:9", "fc:12"], "spec": {"size": "M6"}},
)
def nx_create_thread(
    tx: Tx,
    *,
    face: Annotated[FaceRef, Doc("Cylindrical face: a bore or a shaft")] | None = None,
    faces: Annotated[list[FaceRef], Items(1, 50), Doc("Several faces, one thread each")]
    | None = None,
    spec: TableSpec | ManualSpec,
    start_face: Annotated[FaceRef, Doc("Planar end it starts at; default each face's only one")]
    | None = None,
    length: Annotated[Length | Literal["full"], Doc("Default 'full': the whole face")]
    | None = None,
    detailed: Annotated[bool, Doc("Helical geometry instead of a symbolic thread")] = False,
    hand: Literal["right", "left"] = "right",
    reverse: bool = False,
) -> ThreadData:
    """Thread a cylindrical face, or several (faces: one thread each). spec: a table size
    ('M6': Metric Coarse, engagement 0.75) or pitch and diameters. A bore must be the tap
    drill (a clearance bore is refused). Symbolic keeps the volume; detailed cuts it."""
    args.exactly_one(face=face, faces=faces)
    refs = [face] if face is not None else args.distinct("/faces", faces or [], "faces")
    if length is not None and length != "full" and float(length) <= 0:
        fail("NX_INVALID_ARGUMENT", "length must be positive", field="/length")
    row: dict[str, str] | None = None
    manual: ManualSpec | None = None
    if _is_table(spec):
        row = _table_row(spec)
        major = common.float_or_none(row.get("MajorDiameter"))
        minor = common.float_or_none(row.get("MinorDiameter"))
        tap = common.float_or_none(row.get("TapDrillDia"))
        size = row.get("Size")
    else:
        manual = spec
        _check_manual(spec)
        major, minor, tap = float(spec["major_diameter"]), float(spec["minor_diameter"]), None
        size = None
    plans = [
        _plan(
            tx,
            ref,
            "/face" if face is not None else f"/faces/{i}",
            start_face,
            length,
            (major, minor, tap, size),
        )  # fmt: skip
        for i, ref in enumerate(refs)
    ]
    if manual is not None:
        _check_custom(tx, manual, plans)
    if row is not None:
        check_units(tx, row, "/spec/standard")
    bodies = list({int(p["body"].Tag): p["body"] for p in plans}.values())
    before = [measure.volume(tx, body) for body in bodies]
    made = []
    for plan in plans:
        try:
            made.append(_build(tx, plan, row, manual, length, (detailed, hand, reverse)))
        except NXError as error:
            if error.nx_code != THREAD_OUTWARD:
                raise
            made.append(_build(tx, plan, row, manual, length, (detailed, hand, not reverse)))
        tx.checkpoint()
    out = common.result_of(tx, made[0][0], bodies)
    for i, (body, volume) in enumerate(zip(bodies, before, strict=True)):
        common.changed(volume, measure.volume(tx, body), "less" if detailed else "same",
                       check="volume" if len(bodies) == 1 else f"volume[{i}]")  # fmt: skip
    engagement = row.get("RadialEngage") if row is not None else None
    data: ThreadData = {**out, **made[0][1], "engagement": engagement, "length": plans[0]["span"]}
    if len(plans) > 1:
        data["threads"] = [
            {
                "feature": tx.ref(f, kind="feature"),
                "face": tx.ref(p["face"], kind="face"),
                "length": p["span"],
            }  # fmt: skip
            for p, (f, _got) in zip(plans, made, strict=True)
        ]
    return data
