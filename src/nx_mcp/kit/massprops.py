"""Weight and centre-of-gravity bookkeeping shared by the inspection report and the
assembly weight tools (no NX import).

    e = massprops.entry(mass, center, inertia)      # one mass item (inertia about its CoG)
    t = massprops.combine([e1, e2, ...])             # total mass, CoG, inertia about the CoG
    f = massprops.frame(origin, x_axis, y_axis)      # a reference frame (LCG, TCG, VCG)
    massprops.in_frame(f, point)                     # coordinates along its axes
    massprops.inertia_about(t, f)                    # inertia about its origin, its axes
    massprops.to_assembly(pose, p) / to_local(pose, p)
    massprops.asserted(tx, component_or_part)        # NX weight-management assertion
    massprops.assert_weight(tx, obj, level, mass, center_mm) / clear_weight(...)
    massprops.groups(part)                           # NX component groups (Advanced Assemblies)

Masses are kg; lengths are whatever the caller passes (part units); inertia is
kg * length^2 about the item's own centre of gravity in absolute axes.

NX 2406 findings (live, 2026-10-12):

- UF ``Weight.AskProps`` (no license) reads an occurrence's or a part's cached or
  asserted weight data without changing anything. ``Weight.Estab*Props`` computes
  and *caches* weight data in the part (the part becomes modified), so reports measure
  with ``AskMassProps3d`` instead and only read assertions here.
- ``AskProps`` of a component reports an asserted centre of gravity in the
  component's part frame (millimetres for ``UNITS_KMM``); ``AssertCompProps`` takes it
  in the assembly frame. ``AssertPartProps``/``AskProps`` of a part use the part frame.
  A part assertion is not visible through ``AskProps`` of its components.
- An assertion needs ``Accuracy`` < 1 (0.99 here; 1.0 is "an invalid accuracy value")
  and ``CacheState`` ``ASSERTED``; unasserted values are ``UNKNOWN``. Asserting and
  deleting assertions need ``adv_assemblies``; the session undo mark rolls both back.
- ``Part.ComponentGroups`` iterates four system groups (``AllComponents``,
  ``LoadedChangedComponents``, ``UnloadedChangedComponents``, ``CurrentComponents``)
  before the user's; ``GetComponents`` of a system group includes the root component.
  ``CreateComponentGroup`` refuses a name in use ("Component set with given name
  already exists in part") and needs ``adv_assemblies``.
"""

from __future__ import annotations

import contextlib
import math

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import geom, licenses

ADV_ASSEMBLIES = "adv_assemblies"
SYSTEM_GROUPS = frozenset(
    {"allcomponents", "loadedchangedcomponents", "unloadedchangedcomponents", "currentcomponents"}
)
ASSERT_ACCURACY = 0.99
ZERO3 = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]


# ---------------------------------------------------------------------------
# Pure mass math


def entry(mass, center, inertia=None) -> dict:
    """One mass item: ``mass`` kg at ``center``; ``inertia`` about ``center`` (None: a
    point mass)."""
    return {
        "mass": float(mass),
        "center": [float(v) for v in center],
        "inertia": [row[:] for row in inertia] if inertia is not None else None,
    }


def shift_term(mass, d) -> list:
    """Parallel-axis term ``m (|d|^2 E - d d^T)``."""
    d2 = geom.dot(d, d)
    return [[mass * ((d2 if i == j else 0.0) - d[i] * d[j]) for j in range(3)] for i in range(3)]


def _add(a, b):
    return [[a[i][j] + b[i][j] for j in range(3)] for i in range(3)]


def combine(entries) -> dict:
    """``{"mass", "center", "inertia"}`` of several items: the CoG is the mass-weighted
    mean (None when the total mass is not positive) and the inertia the sum of each
    item's own inertia moved to the total CoG (parallel axes)."""
    entries = list(entries)
    mass = sum(e["mass"] for e in entries)
    if not entries or mass <= 0:
        return {"mass": mass, "center": None, "inertia": None}
    center = [sum(e["mass"] * e["center"][i] for e in entries) / mass for i in range(3)]
    tensor = [row[:] for row in ZERO3]
    for e in entries:
        own = e.get("inertia") or ZERO3
        tensor = _add(tensor, _add(own, shift_term(e["mass"], geom.sub(e["center"], center))))
    return {"mass": mass, "center": center, "inertia": tensor}


def frame(origin, x_axis=None, y_axis=None, field="/reference") -> dict:
    """A right-handed reference frame: ``x_axis`` (default +X), ``y_axis`` made
    orthogonal to it (default +Y), z = x cross y. ``{"origin", "axes"}`` (axes as rows)."""
    x = geom.unit(list(x_axis) if x_axis is not None else [1.0, 0.0, 0.0], field + "/x_axis")
    y = list(y_axis) if y_axis is not None else [0.0, 1.0, 0.0]
    y = geom.sub(y, geom.scale(x, geom.dot(x, y)))
    if geom.norm(y) <= 1e-9:
        fail(
            "NX_INVALID_ARGUMENT", "y_axis must not be parallel to x_axis", field=field + "/y_axis"
        )
    y = geom.unit(y, field + "/y_axis")
    return {"origin": [float(v) for v in origin], "axes": [x, y, geom.cross(x, y)]}


def in_frame(ref, point) -> list:
    """``point`` along the frame's axes, from its origin."""
    d = geom.sub(point, ref["origin"])
    return [geom.dot(axis, d) for axis in ref["axes"]]


def direction_in_frame(ref, vector) -> list:
    """A vector (a shift) along the frame's axes."""
    return [geom.dot(axis, vector) for axis in ref["axes"]]


def inertia_about(total, ref) -> list:
    """Inertia of ``total`` (``combine``) about the frame's origin, in the frame's axes."""
    moved = _add(
        total["inertia"], shift_term(total["mass"], geom.sub(total["center"], ref["origin"]))
    )
    axes = ref["axes"]  # rows: frame axes in absolute coordinates
    return geom.matmul(geom.matmul(axes, moved), geom.transpose(axes))


def principal_moments(tensor) -> list:
    """Eigenvalues of a symmetric 3x3 inertia tensor, ascending (closed form)."""
    a = [[float(v) for v in row] for row in tensor]
    off = a[0][1] ** 2 + a[0][2] ** 2 + a[1][2] ** 2
    q = (a[0][0] + a[1][1] + a[2][2]) / 3.0
    scale = max(abs(v) for row in a for v in row) or 1.0
    if off <= (1e-15 * scale) ** 2:
        return sorted([a[0][0], a[1][1], a[2][2]])
    p = math.sqrt((sum((a[i][i] - q) ** 2 for i in range(3)) + 2.0 * off) / 6.0)
    b = [[(a[i][j] - (q if i == j else 0.0)) / p for j in range(3)] for i in range(3)]
    det = (
        b[0][0] * (b[1][1] * b[2][2] - b[1][2] * b[2][1])
        - b[0][1] * (b[1][0] * b[2][2] - b[1][2] * b[2][0])
        + b[0][2] * (b[1][0] * b[2][1] - b[1][1] * b[2][0])
    )
    phi = math.acos(max(-1.0, min(1.0, det / 2.0))) / 3.0
    big = q + 2.0 * p * math.cos(phi)
    small = q + 2.0 * p * math.cos(phi + 2.0 * math.pi / 3.0)
    return sorted([big, 3.0 * q - big - small, small])


def rotate_inertia(inertia, r) -> list:
    """``R I R^T``: a tensor in local axes turned to absolute axes."""
    return geom.matmul(geom.matmul(r, inertia), geom.transpose(r))


def pose_of(component):
    """``(t, R)`` of an occurrence in assembly coordinates (``p = R p_local + t``)."""
    with native():
        position, matrix = component.GetPosition()
    return geom.xyz(position), [list(map(float, row)) for row in geom.rows(matrix)]


def to_assembly(pose, point) -> list:
    """A component-local point in assembly coordinates (``pose = (t, R)``)."""
    t, r = pose
    return geom.add(geom.matvec(r, point), t)


def to_local(pose, point) -> list:
    """An assembly point in the component's local coordinates."""
    t, r = pose
    return geom.matvec(geom.transpose(r), geom.sub(point, t))


# ---------------------------------------------------------------------------
# Licenses


def require_license(tx, feature=ADV_ASSEMBLIES, what="This tool") -> None:
    """``NX_LICENSE_UNAVAILABLE`` before any change when ``feature`` is not served
    (``kit.licenses``; NX cannot tell: NX decides)."""
    licenses.require(
        tx,
        (feature,),
        f"{what} needs the NX license feature {feature} (Advanced Assemblies)",
        hint="Tell the engineer the license is missing",
    )


# ---------------------------------------------------------------------------
# NX weight management (UF Weight)


def _code(value) -> int:
    value = getattr(value, "value", value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(str(value))


def _weight(tx):
    uf = getattr(getattr(tx, "uf", None), "Weight", None)
    enums = getattr(getattr(getattr(tx, "nx", None), "UF", None), "Weight", None)
    return uf, enums


def asserted(tx, obj) -> dict | None:
    """The NX-asserted weight of a component or part: ``{"mass", "center"}`` (kg; centre
    in the object's own part frame, millimetres, None when not asserted), or None when
    neither mass nor centre is asserted (or UF Weight is not available)."""
    uf, enums = _weight(tx)
    if uf is None or enums is None:
        return None
    try:
        props = uf.AskProps(obj.Tag, enums.UnitsType.UNITS_KMM)
    except Exception:
        return None
    flag = _code(enums.StateType.ASSERTED)
    mass_set = _code(props.MassState) == flag
    center_set = _code(props.CofmState) == flag
    if not (mass_set or center_set):
        return None
    return {
        "mass": float(props.Mass) if mass_set else None,
        "center": [float(v) for v in props.CenterOfMass] if center_set else None,
    }


def assert_weight(tx, obj, level, mass, center_mm=None) -> None:
    """Assert ``mass`` (kg) and optionally the centre of gravity (millimetres; the
    assembly frame for a component, the part frame for a part) with UF Weight."""
    uf, enums = _weight(tx)
    if uf is None or enums is None:
        fail("NX_VERSION_UNSUPPORTED", "This NX has no UF Weight (assembly weight management)")
    states = enums.StateType
    with native(default="NX_BUILD_FAILED"):
        props = uf.AskProps(obj.Tag, enums.UnitsType.UNITS_KMM)
        props.Units = enums.UnitsType.UNITS_KMM
        props.Accuracy = ASSERT_ACCURACY
        props.CacheState = states.ASSERTED
        props.Mass = float(mass)
        props.MassState = states.ASSERTED
        if center_mm is not None:
            props.CenterOfMass = [float(v) for v in center_mm]
            props.CofmState = states.ASSERTED
        else:
            props.CofmState = states.UNKNOWN
        for name in ("DensityState", "VolumeState", "AreaState", "MofiState"):
            setattr(props, name, states.UNKNOWN)
        if level == "part":
            uf.AssertPartProps(obj.Tag, props)
        else:
            uf.AssertCompProps(obj.Tag, props)


def clear_weight(tx, obj, level) -> None:
    """Delete the NX weight assertion of a component or a part."""
    uf, _enums = _weight(tx)
    if uf is None:
        fail("NX_VERSION_UNSUPPORTED", "This NX has no UF Weight (assembly weight management)")
    with native(default="NX_BUILD_FAILED"):
        if level == "part":
            uf.DeletePartAssertion(obj.Tag)
        else:
            uf.DeleteCompAssertion(obj.Tag)


# ---------------------------------------------------------------------------
# Materials and component groups


def material_name(body) -> str | None:
    """The physical material of a body (an occurrence reports its prototype's), or None
    when it has none (NX then uses its default solid density)."""
    occurrence = getattr(body, "IsOccurrence", False)
    prototype = getattr(body, "Prototype", None) if occurrence else body
    if prototype is None:
        return None
    try:
        materials = prototype.OwningPart.MaterialManager.PhysicalMaterials
        material = materials.AskMaterialOfObject(prototype)
    except Exception:
        return None
    if material is None:
        return None
    with contextlib.suppress(Exception):
        return str(material.Name) or None
    return None


def is_system_group(name) -> bool:
    return str(name).casefold() in SYSTEM_GROUPS


def group_members(group) -> list:
    """Components of an NX component group, without the root component."""
    with native():
        found = list(group.GetComponents() or [])
    return [c for c in found if c is not None and getattr(c, "Parent", None) is not None]


def groups(part, include_system=False) -> list:
    """``[(group, [components])]`` of the part's NX component groups, user groups only
    unless ``include_system``."""
    collection = getattr(part, "ComponentGroups", None)
    if collection is None:
        return []
    out = []
    with native():
        found = list(collection)
    for group in found:
        if not include_system and is_system_group(group.Name):
            continue
        out.append((group, group_members(group)))
    return out


def find_group(part, name):
    """The part's component group named ``name`` (case-insensitive), or None."""
    for group, _members in groups(part, include_system=True):
        if str(group.Name).casefold() == str(name).casefold():
            return group
    return None
