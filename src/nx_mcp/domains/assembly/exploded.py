"""Explosion internals shared by the explosion, trace, animation and update tools.

NX 2406 findings encoded here:

- An explosion stores a component-local post-transform relative to the pose the
  occurrence inherits from its exploded parent. Setting an absolute exploded pose
  therefore first resets (unexplodes) the component, measures the inherited pose
  and explodes by the local delta; parents are placed before their children.
- Managed traces keep persistent component and edge handles in the
  ``NX_MCP_TRACE_V1`` attribute. Their endpoints follow the exploded poses only when
  refreshed: every explosion edit, display change, animation and assembly update
  refreshes them. A trace whose endpoints coincide (assembled state) is blanked.
- A smart point on an occurrence edge at a percentage of arc length evaluates at
  once (no model update); it is created inside ``tx.scratch()`` and undone.
"""

from __future__ import annotations

import contextlib
import json
import math
from typing import TYPE_CHECKING

from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.domains.assembly import common, structure
from nx_mcp.kit import geom, model, readback
from nx_mcp.kit import pose as kpose

if TYPE_CHECKING:  # explosions imports this module
    from nx_mcp.domains.assembly.explosions import ExplodedRow

TRACE_ATTRIBUTE = "NX_MCP_TRACE_V1"


def uf(tx):
    return tx.uf.Assem


def tree(explosion) -> dict:
    """``{tag: (exploded child, component, names, hidden)}`` depth first."""
    out = {}

    def walk(parent, names, hidden):
        for child in parent.GetChildren():
            component = child.GetComponent()
            path = [*names, str(component.Name)]
            gone = hidden or bool(component.IsSuppressed)
            out[int(component.Tag)] = (child, component, path, gone)
            walk(child, path, gone)

    walk(explosion.RootComponent, [], False)
    return out


def exploded_pose(child):
    return structure.pose(child)


def entry_row(tx, entry) -> ExplodedRow:
    child, component, names, hidden = entry
    t, r = exploded_pose(child)
    at, ar = structure.pose(component)
    return {
        "component": common.component_row(tx, component, names, with_pose=False),
        "translation": common.tidy(t),
        "rotation": common.tidy(r),
        "assembled_translation": common.tidy(at),
        "assembled_rotation": common.tidy(ar),
        "suppressed": hidden,
    }


def modeling_views(part) -> list:
    views = getattr(part, "ModelingViews", None)
    try:
        return list(views) if views is not None else []
    except TypeError:
        return []


def views(tx, part, explosion) -> list:
    """Rows of the modeling and drawing views that show ``explosion``."""
    tag = int(explosion.Tag)
    out = []
    for kind, values in (("modeling_view", modeling_views(part)),
                         ("drawing_view", list(part.DraftingViews))):  # fmt: skip
        for view in values:
            if int(uf(tx).AskViewExplosion(view.Tag) or 0) == tag:
                out.append(tx.ref(view, kind))
    return out


def drawing_views(tx, part, explosion) -> list:
    tag = int(explosion.Tag)
    return [v for v in part.DraftingViews if int(uf(tx).AskViewExplosion(v.Tag) or 0) == tag]


def regenerate(tx):
    """Redraw the graphics window (interactive NX only)."""
    if not tx.session.IsBatch:
        with native(default="NX_ERROR"):
            tx.uf.Disp.RegenerateDisplay()


def shifted(shift):
    """A target for :func:`apply`: the inherited pose moved by ``shift`` (assembly axes)."""
    return lambda base: kpose.relative(base, [float(v) for v in shift])


def apply(tx, explosion, placements) -> dict:
    """Set exploded poses: ``placements`` is ``[(tag, target)]``, ``target`` an absolute
    ``(t, r)`` or a function of the inherited pose (the pose the component takes from its
    exploded parent, its assembled pose for a root child; see :func:`shifted`). Returns
    ``{tag: absolute target}``."""
    entries = tree(explosion)
    api = uf(tx)
    placed = {}
    for tag, target in sorted(placements, key=lambda p: len(entries[p[0]][2])):
        component = entries[tag][1]
        with native(default="NX_BUILD_FAILED"):
            api.UnexplodeComponent(explosion.Tag, component.Tag)
            base = exploded_pose(tree(explosion)[tag][0])
            pose = target(base) if callable(target) else target
            api.ExplodeComponent(explosion.Tag, component.Tag, kpose.local_delta(base, pose))
        placed[tag] = pose
    return placed


def reset(tx, explosion, tags):
    entries = tree(explosion)
    for tag in sorted(tags, key=lambda t: len(entries[t][2])):
        with native(default="NX_BUILD_FAILED"):
            uf(tx).UnexplodeComponent(explosion.Tag, entries[tag][1].Tag)


# ---------------------------------------------------------------------------
# Traces


def handle(tx, obj) -> str:
    return str(tx.uf.Tag.AskHandleFromTag(obj.Tag))


def _stale(message):
    fail("NX_STALE_REFERENCE", message, hint="Recreate the trace with nx_create_explosion_trace")


def _occurrence_edge(tx, entries, record):
    try:
        component_tag = int(tx.uf.Tag.AskTagOfHandle(record["component"]))
        edge_tag = int(tx.uf.Tag.AskTagOfHandle(record["edge"]))
    except Exception:
        _stale("A trace anchor no longer resolves")
    entry = entries.get(component_tag)
    if entry is None or entry[3]:
        _stale("A trace component is missing or suppressed")
    component = entry[1]
    edges = [e for b in component.Prototype.Bodies for e in b.GetEdges() if int(e.Tag) == edge_tag]
    if len(edges) != 1:
        _stale("A trace edge is missing")
    edge = component.FindOccurrence(edges[0])
    if edge is None:
        _stale("The trace edge has no occurrence")
    return entry, edge


def anchor_edges(tx, explosion, records) -> list:
    """Occurrence edges the trace anchors sit on."""
    entries = tree(explosion)
    return [_occurrence_edge(tx, entries, r)[1] for r in records]


def anchor_positions(tx, explosion, records) -> list:
    """Exploded positions of trace anchors (component, edge, percent of arc length)."""
    entries = tree(explosion)
    part = tx.part
    found = [_occurrence_edge(tx, entries, r) for r in records]
    assembled = []
    with tx.scratch():
        for (_entry, edge), record in zip(found, records, strict=True):
            with native(default="NX_BUILD_FAILED"):
                scalar = part.Scalars.CreateScalar(
                    float(record["percent"]),
                    tx.nx.Scalar.DimensionalityType.NotSet,
                    tx.nx.SmartObject.UpdateOption.WithinModeling,
                )
                point = part.Points.CreatePoint(
                    edge,
                    scalar,
                    tx.nx.PointCollection.PointOnCurveLocationOption.PercentArcLength,
                    tx.nx.SmartObject.UpdateOption.WithinModeling,
                )
                assembled.append(geom.xyz(point.Coordinates))
    out = []
    for ((child, component, _n, _h), _edge), p in zip(found, assembled, strict=True):
        local = kpose.compose(kpose.inverse(structure.pose(component)), (p, kpose.identity()))[0]
        out.append(kpose.compose(exploded_pose(child), (local, kpose.identity()))[0])
    return out


def is_managed(tx, line) -> bool:
    with contextlib.suppress(Exception):
        return bool(line.HasUserAttribute(TRACE_ATTRIBUTE, tx.nx.NXObject.AttributeType.String, -1))
    return False


def records_of(line) -> list:
    return json.loads(line.GetStringAttribute(TRACE_ATTRIBUTE))


def traces(tx, part, explosion) -> list:
    """Trace lines of ``explosion`` in ``part``."""
    tag = int(explosion.Tag)
    out = []
    for line in getattr(part, "Tracelines", None) or []:
        with contextlib.suppress(Exception):
            if int(line.AskExplosion().Tag) == tag:
                out.append(line)
    return out


def endpoints(line):
    return geom.xyz(line.StartPoint.Coordinates), geom.xyz(line.EndPoint.Coordinates)


def leg_directions(line) -> list | None:
    """``[start, end]`` unit directions the trace legs leave their anchors, or None when
    NX does not answer."""
    try:
        return [geom.unit(geom.xyz(d.Vector)) for d in (line.StartDirection, line.EndDirection)]
    except Exception:
        return None


def trace_row(tx, line, explosion) -> dict:
    """A trace's endpoints, anchor edges, percents, leg offsets and directions. A trace not
    made by nx_create_explosion_trace has only its endpoints; one whose anchor edges are
    gone says ``stale``."""
    start, end = endpoints(line)
    row: dict = {"trace": tx.ref(line, "traceline"), "explosion": tx.ref(explosion, "explosion"),
                 "start": start, "end": end}  # fmt: skip
    if is_managed(tx, line):
        records = records_of(line)
        try:
            edges = anchor_edges(tx, explosion, records)
        except NXError:
            row["stale"] = True
        else:
            row["start_edge"], row["end_edge"] = (tx.ref(e, "edge") for e in edges)
        row["start_percent"] = float(records[0]["percent"])
        row["end_percent"] = float(records[1]["percent"])
    with contextlib.suppress(Exception):
        row["start_offset"], row["end_offset"] = float(line.StartOffset), float(line.EndOffset)
    directions = leg_directions(line)
    if directions is not None:
        row["start_direction"], row["end_direction"] = directions
    return row


def refresh_traces(tx, explosion, mark=None) -> list:
    """Move managed trace endpoints to the current exploded anchors; returns the lines."""
    part = tx.part
    pending = []
    for line in traces(tx, part, explosion):
        if is_managed(tx, line):
            pending.append((line, anchor_positions(tx, explosion, records_of(line))))
    for line, positions in pending:
        with native(default="NX_BUILD_FAILED"):
            if math.dist(*positions) <= 1e-9:
                line.Blank()
                continue
            line.Unblank()
            for point, position in zip((line.StartPoint, line.EndPoint), positions, strict=True):
                point.SetCoordinates(tx.nx.Point3d(*position))
    if pending:
        model.update(tx, mark)
        for i, (line, positions) in enumerate(pending):
            if math.dist(*positions) > 1e-9:
                got = endpoints(line)
                readback.expect_close(got[0], positions[0], f"trace[{i}].start")
                readback.expect_close(got[1], positions[1], f"trace[{i}].end")
    return [line for line, _ in pending]
