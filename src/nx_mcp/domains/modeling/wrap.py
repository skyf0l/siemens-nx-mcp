"""Wrap geometry: a convex solid envelope around bodies (tool-surface 3.6).

NX 2406 lessons kept here: ``WrapGeometryBuilder.Geometry`` is a selection list filled
with ``Add``; NX facets the inputs to ``DistTol`` (default 2.54 mm, far too coarse for a
measured envelope) and grows the faceted hull a little (measured: 0.0135 mm at 0.1 mm
tolerance with a 1 mm offset, none for two blocks without offset) so it never cuts into
the inputs, then by ``AddOffset``. ``CloseGaps`` says how the offset faces meet:
sharp (extended), beveled (a flat between them) or no offset (the bare hull). The
result is one new solid; the inputs stay.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, Items, Length
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, expr, measure, readback, select, units

CloseGaps = Literal["sharp", "beveled", "no_offset"]
GAP_TYPES = {"sharp": "Sharp", "beveled": "Beveled", "no_offset": "NoOffset"}
# Default faceting tolerance in millimetres (NX's own default is 2.54 mm).
TOLERANCE_MM = 0.1


class WrapData(common.FeatureData):
    inputs_volume: float  # the wrapped solids' total volume


@tool(
    effect="model",
    label="Wrap bodies",
    summary=lambda a, say: f"Wrap {common.bodies_text(a)}",
    keywords=("wrap geometry", "convex hull", "envelope", "shrink wrap", "simplify", "enclose"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"bodies": ["b:3", "b:7"], "offset": 1},
)
def nx_create_wrap_geometry(
    tx: Tx,
    *,
    bodies: Annotated[list[BodyRef], Items(1, 100)],
    offset: Annotated[Length, Doc("Extra growth of the envelope, >= 0")] = 0.0,
    close_gaps: Annotated[
        CloseGaps, Doc("How offset faces meet: extended, flat bevel or none")
    ] = "sharp",
    tolerance: Annotated[Length, Doc("Faceting tolerance; the hull clears inputs by about this")]
    | None = None,
) -> WrapData:
    """Wrap bodies in one convex solid envelope (keeps the inputs), for space claims or
    simplified envelopes. The hull is faceted to tolerance (default 0.1 mm) and
    clears the inputs by offset plus a fraction of tolerance. volume: the envelope's."""
    if float(offset) < 0:
        fail("NX_INVALID_ARGUMENT", "offset must be 0 or more", field="/offset")
    if tolerance is not None and float(tolerance) <= 0:
        fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field="/tolerance",
             hint="Omit it for 0.1 mm; a larger value makes a coarser, looser hull")  # fmt: skip
    found = select.bodies(tx, bodies, "/bodies")
    solids = [b for b in found if getattr(b, "IsSolidBody", True)]
    inputs = measure.volume(tx, solids) if solids else 0.0
    tags = common.body_tags(tx)
    with builders.build(tx, tx.part.Features.CreateWrapGeometryBuilder, None) as b:
        b.CloseGaps = getattr(
            tx.nx.Features.WrapGeometryBuilder.CloseGapType, GAP_TYPES[close_gaps]
        )
        b.DistTol = (
            units.part_tolerance(tx, TOLERANCE_MM) if tolerance is None else float(tolerance)
        )
        expr.drive(b.AddOffset, offset)
        expr.drive(b.SplitOffset, 0.0)
        for body in found:
            b.Geometry.Add(body)
        feature = b.commit()
    made = [x for x in common.feature_bodies(feature) if int(x.Tag) not in tags]
    made = made or common.feature_bodies(feature)
    readback.expect(len(made) == 1, "bodies", f"NX built {len(made)} wrap bodies", bodies=len(made))
    common.sound(tx, made)
    out = common.result_of(tx, feature, made)
    common.changed(inputs, out["volume"], "at_least")
    low, high = measure.bounds(tx, made)
    in_low, in_high = measure.bounds(tx, found)
    slack = units.part_tolerance(tx) * 10
    inside = all(low[i] <= in_low[i] + slack and high[i] >= in_high[i] - slack for i in range(3))
    readback.expect(inside, "bounds", "The envelope does not enclose the inputs",
                    envelope=[low, high], inputs=[in_low, in_high])  # fmt: skip
    return {**out, "inputs_volume": inputs}
