"""Sweep Along Guide (a section along one guide, sharp corners mitred, optional wall offsets)
and Variational Sweep (a path sketch's section along its path, e.g. rails and gunwales).

NX 2406 lessons kept here (probed live):

- ``Features.CreateSweepAlongGuideBuilder``: ``Section`` and ``Guide`` are the builder's own
  sections, filled in place; ``FirstOffset``/``SecondOffset`` are expressions and
  ``BooleanOperation`` combines like an extrude. A 10 x 5 rectangle along a 100 line:
  volume 5000 (area x length); offsets 1 and -1 make a wall 2 thick around the outline
  instead: (2 x 30) x 100 = 6000. Offsets only add expressions when non-zero.
- ``Features.CreateVarsweepBuilder`` (basic freeform license, asked first: a missing one
  can open a blocking dialog) takes ``Section`` set to a new section of a sketch created
  on a path (``nx_create_sketch`` placement path; its feature's parent is
  ``DATUM_ON_PATH``); NX finds the path itself (``SketchOnPathFeature``). ``Limits``
  start/end are percentages of the path (0 and 100 by default, feature expressions "Start
  limit"/"End limit"). A 5 x 4 rectangle on an 80 long edge: 1600; end 50 with subtract
  from the block removed 800. ``FeatureOptions.BodyType`` Solid and the tolerances (0 by
  default) are set.
"""

from __future__ import annotations

from typing import Annotated

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Combine, Doc, Items, Length, Range, RefTo, SketchRef
from nx_mcp.domains.modeling import common, features
from nx_mcp.kit import args, builders, expr, licenses, measure, select, units
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import sketch as ksketch

GuideRef = Annotated[str, RefTo("sketch", "curve", "edge")]
LICENSE = "nx_freeform_1"
TOLERANCE_MM = 0.01
PATH_PARENT = "DATUM_ON_PATH"  # the feature type a path sketch's feature hangs on
Percent = Annotated[float, Range(ge=0, le=100)]


OPEN_HINT = (
    "Close the profile (nx_get_sketch lists its loops), or give first_offset and "
    "second_offset (e.g. 0.5 and -0.5) for a thin wall along it"
)


class GuideSweepData(common.FeatureData):
    guide_length: float  # total length of the guide curves


def _guide(tx, refs):
    """``(fill(into), curves)`` for a guide: one sketch, or connected curves and edges."""
    args.distinct("/guide", refs, "guide references")
    picked = []
    for i, ref in enumerate(refs):
        field = f"/guide/{i}"
        try:
            sk = ksketch.resolve(tx, select.resolve(tx, ref, "sketch", field=field), field)
        except NXError as error:
            if error.code != "NX_WRONG_KIND":
                raise
            picked.append(select.owned(tx, select.resolve(tx, ref, "curve", "edge", field=field),
                                       field, "curves"))  # fmt: skip
            continue
        if len(refs) > 1:
            fail("NX_INVALID_ARGUMENT", "A sketch guide is the only guide entry", field="/guide")
        return (lambda into, sk=sk: builders.section(tx, sk, into=into)), common.sketch_curves(sk)
    return (lambda into: builders.curve_section(tx, picked, into=into)), picked


@tool(
    effect="model",
    label="Sweep along guide",
    summary="Sweep {section} along guide {guide}",
    keywords=(
        "sweep along guide",
        "sweep with corners",
        "mitred sweep",
        "frame sweep",
        "thin wall sweep",
        "profile along path",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"section": "s:3", "guide": "s:4"},
)
def nx_create_sweep_along_guide(
    tx: Tx,
    *,
    section: Annotated[SketchRef, Doc("Section sketch, usually at the guide's start")],
    guide: Annotated[
        Annotated[list[GuideRef], Items(1, 100)] | GuideRef,
        Doc("One sketch, or connected curves/edges; corners allowed"),
    ],
    first_offset: Annotated[Length, Doc("Wall offset of the section outline; 0: solid")] = 0.0,
    second_offset: Annotated[Length, Doc("Other wall side; the wall lies between offsets")] = 0.0,
    combine: Combine | None = None,
) -> GuideSweepData:
    """Sweep one section sketch along one guide, mitring sharp guide corners (frames, bars bent
    from a polyline). nx_create_sweep needs a smooth guide. Offsets 0 and 0 (default) give a
    solid of a closed section; two different offsets (1 and -1: a wall 2 thick) a wall
    around its outline, open sections too. New body or combined. guide_length: the guide's;
    a straight guide: volume = area x length."""
    if isinstance(guide, str):
        guide = [guide]
    if section in guide:
        fail("NX_INVALID_ARGUMENT", "section and guide must differ", field="/guide")
    wall = float(first_offset) != 0 or float(second_offset) != 0
    if wall and float(first_offset) == float(second_offset):
        fail(
            "NX_INVALID_ARGUMENT",
            "first_offset and second_offset are equal: the wall between them has no thickness",
            field="/second_offset",
            hint="Give two different offsets for a wall (e.g. 1 and -1: 2 thick), or both 0 for "
            "a solid of the section",
        )
    common.check_combine(combine)
    with tx.checks():
        sk = ksketch.resolve(tx, section, "/section")
        fill, curves = _guide(tx, guide)
        if any(int(c.Tag) in ksketch.tags(sk) for c in curves):
            fail("NX_INVALID_ARGUMENT", "section and guide must differ", field="/guide")
        if not wall:
            features.refuse_open(tx, sk, "/section", OPEN_HINT)
    length = measure.length(tx, curves)
    targets = common.combine_targets(tx, combine)
    before = common.target_state(tx, targets)
    tags, known = common.body_tags(tx), kbodies.faulty(tx, targets)
    overlapping = kbodies.clashing(tx, targets)
    with builders.build(tx, tx.part.Features.CreateSweepAlongGuideBuilder, None) as b:
        tol = units.part_tolerance(tx, TOLERANCE_MM)
        b.DistanceTolerance, b.ChainingTolerance = tol, tol * 0.95
        builders.section(tx, sk, into=b.Section)
        fill(b.Guide)
        expr.drive(b.FirstOffset, first_offset)
        expr.drive(b.SecondOffset, second_offset)
        builders.boolean(tx, b.BooleanOperation, combine)
        feature = b.commit()
    common.swept_sound(tx, common.feature_bodies(feature), known, overlapping)
    if combine is not None:
        out = common.combined(tx, feature, combine, targets, before, tags)
    else:
        out = common.result_of(tx, feature)
        common.changed(0.0, out["volume"], "positive")
    return {**out, "guide_length": length}


class VariationalData(common.FeatureData):
    start: float
    end: float


@tool(
    effect="model",
    label="Variational sweep",
    summary="Variational sweep {sketch}",
    keywords=(
        "variational sweep",
        "sweep sketch on path",
        "rail",
        "gunwale",
        "rub rail",
        "section along edge",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"sketch": "s:8", "end": 100},
)
def nx_create_variational_sweep(
    tx: Tx,
    *,
    sketch: Annotated[SketchRef, Doc("Closed section sketched on the path")],
    start: Annotated[Percent, Doc("Start along the path, percent")] = 0.0,
    end: Annotated[Percent, Doc("End along the path, percent")] = 100.0,
    combine: Combine | None = None,
) -> VariationalData:
    """Sweep a section sketched on a path along that path (rails, gunwales, rub strakes).
    It runs from start to end percent of the path, as a solid or combined. Create the
    sketch with nx_create_sketch placement {path, help_point}, draw a closed profile, finish
    it. Needs the basic freeform license."""
    if not start < end:
        fail("NX_INVALID_ARGUMENT", "start must be below end", field="/end",
             hint="Percent along the path, e.g. start 0, end 100")  # fmt: skip
    common.check_combine(combine)
    with tx.checks():
        sk = ksketch.resolve(tx, sketch)
        features.refuse_open(tx, sk, "/sketch", "Draw a closed profile on the path sketch")
        parents = list(sk.Feature.GetParents()) if hasattr(sk.Feature, "GetParents") else []
        if not any(str(getattr(f, "FeatureType", "")) == PATH_PARENT for f in parents):
            fail("NX_WRONG_GEOMETRY", "The sketch is not on a path", field="/sketch",
                 hint="Create it with nx_create_sketch placement {path: [edge], help_point}, "
                 "or use nx_create_sweep_along_guide with a separate guide")  # fmt: skip
    licenses.require(tx, (LICENSE,), "Variational sweep needs the basic freeform license",
                     strict=True)  # fmt: skip
    targets = common.combine_targets(tx, combine)
    before = common.target_state(tx, targets)
    tags, known = common.body_tags(tx), kbodies.faulty(tx, targets)
    overlapping = kbodies.clashing(tx, targets)
    with builders.build(tx, tx.part.Features.CreateVarsweepBuilder, None) as b:
        b.DistanceTolerance = units.part_tolerance(tx, TOLERANCE_MM)
        b.AngularTolerance = 0.5
        b.Section = builders.section(tx, sk)
        b.FeatureOptions.BodyType = tx.nx.GeometricUtilities.FeatureOptions.BodyStyle.Solid
        b.Limits.StartExtend.Value.RightHandSide = repr(float(start))
        b.Limits.EndExtend.Value.RightHandSide = repr(float(end))
        builders.boolean(tx, b.BooleanOperation, combine)
        feature = b.commit()
    common.swept_sound(tx, common.feature_bodies(feature), known, overlapping)
    if combine is not None:
        out = common.combined(tx, feature, combine, targets, before, tags)
    else:
        out = common.result_of(tx, feature)
        common.changed(0.0, out["volume"], "positive")
    return {**out, "start": float(start), "end": float(end)}
