"""Add the surroundings of a topology study: a g-load, scenery bodies, connections.

NX 2406 (probed live):

- ``Study.CreateEnvironmentLoadBuilder()`` edits the study's one environment load
  (gravity, an ``Acceleration``): ``GravityEnabled``, ``GravityDirection`` (the way the
  inertia load acts) and ``GravityMagnitude`` in m/s^2. It acts in every load case
  (``Subcase.AddReference`` ignores it); disabling it removes the ``Acceleration``.
- ``CreateSceneryBodyBuilder``: ``Bodies.Add`` + ``MaterialDefinition`` InheritedFromBody;
  the commit returns the ``SceneryBody`` (an analysis body: it carries load, it is not
  optimized). Supports and loads may then sit on its faces.
- ``CreateConnectionBuilder``: ``Type`` BodyToBody, ``SelectBody1/2.Value``; the commit
  returns None, ``GetAllConnections`` lists the bond.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Direction, Doc, FeatureRef, Items, Range
from nx_mcp.domains.optimization import common
from nx_mcp.kit import builders, readback, result, select

Kind = Literal["acceleration", "scenery", "connection"]
G_REL = 1e-6


class Acceleration(TypedDict):
    g: float
    direction: list[float]


class EnvironmentData(TypedDict):
    study: result.Row
    kind: str
    acceleration: Acceleration | None
    scenery: list[result.Row]
    connections: int


def _check(kind, bodies, magnitude, direction):
    """Cross-field rules the schema cannot say, before NX is touched."""
    if kind == "acceleration":
        if bodies is not None:
            fail("NX_INVALID_ARGUMENT", "acceleration takes no bodies", field="/bodies")
        if magnitude and direction is None:
            fail("NX_INVALID_ARGUMENT", "acceleration needs direction", field="/direction")
        return
    for field, value in (("magnitude", magnitude), ("direction", direction)):
        if value is not None:
            fail("NX_INVALID_ARGUMENT", f"{kind} takes no {field}", field="/" + field)
    if kind == "connection" and len(bodies or []) != 2:
        fail("NX_INVALID_ARGUMENT", "connection takes exactly 2 bodies", field="/bodies")


def acceleration_of(tx, study) -> Acceleration | None:
    """The study's g-load: magnitude in g and the unit direction it acts in."""
    for load in common.all_of(study, "GetAllEnvironmentLoads"):
        magnitude = getattr(load, "Magnitude", None)
        if magnitude is None:
            continue
        value = common.value_of(magnitude, "acceleration")
        vector = getattr(getattr(load, "Direction", None), "Vector", None)
        if value is None or vector is None:
            continue
        xyz = [float(vector.X), float(vector.Y), float(vector.Z)]
        size = math.sqrt(sum(v * v for v in xyz)) or 1.0
        return {"g": value / common.G_MM, "direction": [v / size for v in xyz]}
    return None


def _accelerate(tx, study, g, vector):
    with builders.build(tx, study.CreateEnvironmentLoadBuilder) as b:
        b.GravityEnabled = bool(g)
        if g:
            b.GravityDirection = builders.direction(tx, vector, field="/direction")
            common.drive(tx, b.GravityMagnitude, g * common.G_MM, "acceleration")
        b.commit()


def _scenery(tx, study, body):
    DS = common.ds(tx)
    with builders.build(tx, study.CreateSceneryBodyBuilder, DS.SceneryBody.Null) as b:
        b.Bodies.Add(body)
        b.MaterialDefinition = DS.SceneryBodyBuilder.MaterialDefinitionType.InheritedFromBody
        b.commit()


def _connect(tx, study, first, second):
    DS = common.ds(tx)
    with builders.build(tx, study.CreateConnectionBuilder, DS.Connection.Null) as b:
        b.Type = DS.ConnectionBuilder.Types.BodyToBody
        b.SelectBody1.Value = first
        b.SelectBody2.Value = second
        b.commit()


def _bodies(tx, kind, refs, design, scenery):
    """The bodies for scenery (new, with a material) or a connection (study bodies)."""
    found = select.bodies(tx, refs, "/bodies")
    taken = {int(design.Tag)} | {int(b.Tag) for b in scenery}
    seen: set[int] = set()
    for i, body in enumerate(found):
        field, tag = f"/bodies/{i}", int(body.Tag)
        if tag in seen:
            fail("NX_INVALID_ARGUMENT", "Pass each body once", field=field)
        seen.add(tag)
        if kind == "connection" and tag not in taken:
            fail("NX_INVALID_ARGUMENT", "Connect the design space and scenery bodies only",
                 field=field, hint="Add the body first with kind 'scenery'")  # fmt: skip
        if kind == "scenery" and tag in taken:
            fail("NX_INVALID_ARGUMENT", "The body is already in the study", field=field)
        if kind == "scenery" and not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "A scenery body must be a solid", field=field)
        if kind == "scenery":
            try:
                own = tx.part.MaterialManager.PhysicalMaterials.AskMaterialOfObject(body)
            except Exception:  # NX raises for a body without a material
                own = None
            if own is None:
                fail("NX_INVALID_ARGUMENT", "The scenery body has no material", field=field,
                     hint="Assign one with nx_set_material first")  # fmt: skip
    return found


@tool(
    effect="model",
    label="Add topology environment",
    summary={
        "acceleration": "{magnitude} g acceleration on {study}",
        "scenery": "Scenery {bodies} in {study}",
        "connection": "Connect {bodies} in {study}",
    },
    keywords=(
        "gravity",
        "acceleration",
        "g-load",
        "slamming",
        "scenery body",
        "non-design body",
        "bonded connection",
        "assembly load path",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={
        "acceleration": ("magnitude",),
        "scenery": ("bodies",),
        "connection": ("bodies",),
    },  # fmt: skip
    cost="heavy",
    display_bound=True,
    example={"study": "f:12", "kind": "acceleration", "magnitude": 5, "direction": "-z"},
)
def nx_create_topology_environment(
    tx: Tx,
    *,
    study: FeatureRef,
    kind: Kind,
    bodies: Annotated[list[BodyRef], Items(1, 20), Doc("scenery: solids to add; connection: 2")]
    | None = None,
    magnitude: Annotated[
        float, Range(ge=0, le=1e4), Doc("g (9.80665 m/s^2); 1 = gravity; 0 removes")
    ]
    | None = None,
    direction: Annotated[Direction, Doc("Way the inertia load acts; gravity '-z'")] | None = None,
) -> EnvironmentData:
    """Add a g-load, scenery bodies or a bonded connection to a topology study.
    acceleration: inertia load on every body in every load case; replaces the last one.
    scenery: solids that carry load but are not optimized (own material; supports and loads
    may sit on their faces). connection: bonds 2 touching study bodies (design space or
    scenery)."""
    _check(kind, bodies, magnitude, direction)
    with tx.checks():
        feature = common.feature_of(tx, study)
        found = common.active_study(feature)
        designs = common.all_of(found, "GetAllDesignSpaces")
        if not designs:
            fail("NX_NOT_FOUND", "The study has no design space", field="/study")
        scenery = common.scenery_bodies(found)
        picked = _bodies(tx, kind, bodies, designs[0].Body, scenery) if bodies else []
        vector = select.direction(tx, direction, "/direction") if direction is not None else None
        common.require_license(tx)
        before = len(scenery), len(common.all_of(found, "GetAllConnections"))
    with common.task(tx, feature) as active:
        current = common.active_study(active)
        if kind == "acceleration":
            _accelerate(tx, current, float(magnitude or 0.0), vector)
        elif kind == "scenery":
            for body in picked:
                _scenery(tx, current, body)
        else:
            _connect(tx, current, picked[0], picked[1])
        with native():
            got = acceleration_of(tx, current)
            scenery = common.scenery_bodies(current)
            connections = len(common.all_of(current, "GetAllConnections"))
    if kind == "acceleration":
        if magnitude:
            readback.expect(got is not None, "acceleration", "NX made no acceleration")
            assert got is not None
            readback.expect_close(got["g"], float(magnitude), "g", tol=G_REL * float(magnitude))
        else:
            readback.expect(got is None, "acceleration", "NX kept the acceleration")
    want = (before[0] + len(picked) * (kind == "scenery"), before[1] + (kind == "connection"))
    readback.expect((len(scenery), connections) == want, "count",
                    scenery=len(scenery), connections=connections)  # fmt: skip
    return {
        "study": tx.ref(feature, kind="feature"),
        "kind": kind,
        "acceleration": got,
        "scenery": result.rows(tx, scenery, "body"),
        "connections": connections,
    }
