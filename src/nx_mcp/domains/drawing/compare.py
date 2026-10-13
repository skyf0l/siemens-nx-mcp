"""Compare the work part's drawing with an earlier revision saved on disk.

NX 2406 findings encoded here:

- ``DrawingCompareManager.CreateCompareReportBuilder(path, reuse_snapshot)`` (the part's
  ``DrawingCompare``) compares while the builder is created: its ``SummaryStatusLine``
  ("Summary: N Changes, N Additions, N Deletions") and summaries are readable without
  ``Commit``. Commit would store a comparison report and change symbols in the part, so
  the builder is only read and destroyed: the compare changes nothing.
- The deprecated ``CreateDrawingCompareSettingsBuilder`` is not used.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, PathTo
from nx_mcp.kit import builders, licenses, parts

MAX_LINES = 200
COUNTS = ("changes", "additions", "deletions")
HEADER = {"Number of Sheets": "sheets", "Number of Views": "views",
          "Number of Annotations": "annotations"}  # fmt: skip


class CompareCounts(TypedDict):
    changes: Annotated[int, Doc("Objects in both revisions that differ")]
    additions: Annotated[int, Doc("Objects only in the work part")]
    deletions: Annotated[int, Doc("Objects only in the earlier revision")]


class TypeCounts(CompareCounts):
    type: Annotated[str, Doc("NX object class, e.g. Annotations, Dimensions, Views")]


class Side(TypedDict, total=False):
    file: str
    sheets: int
    views: int
    annotations: int


class CompareData(TypedDict):
    against: str
    summary: Annotated[str, Doc("NX's summary line")]
    counts: CompareCounts
    by_type: Annotated[list[TypeCounts], Doc("Counts per object class that changed")]
    current: Annotated[Side, Doc("Object counts of the work part")]
    earlier: Annotated[Side, Doc("Object counts of the earlier revision")]
    details: Annotated[list[str], Doc("NX's per-object report lines, fields split by ~")]
    truncated: bool


def _lines(value):
    if value is None:
        return []
    items = value.splitlines() if isinstance(value, str) else [str(v) for v in value]
    return [line.strip() for line in items if line.strip()]


def counts_of(summary):
    """{changes, additions, deletions} from NX's summary line ("1 Deletion" too)."""
    out = dict.fromkeys(COUNTS, 0)
    for number, word in re.findall(r"(\d+)\s+([A-Za-z]+)", summary or ""):
        key = word.lower().rstrip("s") + "s"
        if key in out:
            out[key] = int(number)
    return out


def _int_or_none(text):
    try:
        return int(text)
    except ValueError:
        return None


def read_report(summary_lines, change_lines, detail_lines):
    """NX's report lines ("Field~work part~earlier", "Type~changes~additions~deletions")
    as sides, per-type counts and the remaining detail lines."""
    current, earlier, by_type, details = {}, {}, [], []
    for line in summary_lines + detail_lines:
        fields = line.split("~")
        if len(fields) == 3 and fields[0] in HEADER:
            key = HEADER[fields[0]]
            current[key], earlier[key] = _int_or_none(fields[1]), _int_or_none(fields[2])
        elif len(fields) == 3 and fields[0] == "File Name":
            current["file"], earlier["file"] = fields[1], fields[2]
    seen = set()
    for line in change_lines:
        fields = line.split("~")
        values = [_int_or_none(f) for f in fields[1:]]
        if len(fields) == 4 and None not in values and fields[0] not in seen:
            seen.add(fields[0])
            by_type.append({"type": fields[0], **dict(zip(COUNTS, values, strict=True))})
    skip = {"Version", "Date/Time", "File Location", "File Name", *HEADER}
    for line in detail_lines:
        head = line.split("~")[0]
        if head not in skip and not line.startswith("Summary:") and line not in change_lines:
            details.append(line)
    return current, earlier, by_type, details


@tool(
    effect="read",
    label="Compare drawing revisions",
    summary=lambda a, say: f"Compare the drawing with {say('against')}",
    keywords=(
        "drawing compare",
        "compare drawing revisions",
        "drawing changes",
        "what changed in the drawing",
        "revision differences",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    switch="display",
    example={"against": "drawings/bracket_rev_a.prt"},
)
def nx_check_drawing_changes(
    tx: Tx,
    *,
    against: Annotated[
        str, PathTo("read", ".prt"), Doc("Earlier revision: a saved .prt with drawing sheets")
    ],
) -> CompareData:
    """Compare the work part's drawing sheets with an earlier revision saved on disk. NX's
    drawing compare reports counts of changed, added and deleted drafting objects (sheets,
    views, dimensions, notes, symbols, tables) and report lines. Read only: no change
    symbols or report are stored. Additions are objects only in the work part."""
    with tx.checks():
        part = tx.part
        if parts.same_file(str(getattr(part, "FullPath", "") or ""), against):
            fail("NX_INVALID_ARGUMENT", "against is the work part's own file",
                 field="/against", hint="Pass the earlier revision's file")  # fmt: skip
        if not list(part.DrawingSheets):
            fail("NX_WRONG_APPLICATION", "The work part has no drawing sheet",
                 hint="Compare a drawing part; create sheets with nx_create_drawing_sheet")  # fmt: skip
        licenses.require(tx, ("drafting",), "Drawing compare needs a drafting license")
    before = {int(p.Tag) for p in tx.session.Parts}
    try:
        with builders.build(
            tx, part.DrawingCompare.CreateCompareReportBuilder, str(against), False
        ) as b:
            summary = str(b.SummaryStatusLine or "").strip()
            lines = [_lines(b.GetSummary()), _lines(b.GetChangesSummary())]
            lines.append(_lines(b.GetDetailSummary()))
    finally:
        _close_loaded(tx, before)
    if not summary:
        fail("NX_EXPORT_FAILED", "NX did not compare the drawings",
             hint="Check that against holds drawing sheets of the same part")  # fmt: skip
    current, earlier, by_type, details = read_report(*lines)
    return {
        "against": Path(against).name,
        "summary": summary,
        "counts": counts_of(summary),
        "by_type": by_type,
        "current": current,
        "earlier": earlier,
        "details": details[:MAX_LINES],
        "truncated": len(details) > MAX_LINES,
    }


def _close_loaded(tx, before):
    """Close parts NX loaded only to compare (the earlier revision), never others."""
    for p in list(tx.session.Parts):
        if int(p.Tag) not in before:
            p.Close(tx.nx.BasePart.CloseWholeTree.FalseValue,
                    tx.nx.BasePart.CloseModified.CloseModified, None)  # fmt: skip
