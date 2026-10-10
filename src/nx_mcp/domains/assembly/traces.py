"""Explosion trace lines between component edges (tool-surface 3.8).

A trace is an NX automatic trace line anchored to two edges of components in the
explosion, at a percentage of each edge's arc length. Its anchors persist as NX
handles (``exploded.TRACE_ATTRIBUTE``); every explosion change refreshes them.
"""

from __future__ import annotations

import json
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Length, Point3, Range, RefTo, Vec3
from nx_mcp.domains.assembly import common, exploded
from nx_mcp.kit import builders, geom, model, readback, result, select

Percent = Annotated[float, Range(ge=0, le=100), Doc("0-100 % of the edge's arc length")]
Direction = Annotated[
    tuple[float, float, float], Doc("Direction the line leaves this anchor; default +Z")
]
TraceEdge = Annotated[str, RefTo("edge", doc="Edge id 'e:N' of a component in the explosion")]
TraceId = Annotated[str, RefTo("traceline", doc="Trace id 'tl:N'")]


class TraceData(TypedDict, total=False):
    trace: result.Row
    explosion: result.Row
    start: Point3
    end: Point3
    start_edge: result.Row
    end_edge: result.Row
    start_percent: float
    end_percent: float
    start_direction: Vec3
    end_direction: Vec3
    start_offset: float
    end_offset: float


def _name(part) -> str:
    """A free ``TRACE<n>`` name (NX would show a journal identifier)."""
    taken = {
        str(getattr(x, "Name", "")).casefold() for x in getattr(part, "Tracelines", None) or []
    }
    n = 1
    while f"trace{n}" in taken:
        n += 1
    return f"TRACE{n}"


def _row(tx, line, ex, records) -> TraceData:
    """The trace, its endpoints, anchor edges, percents and leg offsets."""
    start, end = exploded.endpoints(line)
    edges = exploded.anchor_edges(tx, ex, records)
    return {
        "trace": tx.ref(line, "traceline"),
        "explosion": tx.ref(ex, "explosion"),
        "start": start,
        "end": end,
        "start_edge": tx.ref(edges[0], "edge"),
        "end_edge": tx.ref(edges[1], "edge"),
        "start_percent": float(records[0]["percent"]),
        "end_percent": float(records[1]["percent"]),
        "start_offset": float(line.StartOffset),
        "end_offset": float(line.EndOffset),
    }


def _edge(tx, ref, entries, field):
    edge = select.resolve(tx, ref, "edge", field=field)
    owner = getattr(edge, "OwningComponent", None)
    if (
        not getattr(edge, "IsOccurrence", False)
        or owner is None
        or int(owner.Tag) not in entries
        or entries[int(owner.Tag)][3]
    ):
        fail("NX_WRONG_OWNER", "Select an edge of an unsuppressed component in this explosion",
             field=field)  # fmt: skip
    return edge


@tool(
    effect="model",
    label="Create explosion trace",
    summary="Trace line in {explosion}",
    keywords=("trace line", "explosion line", "assembly path line", "exploded view line"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    refresh_annotations=False,
    example={"explosion": "ex:3", "start_edge": "e:40", "end_edge": "e:52"},
)
def nx_create_explosion_trace(
    tx: Tx,
    *,
    explosion: common.ExplosionId,
    start_edge: TraceEdge,
    end_edge: TraceEdge,
    start_percent: Percent = 0,
    end_percent: Percent = 0,
    start_direction: Direction | None = None,
    end_direction: Direction | None = None,
) -> TraceData:
    """Draw a trace line between edges of two components in an explosion. Each end sits at a
    percent of its edge's arc length and follows later explosion edits."""
    directions = [
        geom.unit(v if v is not None else [0.0, 0.0, 1.0], field)
        for v, field in ((start_direction, "/start_direction"), (end_direction, "/end_direction"))
    ]
    with tx.checks():
        part = common.explosion_context(tx)
        ex = common.explosion(tx, explosion)
        entries = exploded.tree(ex)
        edges = [_edge(tx, start_edge, entries, "/start_edge"),
                 _edge(tx, end_edge, entries, "/end_edge")]  # fmt: skip
    records = [
        {
            "edge": exploded.handle(tx, edge.Prototype),
            "component": exploded.handle(tx, edge.OwningComponent),
            "percent": float(percent),
        }
        for edge, percent in zip(edges, (start_percent, end_percent), strict=True)
    ]
    positions = exploded.anchor_positions(tx, ex, records)
    with native(default="NX_BUILD_FAILED"):
        points = [builders.point(tx, p) for p in positions]
        for point in points:
            point.Blank()
        line = part.Tracelines.CreateAutomaticTraceline(
            ex,
            points[0],
            builders.direction(tx, directions[0], field="/start_direction"),
            points[1],
            builders.direction(tx, directions[1], field="/end_direction"),
            builders.matrix(tx, geom.IDENTITY),
            tx.nx.AutomaticTraceline.ModeOption.Infer,
            0,
            0.0,
            0.0,
            [],
            [],
        )
        encoded = json.dumps(records)
        line.SetAttribute(exploded.TRACE_ATTRIBUTE, encoded)
        line.SetName(_name(part))
    readback.expect(
        line.GetStringAttribute(exploded.TRACE_ATTRIBUTE) == encoded, "anchors",
        "NX did not keep the trace anchors",
    )  # fmt: skip
    model.update(tx)
    start, end = exploded.endpoints(line)
    readback.expect_close(start, positions[0], "start")
    readback.expect_close(end, positions[1], "end")
    out = _row(tx, line, ex, records)
    out["start_direction"], out["end_direction"] = directions
    return out


@tool(
    effect="model",
    label="Edit explosion trace",
    summary="Edit trace {trace}",
    keywords=("move trace anchor", "trace offset", "edit trace line", "trace percent"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    refresh_annotations=False,
    example={"trace": "tl:6", "start_percent": 25},
)
def nx_edit_explosion_trace(
    tx: Tx,
    *,
    trace: TraceId,
    start_percent: Percent | None = None,
    end_percent: Percent | None = None,
    start_offset: Length | None = None,
    end_offset: Length | None = None,
) -> TraceData:
    """Slide a trace's anchors along their edges (percent) or set its end legs. An offset is
    a straight leg leaving the anchor; anchors stay put."""
    given = (start_percent, end_percent, start_offset, end_offset)
    if all(v is None for v in given):
        fail("NX_INVALID_ARGUMENT", "Give start/end_percent or start/end_offset",
             hint='e.g. {"trace": "tl:6", "start_percent": 50}')  # fmt: skip
    with tx.checks():
        line = select.resolve(tx, trace, "traceline", field="/trace")
        if not exploded.is_managed(tx, line):
            fail("NX_UNSUPPORTED_OPTION", "Only traces made by nx_create_explosion_trace",
                 field="/trace")  # fmt: skip
        common.explosion_context(tx)
        ex = line.AskExplosion()
        records = exploded.records_of(line)
    for record, value in zip(records, (start_percent, end_percent), strict=True):
        if value is not None:
            record["percent"] = float(value)
    with native(default="NX_BUILD_FAILED"):
        line.SetAttribute(exploded.TRACE_ATTRIBUTE, json.dumps(records))
        if start_offset is not None:
            line.StartOffset = float(start_offset)
        if end_offset is not None:
            line.EndOffset = float(end_offset)
    exploded.refresh_traces(tx, ex)
    exploded.regenerate(tx)
    for name, offset in (("StartOffset", start_offset), ("EndOffset", end_offset)):
        if offset is not None:
            readback.expect_close(float(getattr(line, name)), offset, name)
    return _row(tx, line, ex, records)
