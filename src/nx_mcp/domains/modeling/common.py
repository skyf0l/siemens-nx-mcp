"""Result shapes and small checks shared by the modeling tools (this domain only)."""

from __future__ import annotations

import math
from typing import Literal, TypedDict

from nx_mcp.core.errors import fail
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, facts, geom, measure, readback, result, select, units
from nx_mcp.kit import sketch as ksketch

# Values within this relative band of each other count as "unchanged" volume.
VOLUME_REL = 1e-9


# Bare principal names where a type has no principal form of its own (a rotation axis
# vector, a datum plane reference); the core Axis, Direction and Plane take them already.
PrincipalName = Literal["x", "y", "z", "-x", "-y", "-z"]
PRINCIPALS = frozenset({"x", "y", "z", "-x", "-y", "-z"})
PlaneName = Literal["xy", "yz", "xz"]
PLANE_NAMES = frozenset({"xy", "yz", "xz"})
# A path joint turning by more than this many degrees is a corner that a sweep guide or a
# tube path cannot follow (NX 2406: "Some curves in Guide sections not tangent continuous",
# "Nonlinear guide objects must be smooth with a continuous tangent").
CORNER_DEGREES = 0.5
SMOOTH_HINT = (
    "Round the corners with nx_create_sketch_corner (kind fillet) or draw the path with arcs "
    "or splines: it must be tangent-continuous (G1); a polyline cannot be followed"
)


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


def same_volume(before: float, after: float) -> bool:
    """Equal volumes within ``changed``'s "same" band."""
    band = VOLUME_REL * max(1.0, abs(before), abs(after))
    return abs(after - before) <= max(band, 1e-6 * max(1.0, abs(before)))


def changed(before, after, how, check="volume"):
    """Read back a volume change: ``more``, ``less``, ``at_least``, ``at_most``,
    ``differs``, ``positive`` or ``same`` (within a 1e-6 relative band)."""
    band = VOLUME_REL * max(1.0, abs(before), abs(after))
    same = same_volume(before, after)
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


def sound(tx, made, known=()) -> None:
    """After a build: an invalid (self-intersecting) body fails and rolls back; bodies
    spanning over 10 m get a warning (``kit.bodies``)."""
    kbodies.expect_sound(tx, made, known=known)
    note = kbodies.scale_note(tx, made)
    if note:
        tx.warn("NX_UPDATE_WARNING", note)


def swept_sound(tx, made, known=(), overlapping=()) -> None:
    """:func:`sound` for a body swept along a path (tube, sweeps), plus the self-overlap
    check: NX 2406 commits a tube wider than its helix pitch as a solid whose faces cut each
    other, which UF consistency passes. ``overlapping``: ``kit.bodies.clashing`` of the
    combine targets before the build."""
    sound(tx, made, known)
    no_clash(tx, made, overlapping)


def no_clash(tx, made, overlapping=(), hint=kbodies.CLASH_HINT) -> None:
    """``kit.bodies.expect_no_clash``; a body too large to examine gets an ``NX_NOTE``
    warning that says the self-overlap check was skipped."""
    note = kbodies.expect_no_clash(tx, made, known=overlapping, hint=hint)
    if note:
        tx.warn("NX_NOTE", note)


def boolean_feature(tx, operation, target, tools):
    """Commit one boolean feature (``unite``/``subtract``/``intersect``) of ``tools``
    with ``target``; returns the feature."""
    with builders.build(tx, tx.part.Features.CreateBooleanBuilder, None) as b:
        b.Operation = getattr(tx.nx.Features.FeatureBooleanType, builders.BOOLEAN_TYPES[operation])
        b.Target = target
        b.Tools.Add(list(tools))
        return b.commit()


# Volume band of a unite, boolean or transform read-back: NX mass properties of B-spline
# bodies differ by up to about 1e-5 relative between two evaluations of the same shape.
VOLUME_BAND = 1e-4
INSIDE, OUTSIDE = 1, 2  # UF_MODL_ask_point_containment against a body


def inner_point(tx, body):
    """A point strictly inside ``body``: a face sample stepped inward along the outward
    normal and confirmed by UF point containment; None when no face gives one."""
    modeling = tx.uf.Modeling
    tol = units.part_tolerance(tx)
    try:
        for face in list(body.GetFaces())[:8]:
            for point, normal in facts.interior_samples(tx, face, 1):
                for depth in (10.0 * tol, 2.0 * tol):
                    q = geom.sub(point, geom.scale(normal, depth))
                    if int(modeling.AskPointContainment(q, body.Tag)) == INSIDE:
                        return q
    except Exception:  # a body UF cannot sample: the drop check skips it
        return None
    return None


def probes(tx, tools) -> list[tuple[int, list[float]]]:
    """``[(index, inner point)]`` of the ``tools`` bodies, taken before a unite (tools UF
    cannot sample are left out)."""
    found = [(i, inner_point(tx, t)) for i, t in enumerate(tools)]
    return [(i, q) for i, q in found if q]


def dropped_tools(tx, made, inner) -> list[int]:
    """Indexes of tools whose inner point lies outside every result body: a unite holds
    every point of every tool, so such a tool was left out."""
    out = []
    for i, q in inner:
        try:
            states = [int(tx.uf.Modeling.AskPointContainment(q, b.Tag)) for b in made]
        except Exception:
            continue
        if states and all(s == OUTSIDE for s in states):
            out.append(i)
    return out


GROWN_HINT = (
    "Unite the tools in separate calls and check each volume; a tool that closes a hollow of "
    "the target leaves a sealed cavity NX may fill: build that wall into the target's own "
    "profile instead (nx_check_model lists sealed voids)"
)
DROPPED_HINT = (
    "Unite the dropped tool in its own nx_create_boolean call and check that the volume "
    "grows; nx_measure_section shows whether its material is there"
)


def unite_volume(
    tx,
    made,
    before: float,
    tools_volume: float,
    inner,
    *,
    name=lambda i: f"tools[{i}]",
    field: str | None = "/tools",
) -> None:
    """Refuse a unite whose result ``made`` holds less than the target (``before``), more
    than the target plus the tools, or misses a tool's material (``inner``: :func:`probes`
    taken before the unite). ``name``/``field`` say a dropped tool's index."""
    after = measure.volume(tx, made)
    total = before + tools_volume
    band = VOLUME_BAND * max(1.0, total)
    readback.expect(
        after >= before - band,
        "volume",
        f"NX's unite holds {after:.6g}, less than the target alone ({before:.6g})",
        before=before,
        after=after,
    )
    if after > total + band:
        fail(
            "NX_VERIFICATION_FAILED",
            f"NX's unite holds {after:.6g}, more than the target and tools together "
            f"({total:.6g}): it filled an enclosed cavity or turned a body inside out",
            hint=GROWN_HINT,
            details={"check": "volume", "after": after, "target": before, "tools": tools_volume},
            retry="after_fix",
        )
    if after >= total - band:
        return  # no volume shared or lost: every tool is there
    dropped = dropped_tools(tx, made, inner)
    if dropped:
        fail(
            "NX_VERIFICATION_FAILED",
            f"NX left {name(dropped[0])} out of the unite: a point inside it lies outside "
            f"the result ({len(dropped)} tool(s) dropped)",
            field=f"{field}/{dropped[0]}" if field else None,
            hint=DROPPED_HINT,
            details={
                "check": "tools",
                "dropped": dropped,
                "after": after,
                "target": before,
                "tools": tools_volume,
            },
            retry="after_fix",
        )


def unite_into(tx, target, tools):
    """Unite the separate ``tools`` bodies into ``target`` (one boolean feature) and read it
    back like nx_create_boolean (:func:`unite_volume`): every tool is consumed, none was
    dropped, and the body holds at least the target's volume and at most the sum. Returns
    ``(feature, united body)``."""
    target_volume = measure.volume(tx, target)
    tools_volume = measure.volume(tx, list(tools))
    inner = probes(tx, tools)
    feature = boolean_feature(tx, "unite", target, tools)
    alive = {int(b.Tag) for b in tx.part.Bodies}
    left = [t for t in tools if int(t.Tag) in alive]
    made = feature_bodies(feature) or [target]
    if left or len(made) != 1:
        # NX 2406 commits a unite of copies that miss the target and keeps several
        # bodies (measured: 4 for a circular pattern of a seed off the axis).
        fail(
            "NX_BUILD_FAILED",
            f"The copies did not unite into one body: NX kept {max(len(made), len(left) + 1)} "
            "bodies",
            hint="Copies must overlap or touch the seed's body to unite; omit unite to keep "
            "separate bodies",
            details={"bodies": max(len(made), len(left) + 1)},
        )
    unite_volume(
        tx,
        made,
        target_volume,
        tools_volume,
        inner,
        name=lambda i: f"patterned body {i}",
        field=None,
    )
    return feature, made[0]


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
    if new and alive and same_volume(before, measure.volume(tx, alive)):
        # The targets are untouched and a body appeared: the boolean was not applied (a
        # split target changes volume, so it never lands here).
        fail(
            "NX_BUILD_FAILED",
            f"NX did not apply the {combine['operation']}: the tool body was left separate "
            "and the target kept its volume",
            hint="Make sure the new geometry overlaps the target body; or omit combine and "
            "combine the new body with nx_create_boolean",
            details={"separate_bodies": len(new), "target_volume": before},
        )
    out = result_of(tx, feature, [*alive, *new])
    changed(before, out["volume"], COMBINE_CHANGE[combine["operation"]])
    return out


def combine_new(tx, feature, made, combine, target, before: float) -> FeatureData:
    """Combine the bodies ``made`` by a feature without a Boolean option (sweep, loft)
    with one ``target`` through a separate Boolean feature; the result names ``feature``."""
    joined = boolean_feature(tx, combine["operation"], target, made)
    kept = {int(b.Tag) for b in tx.part.Bodies}
    left = [b for b in made if int(b.Tag) in kept]
    if combine["operation"] == "unite" and left:
        fail(
            "NX_BUILD_FAILED",
            "The new body does not touch the target body, so NX kept it separate",
            hint="Move the sections so the body overlaps or touches the target, or omit "
            "combine to make a new body",
            details={"separate_bodies": len(left)},
        )
    bodies = feature_bodies(joined) or [target]
    out = result_of(tx, joined, [b for b in bodies if int(b.Tag) in kept])
    changed(before, out["volume"], COMBINE_CHANGE[combine["operation"]])
    out["feature"] = tx.ref(feature, kind="feature")
    return out


def one_target(tx, combine, what: str) -> list:
    """The one target body of a ``Combine`` for a feature combined by a Boolean feature."""
    check_one_target(combine, what)
    if combine is None:
        return []
    return solids(tx, combine["targets"], "/combine/targets")


def check_one_target(combine, what: str) -> None:
    """Refuse, before NX, a ``Combine`` of a Boolean-combined feature with several targets."""
    if combine is not None and len(combine["targets"]) != 1:
        fail(
            "NX_INVALID_ARGUMENT",
            f"A {what} combines with one target",
            field="/combine/targets",
            hint="Combine with one body, then combine the result with the others",
        )


def principal(value):
    """``{"principal": value}`` for a bare principal axis name (``"z"``, ``"-x"``), else
    ``value`` unchanged (a ref, a vector or an Axis/Direction object)."""
    return {"principal": value} if isinstance(value, str) and value in PRINCIPALS else value


def principal_plane(value):
    """``{"principal": value}`` for a bare principal plane name (``"yz"``), else ``value``
    unchanged (a ref or a Plane object)."""
    return {"principal": value} if isinstance(value, str) and value in PLANE_NAMES else value


def sketch_curves(sk) -> list:
    """The sketch's non-construction geometry (a path or guide)."""
    return [c for c in ksketch.geometry(sk) if not getattr(c, "IsReference", False)]


def corners(tx, curves) -> list[dict]:
    """``[{"point", "angle"}]``: joints where two of ``curves`` meet end to end with
    tangents turning by more than ``CORNER_DEGREES``. Objects that cannot be evaluated
    (sketch points) are skipped; ends where three or more curves meet are left to NX."""
    ends = []  # (curve index, point, unit tangent pointing out of the curve)
    for i, c in enumerate(curves):
        try:
            with curve.evaluator(tx, c) as e:
                (p0, t0), (p1, t1) = e.at(0.0), e.at(1.0)
        except Exception:  # a point or an object UF Eval does not take
            continue
        if geom.norm(t0) < 1e-12 or geom.norm(t1) < 1e-12:
            continue
        ends += [(i, p0, geom.scale(geom.unit(t0), -1.0)), (i, p1, geom.unit(t1))]
    tol = units.part_tolerance(tx)
    groups: list[list] = []
    for end in ends:
        for group in groups:
            if geom.dist(group[0][1], end[1]) <= max(tol, geom.tol(group[0][1], end[1])):
                group.append(end)
                break
        else:
            groups.append([end])
    found = []
    for group in groups:
        if len(group) != 2 or group[0][0] == group[1][0]:
            continue  # a free end, a branch, or a closed curve's own seam
        cos = max(-1.0, min(1.0, -geom.dot(group[0][2], group[1][2])))
        angle = math.degrees(math.acos(cos))
        if angle > CORNER_DEGREES:
            found.append({"point": [round(v, 6) for v in group[0][1]], "angle": round(angle, 3)})
    return found


def refuse_corners(tx, curves, field: str, what: str) -> None:
    """Refuse a sweep guide or tube path with sharp corners before NX builds it."""
    sharp = corners(tx, curves)
    if sharp:
        worst = max(sharp, key=lambda c: c["angle"])
        fail(
            "NX_WRONG_GEOMETRY",
            f"The {what} has {len(sharp)} sharp corner(s) (up to {worst['angle']:.3g} deg at "
            f"{worst['point']}); it must be tangent-continuous",
            field=field,
            hint=SMOOTH_HINT,
            details={"corners": sharp[:10]},
        )


def smooth_hint(error) -> None:
    """Add ``SMOOTH_HINT`` to NX's own refusal of a path that is not tangent-continuous."""
    text = str(error.message).lower()
    if (
        error.code == "NX_BUILD_FAILED"
        and not error.hint
        and ("tangent" in text or "smooth" in text)
    ):
        error.hint = SMOOTH_HINT
