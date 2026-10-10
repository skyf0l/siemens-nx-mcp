"""Primitive solids: block, cylinder, cone, sphere (one tool, ``shape`` variants).

NX 2406 builds each with its own feature builder (``BlockFeatureBuilder``,
``CylinderBuilder``, ``ConeBuilder``, ``SphereBuilder``); every size is a feature
expression, so a size given as an expression name or formula keeps the primitive driven
by it. A new body is read back by volume and centroid against the closed-form values; a
combined one like an extrude (``common.combined``).
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Combine, Doc, Length, Point3, Vec3
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, expr, geom, measure, readback, select

Shape = Literal["block", "cylinder", "cone", "sphere"]
# The sizes each shape takes (any other size is refused, never ignored).
SIZES: dict[str, tuple[str, ...]] = {
    "block": ("length", "width", "height"),
    "cylinder": ("diameter", "height"),
    "cone": ("diameter", "top_diameter", "height"),
    "sphere": ("diameter",),
}
OPTIONAL = {"top_diameter"}  # a cone without top_diameter ends in an apex
# Mass properties run at 0.999 accuracy: the read-back allows this relative error.
VOLUME_REL = 1e-3


def _summary(a, say) -> str:
    text = f"Create {a.get('shape') or 'block'}"
    if a.get("combine"):
        text += " " + a["combine"]["operation"]
    return text


def _check(shape: str, sizes: dict, x_direction, centered) -> None:
    allowed = SIZES[shape]
    for name, value in sizes.items():
        if value is None:
            if name in allowed and name not in OPTIONAL:
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"A {shape} needs {', '.join(n for n in allowed if n not in OPTIONAL)}",
                    field="/" + name,
                )
            continue
        if name not in allowed:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{name} is not a size of a {shape}; a {shape} takes {', '.join(allowed)}",
                field="/" + name,
            )
        minimum_zero = name == "top_diameter"
        if (
            not math.isfinite(float(value))
            or float(value) < 0
            or (float(value) == 0 and not minimum_zero)
        ):
            fail("NX_INVALID_ARGUMENT", f"{name} must be positive", field="/" + name)
    top = sizes["top_diameter"]
    if (
        shape == "cone"
        and top is not None
        and math.isclose(float(top), float(sizes["diameter"]), rel_tol=1e-9)
    ):
        fail(
            "NX_INVALID_ARGUMENT",
            "A cone with equal diameters is a cylinder",
            field="/top_diameter",
            hint='Use shape "cylinder"',
        )
    if x_direction is not None and shape != "block":
        fail("NX_INVALID_ARGUMENT", "x_direction orients a block only", field="/x_direction")
    if centered and shape == "sphere":
        fail(
            "NX_INVALID_ARGUMENT",
            "A sphere's origin is always its centre; omit centered",
            field="/centered",
        )


def _frame(tx, direction, x_direction) -> tuple[list[float], list[float], list[float]]:
    """``(x, y, z)`` unit axes: z along ``direction`` (default absolute Z), x along
    ``x_direction`` projected square to z (default the absolute X, or Y when z is X)."""
    z = [0.0, 0.0, 1.0]
    if direction is not None:
        z = geom.unit(select.direction(tx, common.principal(direction), "/direction"), "/direction")
    if x_direction is not None:
        x = geom.unit(x_direction, "/x_direction")
        field = "/x_direction"
    else:
        x = [1.0, 0.0, 0.0] if abs(z[0]) < 0.9 else [0.0, 1.0, 0.0]
        field = "/direction"
    x = geom.sub(x, geom.scale(z, geom.dot(x, z)))
    if geom.norm(x) < 1e-6:
        fail("NX_INVALID_ARGUMENT", "x_direction must not be parallel to direction", field=field)
    x = geom.unit(x, field)
    return x, geom.cross(z, x), z


def _axis(tx, base, z):
    """A smart axis through ``base`` along ``z`` (cylinder and cone builders)."""
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    return tx.part.Axes.CreateAxis(
        builders.point(tx, base), builders.direction(tx, z, base, "/direction"), update
    )


def _expected(shape: str, sizes: dict, base, x, y, z) -> tuple[float, list[float]]:
    """Closed-form ``(volume, centroid)`` of the primitive placed at ``base`` (block:
    corner; cylinder and cone: base centre; sphere: centre)."""
    if shape == "block":
        length, width, height = (float(sizes[n]) for n in ("length", "width", "height"))
        centre = base
        for axis, size in ((x, length), (y, width), (z, height)):
            centre = geom.add(centre, geom.scale(axis, size / 2))
        return length * width * height, centre
    if shape == "sphere":
        d = float(sizes["diameter"])
        return math.pi * d**3 / 6, list(base)
    h = float(sizes["height"])
    big = float(sizes["diameter"]) / 2
    small = big if shape == "cylinder" else float(sizes.get("top_diameter") or 0.0) / 2
    sum_r = big * big + big * small + small * small
    volume = math.pi * h * sum_r / 3
    rise = h * (big * big + 2 * big * small + 3 * small * small) / (4 * sum_r)
    return volume, geom.add(base, geom.scale(z, rise))


def _build(tx, shape: str, sizes: dict, base, x, y, z, combine):
    features = tx.nx.Features
    if shape == "block":
        factory, null = tx.part.Features.CreateBlockFeatureBuilder, features.Feature.Null
    elif shape == "cylinder":
        factory, null = tx.part.Features.CreateCylinderBuilder, features.Feature.Null
    elif shape == "cone":  # NX 2406 wants the typed null (a Feature null is refused)
        factory, null = tx.part.Features.CreateConeBuilder, features.Cone.Null
    else:
        factory, null = tx.part.Features.CreateSphereBuilder, features.Sphere.Null
    with builders.build(tx, factory, null) as b:
        if shape == "block":
            b.Type = features.BlockFeatureBuilder.Types.OriginAndEdgeLengths
            b.SetOrientation(tx.nx.Vector3d(*x), tx.nx.Vector3d(*y))
            b.SetOriginAndLengths(
                tx.nx.Point3d(*(float(v) for v in base)),
                *(expr.rhs(sizes[n]) for n in ("length", "width", "height")),
            )
        elif shape == "sphere":
            b.Type = features.SphereBuilder.Types.CenterPointAndDiameter
            b.CenterPoint = builders.point(tx, base)
            expr.drive(b.Diameter, sizes["diameter"])
        elif shape == "cylinder":
            b.Type = features.CylinderBuilder.Types.AxisDiameterAndHeight
            expr.drive(b.Diameter, sizes["diameter"])
            expr.drive(b.Height, sizes["height"])
            b.Axis = _axis(tx, base, z)
        else:
            b.Type = features.ConeBuilder.Types.DiametersAndHeight
            expr.drive(b.BaseDiameter, sizes["diameter"])
            expr.drive(b.TopDiameter, sizes.get("top_diameter") or 0.0)
            expr.drive(b.Height, sizes["height"])
            b.Axis = _axis(tx, base, z)
        builders.boolean(tx, b.BooleanOption, combine)
        return b.commit()


@tool(
    effect="model",
    label="Create primitive",
    summary=_summary,
    keywords=("block", "box", "cuboid", "primitive", "plate", "cylinder", "cone", "sphere"),
    requires=("work_part", "no_active_sketch"),
    variants="shape",
    cost="heavy",
    previewable=True,
    example={"shape": "block", "length": 40, "width": 20, "height": "thickness"},
)
def nx_create_primitive(
    tx: Tx,
    *,
    shape: Shape = "block",
    origin: Annotated[
        Point3, Doc("block: corner; cylinder, cone: base centre; sphere: centre; default 0")
    ]
    | None = None,
    length: Annotated[Length, Doc("block, along x_direction")] | None = None,
    width: Annotated[Length, Doc("block")] | None = None,
    height: Annotated[Length, Doc("block, cylinder, cone")] | None = None,
    diameter: Annotated[Length, Doc("cylinder, sphere, cone base")] | None = None,
    top_diameter: Annotated[Length, Doc("cone; omit: apex")] | None = None,
    direction: Annotated[common.DirectionArg, Doc("height axis; default z")] | None = None,
    x_direction: Annotated[Vec3, Doc("block length axis; default x")] | None = None,
    centered: Annotated[bool, Doc("origin is the centre")] = False,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Create a block, cylinder, cone or sphere solid as a new body or combined with bodies.
    Sizes take numbers or expression names; volume is the result bodies' total."""
    sizes = {
        "length": length, "width": width, "height": height,
        "diameter": diameter, "top_diameter": top_diameter,
    }  # fmt: skip
    _check(shape, sizes, x_direction, centered)
    common.check_combine(combine)
    x, y, z = _frame(tx, direction, x_direction)
    base = [0.0, 0.0, 0.0] if origin is None else [float(v) for v in origin]
    if centered:  # the centre given: step back to the corner or the base centre
        if shape == "block":
            for axis, size in ((x, length), (y, width), (z, height)):
                base = geom.sub(base, geom.scale(axis, float(size or 0.0) / 2))
        else:
            base = geom.sub(base, geom.scale(z, float(height or 0.0) / 2))
    targets = common.combine_targets(tx, combine)
    before = measure.volume(tx, targets) if targets else 0.0
    tags = common.body_tags(tx)
    feature = _build(tx, shape, sizes, base, x, y, z, combine)
    if combine is not None:
        return common.combined(tx, feature, combine, targets, before, tags)
    out = common.result_of(tx, feature)
    volume, centroid = _expected(shape, sizes, base, x, y, z)
    readback.expect_close(out["volume"], volume, "volume", tol=VOLUME_REL * volume)
    bodies = common.feature_bodies(feature)
    got = measure.mass(tx, bodies)["centroid"]
    if got is not None:
        span = max(float(v) for v in sizes.values() if v is not None)
        readback.expect_close(got, centroid, "centroid", tol=VOLUME_REL * span)
    return out
