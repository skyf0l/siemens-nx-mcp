"""Argument types shared by every tool: stdlib ``Annotated`` aliases and TypedDicts.

The markers below carry the schema facts. ``core.validate`` checks them on the NX
side; the sidecar derives Pydantic adapters and JSON Schema from the same
metadata (architecture.md 1.1). Nothing here imports pydantic or NXOpen.

Unit rule (tool-surface decision 12): a plain number is in part units for
``Length``, sheet units for ``SheetLength`` and degrees for ``Angle``; a string
``"<number> <unit>"`` is converted explicitly by the kernel (gate 9). Points,
vectors and matrices take plain numbers only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

# ---------------------------------------------------------------------------
# Markers


@dataclass(frozen=True)
class Doc:
    """Field description, at most 80 characters."""

    text: str


@dataclass(frozen=True)
class Range:
    """Finite numeric bounds."""

    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None


@dataclass(frozen=True)
class Items:
    """List length bounds; required on every list."""

    min: int
    max: int


@dataclass(frozen=True)
class Text:
    """String bounds."""

    max: int
    pattern: str | None = None


@dataclass(frozen=True, init=False)
class RefKind:
    """Accepted reference kinds; empty means any kind."""

    kinds: tuple[str, ...]

    def __init__(self, *kinds: str) -> None:
        object.__setattr__(self, "kinds", tuple(kinds))


@dataclass(frozen=True)
class Quantity:
    """A length, sheet length or angle that also accepts a unit string."""

    dim: Literal["length", "sheet_length", "angle"]


@dataclass(frozen=True)
class PathArg:
    """Workspace path. ``read`` must exist, ``new`` must not, ``dir`` is a
    directory, ``save`` may only be the target part's own file."""

    mode: Literal["read", "new", "dir", "save"]
    suffixes: tuple[str, ...] = ()


MARKERS = (Doc, Range, Items, Text, RefKind, Quantity, PathArg)

# ---------------------------------------------------------------------------
# References

# Exact kinds stored by the reference registry. "datum" is a group name that
# RefTo() accepts for any datum kind.
KINDS = frozenset(
    {
        "part",
        "body",
        "face",
        "edge",
        "feature",
        "sketch",
        "curve",
        "point",
        "datum_plane",
        "datum_axis",
        "datum_csys",
        "datum_point",
        "component",
        "expression",
        "annotation",
        "dimension",
        "drawing_sheet",
        "drawing_view",
        "drawing_table",
        "reference_set",
        "explosion",
        "traceline",
        "component_pattern",
        "assembly_constraint",
        "sketch_constraint",
        "section_view",
        "modeling_view",
    }
)
KIND_GROUPS = {"datum": frozenset({"datum_plane", "datum_axis", "datum_csys", "datum_point"})}

# Short refs ("b:41") and, during migration, legacy registry IDs ("obj_...").
REF_PATTERN = r"^([a-z]{1,3}:[1-9][0-9]{0,11}|obj_[A-Za-z0-9_]{8,120})$"
# Batch children may refer to an earlier step's result: "$2.data.feature".
PLACEHOLDER_PATTERN = r"^\$[0-9]{1,3}(\.[A-Za-z0-9_]{1,64}){1,8}$"


def expand_kinds(kinds) -> frozenset[str]:
    """Exact kinds named by ``kinds``, with groups such as ``datum`` expanded."""
    out: set[str] = set()
    for kind in kinds:
        out |= KIND_GROUPS.get(kind, {kind})
    return frozenset(out)


def RefTo(*kinds: str):  # noqa: N802 - reads as a type in signatures
    """A reference to one of ``kinds`` (any kind when empty)."""
    what = "/".join(kinds) if kinds else "object"
    return Annotated[
        str,
        Text(128, REF_PATTERN),
        RefKind(*kinds),
        Doc(f"Reference ({what}) from an earlier call"),
    ]


Ref = RefTo()

# ---------------------------------------------------------------------------
# Geometry

Point2 = Annotated[tuple[float, float], Doc("[x, y] sketch-local, part units")]
SheetPoint = Annotated[tuple[float, float], Doc("[x, y] on the sheet, sheet units")]
Point3 = Annotated[tuple[float, float, float], Doc("[x, y, z] work-part absolute, part units")]
Vec3 = Annotated[tuple[float, float, float], Doc("Nonzero direction [x, y, z]")]
Mat3 = Annotated[
    tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    Doc("Orthonormal right-handed rotation, row-major"),
]

Length = Annotated[
    float | str, Quantity("length"), Doc("Number in part units, or '10 mm' / '0.5 in'")
]
SheetLength = Annotated[
    float | str, Quantity("sheet_length"), Doc("Number in sheet units, or '10 mm'")
]
Angle = Annotated[float | str, Quantity("angle"), Doc("Number in degrees, or '0.5 rad'")]
Units = Literal["mm", "in"]
Name = Annotated[
    str, Text(64, r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]{0,63}$"), Doc("Name for the new object")
]
Offset = Annotated[int, Range(ge=0, le=1_000_000), Doc("First row to return")]
PageLimit = Annotated[int, Range(ge=1, le=200), Doc("Rows per page, at most 200")]


def PathTo(mode: str, *suffixes: str):  # noqa: N802 - reads as a type in signatures
    """A workspace path argument with ``mode`` and optional allowed suffixes."""
    return Annotated[
        str,
        Text(260),
        PathArg(mode, tuple(s.lower() for s in suffixes)),  # type: ignore[arg-type]
        Doc("Workspace-relative path (or absolute inside the workspace)"),
    ]


PathIn = PathTo("read")
PathOut = PathTo("new")


class PrincipalPlane(TypedDict):
    principal: Literal["xy", "yz", "xz"]


class PlaneByNormal(TypedDict):
    origin: Point3
    normal: Vec3


class PrincipalAxis(TypedDict):
    principal: Literal["x", "y", "z", "-x", "-y", "-z"]


class AxisByPoint(TypedDict):
    origin: Point3
    direction: Vec3


Plane = RefTo("datum_plane", "face") | PrincipalPlane | PlaneByNormal
Axis = RefTo("datum_axis", "edge", "face") | PrincipalAxis | AxisByPoint
Direction = Vec3 | RefTo("edge", "datum_axis", "face") | PrincipalAxis


class SketchPoint(TypedDict):
    curve: RefTo("curve")  # type: ignore[valid-type]
    point: Literal["start", "end", "center", "mid"]


class LimitDistance(TypedDict):
    type: Literal["distance"]
    value: Length


class LimitThroughAll(TypedDict):
    type: Literal["through_all"]


class LimitUntilFace(TypedDict):
    type: Literal["until_face"]
    face: RefTo("face")  # type: ignore[valid-type]


Limit = LimitDistance | LimitThroughAll | LimitUntilFace


class Combine(TypedDict):
    operation: Literal["unite", "subtract", "intersect"]
    targets: Annotated[list[RefTo("body")], Items(1, 20)]  # type: ignore[valid-type]


class _PoseRequired(TypedDict):
    translation: Point3


class Pose(_PoseRequired, total=False):
    rotation: Mat3


# Extra fields are forbidden on every TypedDict argument (read by the sidecar).
for _td in (
    PrincipalPlane,
    PlaneByNormal,
    PrincipalAxis,
    AxisByPoint,
    SketchPoint,
    LimitDistance,
    LimitThroughAll,
    LimitUntilFace,
    Combine,
    Pose,
):
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Unit strings

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
LENGTH_PATTERN = r"^\s*" + _NUMBER + r"\s*(mm|in)\s*$"
ANGLE_PATTERN = r"^\s*" + _NUMBER + r"\s*(deg|rad)\s*$"
QUANTITY_PATTERNS = {
    "length": LENGTH_PATTERN,
    "sheet_length": LENGTH_PATTERN,
    "angle": ANGLE_PATTERN,
}
MM_PER_IN = 25.4
_SPLIT = re.compile(r"^\s*(" + _NUMBER + r")\s*([a-z]+)\s*$")


def parse_quantity(value, dim: str) -> tuple[float, str | None]:
    """``(number, unit)`` of a quantity argument; unit is None for a plain number.

    Raises ValueError for a string that does not match the dimension's pattern.
    """
    if isinstance(value, bool):
        raise ValueError("expected a number or a unit string")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite")
        return number, None
    if not isinstance(value, str) or not re.match(QUANTITY_PATTERNS[dim], value):
        units = "deg or rad" if dim == "angle" else "mm or in"
        raise ValueError(f"expected a number or a string like '10 {units.split()[0]}' ({units})")
    match = _SPLIT.match(value)
    assert match is not None
    number = float(match.group(1))
    if not math.isfinite(number):
        raise ValueError("must be finite")
    return number, match.group(2)


def convert_quantity(value, dim: str, to: str) -> float:
    """Float value of a quantity in ``to`` (``mm``/``in`` for lengths, ``deg`` for angles).

    A plain number is already in the target units (part/sheet units, degrees).
    """
    number, unit = parse_quantity(value, dim)
    if unit is None or unit == to:
        return number
    if dim == "angle":
        if to != "deg" or unit != "rad":
            raise ValueError(f"cannot convert {unit} to {to}")
        return math.degrees(number)
    if (unit, to) == ("mm", "in"):
        return number / MM_PER_IN
    if (unit, to) == ("in", "mm"):
        return number * MM_PER_IN
    raise ValueError(f"cannot convert {unit} to {to}")


def format_quantity(value: float, unit: str | None) -> str:
    """``10 mm``: a short, exact-enough rendering of a converted quantity."""
    text = f"{float(value):.6g}"
    return f"{text} {unit}" if unit else text
