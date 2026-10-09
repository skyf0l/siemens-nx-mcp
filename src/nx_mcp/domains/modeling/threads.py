"""Threads: the installed NX thread table, cosmetic/detailed threads (tool-surface 3.6).

The thread table is read in place from ``UGII/modeling_standards/NX_Thread_Standard.xml``
of the running NX installation; the Hole and Thread commands use the same rows.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Doc, Length, Offset, PageLimit, Range, RefTo, Text
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, measure, readback, result, select

MAX_TABLE_BYTES = 20_000_000
CatalogText = Annotated[str, Text(64), Doc("Exactly as nx_list_thread_sizes lists it")]


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


def select_rows(rows, standard, size=None, field="/standard"):
    """Rows of ``standard`` (and ``size``); unknown names are ``NX_NOT_FOUND``."""
    if not any(r.get("Standard") == standard for r in rows):
        fail(
            "NX_NOT_FOUND",
            f"Thread standard {standard!r} is not in the installed NX thread table",
            field=field,
            hint="List standards with nx_list_thread_sizes",
        )
    out = [r for r in rows if r.get("Standard") == standard]
    if size is not None:
        out = [r for r in out if r.get("Size") == size]
        if not out:
            fail(
                "NX_NOT_FOUND",
                f"Thread size {size!r} is not in {standard!r}",
                field=field.rsplit("/", 1)[0] + "/size",
                hint="List sizes with nx_list_thread_sizes(standard=...)",
            )
    return out


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


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class ThreadRow(TypedDict, total=False):
    standard: str
    unit: str
    size: str
    method: str
    engagement: str
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
            out[key] = _float(r.get(name))
        starts = _float(r.get("NumStarts"))
        out["starts"] = int(starts) if starts is not None else None
    return {k: v for k, v in out.items() if v is not None}


@tool(
    effect="read",
    label="List thread sizes",
    summary=lambda a, say: "Thread table " + (a.get("standard") or "standards"),
    keywords=("thread table", "thread catalog", "tap drill", "thread standard", "metric coarse"),
    cost="light",
    example={"standard": "Metric Coarse"},
)
def nx_list_thread_sizes(
    tx: Tx,
    *,
    standard: CatalogText | None = None,
    size: CatalogText | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ThreadList:
    """List installed NX thread standards, the sizes of one standard, or one size's rows. A
    size row gives diameters, tap drill and pitch."""
    rows = thread_rows()
    if standard is None:
        if size is not None:
            fail("NX_INVALID_ARGUMENT", "Give the standard of the size", field="/standard")
        names = sorted({r.get("Standard") for r in rows if r.get("Standard")})
        found = [
            {"standard": n, "unit": next(r.get("Unit") for r in rows if r.get("Standard") == n)}
            for n in names
        ]
    else:
        found = [_row(r, size is not None) for r in select_rows(rows, standard, size)]
    items, page = result.page(found, offset, limit)
    return {"items": items, "page": page}


class TableSpec(TypedDict, total=False):
    standard: CatalogText
    size: CatalogText
    method: CatalogText
    engagement: CatalogText


class _ManualRequired(TypedDict):
    pitch: Length
    major_diameter: Length
    minor_diameter: Length


class ManualSpec(_ManualRequired, total=False):
    angle: Angle
    starts: Annotated[int, Range(ge=1, le=16)]


for _td in (TableSpec, ManualSpec):
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


class ThreadData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float
    internal: bool
    pitch: float
    major_diameter: float
    minor_diameter: float
    standard: str | None
    size: str | None


@tool(
    effect="model",
    label="Create thread",
    summary=lambda a, say: (
        "Thread "
        + (a["spec"].get("size") or f"pitch {a['spec'].get('pitch')}")
        + f" on {say('face')}"
    ),
    keywords=("thread", "tap", "cosmetic thread", "symbolic thread", "detailed thread", "screw"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={
        "face": "fc:9",
        "start_face": "fc:4",
        "length": 10,
        "spec": {"standard": "Metric Coarse", "size": "M6 x 1.0"},
    },
)
def nx_create_thread(
    tx: Tx,
    *,
    face: RefTo("face"),  # type: ignore[valid-type]
    start_face: RefTo("face"),  # type: ignore[valid-type]
    length: Length,
    spec: TableSpec | ManualSpec,
    detailed: Annotated[bool, Doc("Helical geometry instead of a symbolic thread")] = False,
    hand: Literal["right", "left"] = "right",
    reverse: bool = False,
) -> ThreadData:
    """Add a thread to a cylindrical face, from an installed table row (standard, size) or from
    explicit pitch and diameters. Symbolic threads keep the volume; detailed ones cut it."""
    if length <= 0:
        fail("NX_INVALID_ARGUMENT", "length must be positive", field="/length")
    table = "standard" in spec or "size" in spec
    row = None
    if table:
        if not spec.get("standard") or not spec.get("size"):
            fail("NX_INVALID_ARGUMENT", "A table thread needs standard and size", field="/spec")
        rows = [
            r
            for r in select_rows(thread_rows(), spec["standard"], spec["size"], "/spec/standard")
            if spec.get("method") in (None, r.get("Method"))
            and spec.get("engagement") in (None, r.get("RadialEngage"))
        ]
        if len(rows) != 1:
            fail(
                "NX_AMBIGUOUS_REFERENCE" if rows else "NX_NOT_FOUND",
                "Pick one table row with method and engagement",
                field="/spec",
                details={"candidates": [_row(r, False) for r in rows[:20]]},
            )
        row = rows[0]
    else:
        if spec["minor_diameter"] >= spec["major_diameter"]:
            fail("NX_INVALID_ARGUMENT", "minor_diameter must be below major", field="/spec")
        if min(spec["pitch"], spec["minor_diameter"]) <= 0:
            fail("NX_INVALID_ARGUMENT", "pitch and diameters must be positive", field="/spec")
        if not 0 < spec.get("angle", 60.0) < 180:
            fail("NX_INVALID_ARGUMENT", "angle must be between 0 and 180", field="/spec/angle")
    target = select.one(tx, face, "face", field="/face")
    start = select.one(tx, start_face, "face", field="/start_face")
    kind, _point, _axis = select.face_data(tx, target)
    if kind != select.FACE_CYLINDER:
        fail("NX_WRONG_GEOMETRY", "Select a cylindrical face", field="/face")
    body = target.GetBody()
    if int(start.GetBody().Tag) != int(body.Tag):
        fail("NX_WRONG_OWNER", "start_face must be on the face's body", field="/start_face")
    if row is not None:
        check_units(tx, row, "/spec/standard")
    radius = float(tx.uf.Modeling.AskFaceData(target.Tag)[4])
    before = measure.volume(tx, body)
    T = tx.nx.Features.ThreadBuilder
    with builders.build(tx, tx.part.Features.CreateThreadBuilder, None) as b:
        b.SmartThread = False
        b.ThreadType = T.Type.Detailed if detailed else T.Type.Symbolic
        b.CylindricalFace.Value = target
        b.StartObject.Value = start
        b.ShaftDiameterExp.RightHandSide = repr(2 * radius)
        b.TapDrillDiameterExp.RightHandSide = repr(2 * radius)
        if row is not None:
            b.ThreadInput = T.Input.ThreadTable
            b.ThreadStandard = row["Standard"]
            b.ThreadSize = row["Size"]
            b.ThreadMethod = row["Method"]
            b.RadialEngage = row["RadialEngage"]
            b.MatchThreadSizeToCylinder = False
        else:
            b.ThreadInput = T.Input.Manual
            b.NumStarts = int(spec.get("starts", 1))
            b.PitchExp.RightHandSide = repr(float(spec["pitch"]))
            b.MajorDiameterExp.RightHandSide = repr(float(spec["major_diameter"]))
            b.MinorDiameterExp.RightHandSide = repr(float(spec["minor_diameter"]))
            b.AngleExp.RightHandSide = repr(float(spec.get("angle", 60.0)))
        b.ThreadLimit = T.LimitOption.Value
        b.ThreadLength.RightHandSide = repr(float(length))
        b.ThreadHandedness = T.Handedness.LeftHand if hand == "left" else T.Handedness.RightHand
        b.ReverseThreadDirection = bool(reverse)
        feature = b.commit(validate=True)
        got = {
            "internal": bool(b.IsInternalThread),
            "pitch": float(b.Pitch),
            "major_diameter": float(b.MajorDiameter),
            "minor_diameter": float(b.MinorDiameter),
            "standard": str(b.ThreadStandard) if row is not None else None,
            "size": str(b.ThreadSize) if row is not None else None,
        }
    if row is not None:
        readback.expect(
            (got["standard"], got["size"]) == (row["Standard"], row["Size"]),
            "table_row",
            "NX did not keep the selected standard and size",
            standard=got["standard"],
            size=got["size"],
        )
    else:
        for key in ("pitch", "major_diameter", "minor_diameter"):
            readback.expect_close(got[key], float(spec[key]), key)
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "less" if detailed else "same")
    return {**out, **got}
