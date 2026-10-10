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


def float_or_none(text):
    """``float(text)``, or ``None`` when NX's text is missing or not a number."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


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


# How a combined feature changes the targets' total volume.
COMBINE_CHANGE = {"unite": "more", "subtract": "less", "intersect": "less"}


def check_combine(combine, field="/combine/targets") -> None:
    """Refuse a unite with several targets before anything touches NX.

    NX 2406 unites the new geometry with each target separately: two targets become
    two overlapping bodies that both hold a copy of the tool, never one merged body.
    Unite therefore takes exactly one target; subtract and intersect take several."""
    if combine is not None and combine["operation"] == "unite" and len(combine["targets"]) > 1:
        fail(
            "NX_INVALID_ARGUMENT",
            "unite takes one target body: NX unites a separate copy of the new geometry "
            "into each target and never merges the targets",
            field=field,
            hint="Unite the target bodies first (nx_create_boolean), then pass the one "
            "result body, or combine with one target and unite the rest afterwards",
        )


def combine_targets(tx, combine, field="/combine/targets") -> list:
    """The solid target bodies of a ``Combine`` argument (``[]`` for a new body)."""
    if combine is None:
        return []
    check_combine(combine, field)
    return solids(tx, combine["targets"], field)


def body_tags(tx) -> set:
    """Tags of the work part's bodies (before a combined feature)."""
    return {int(b.Tag) for b in tx.part.Bodies}


def combined(tx, feature, combine, targets, before: float, tags: set) -> FeatureData:
    """Read back a feature built with ``combine``: result bodies are the targets NX kept
    plus any new bodies (a subtract may split a target).

    A unite whose new geometry does not touch the target leaves NX 2406 committing it as
    a separate body (measured: extrude and revolve report success, the target keeps its
    volume and the feature lists both bodies); that is refused, so the call rolls back."""
    after = list(tx.part.Bodies)
    kept = {int(b.Tag) for b in after}
    alive = [t for t in targets if int(t.Tag) in kept]
    new = [b for b in after if int(b.Tag) not in tags]
    if combine["operation"] == "unite" and new and alive:
        fail(
            "NX_BUILD_FAILED",
            "The new geometry does not touch the target body, so NX kept it as a separate "
            "body instead of uniting it",
            hint="Move or resize the profile so it overlaps or touches the target, or omit "
            "combine to make a new body",
            details={"separate_bodies": len(new)},
        )
    out = result_of(tx, feature, [*alive, *new])
    changed(before, out["volume"], COMBINE_CHANGE[combine["operation"]])
    return out
