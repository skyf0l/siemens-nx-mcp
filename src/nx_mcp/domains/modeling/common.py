"""Result shapes and small checks shared by the modeling tools (this domain only)."""

from __future__ import annotations

from typing import TypedDict

from nx_mcp.core.errors import fail
from nx_mcp.kit import measure, readback, result, select

# Values within this relative band of each other count as "unchanged" volume.
VOLUME_REL = 1e-9


class FeatureData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float


def bodies_text(args, field="bodies") -> str:
    """``1 body`` / ``3 bodies`` for a panel summary."""
    n = len(args.get(field) or [])
    return f"{n} body" if n == 1 else f"{n} bodies"


def solid(tx, ref, field="/body"):
    """A solid body of the work part (a sheet body is ``NX_WRONG_GEOMETRY``)."""
    body = select.one(tx, ref, "body", field=field)
    if not getattr(body, "IsSolidBody", True):
        fail("NX_WRONG_GEOMETRY", "Select a solid body", field=field)
    return body


def solids(tx, refs, field="/bodies"):
    out = select.bodies(tx, refs, field)
    for i, body in enumerate(out):
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "Select solid bodies", field=f"{field}/{i}")
    return out


def body_of(edge_or_face):
    return edge_or_face.GetBody()


def one_body(objects, field):
    """The one body owning every edge or face in ``objects``."""
    tags = {int(body_of(o).Tag) for o in objects}
    if len(tags) != 1:
        fail("NX_INVALID_ARGUMENT", "Select edges or faces of one body", field=field)
    return body_of(objects[0])


def feature_bodies(feature):
    return list(feature.GetBodies()) if hasattr(feature, "GetBodies") else []


def body_rows(tx, bodies):
    return [tx.ref(b, kind="body") for b in bodies]


def changed(before, after, how, check="volume"):
    """Read back a volume change: ``more``, ``less``, ``at_least``, ``at_most``,
    ``differs``, ``positive`` or ``same`` (within a 1e-6 relative band)."""
    band = VOLUME_REL * max(1.0, abs(before), abs(after))
    same = abs(after - before) <= max(band, 1e-6 * max(1.0, abs(before)))
    ok = {
        "more": after > before + band,
        "less": after < before - band,
        "at_least": after > before - band,
        "at_most": after < before + band,
        "differs": not same,
        "positive": after > band,
        "same": same,
    }[how]
    readback.expect(
        ok, check, f"NX left the volume {before:.6g} -> {after:.6g}", before=before, after=after
    )
    return after


def result_of(tx, feature, bodies=None) -> FeatureData:
    """``{feature, bodies, volume}`` with the bodies' total volume after the change."""
    bodies = feature_bodies(feature) if bodies is None else list(bodies)
    readback.expect(bool(bodies), "bodies", "NX built no body")
    solid_bodies = [b for b in bodies if getattr(b, "IsSolidBody", True)]
    volume = measure.volume(tx, solid_bodies) if solid_bodies else 0.0
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": body_rows(tx, bodies),
        "volume": volume,
    }
