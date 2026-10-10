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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

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
    directory, ``save`` may only be the target part's own file, ``replace`` is new
    or an existing file of the allowed suffixes (never a directory) that the tool
    replaces only when asked (``Files.stage(..., replace=True)``)."""

    mode: Literal["read", "new", "dir", "save", "replace"]
    suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Json:
    """A free-form JSON object: the arguments of another tool (``nx_run_batch``),
    checked against that tool's own schema later. Bounded by key count and size."""

    max_keys: int = 40
    max_chars: int = 16000


@dataclass(frozen=True, eq=False)
class ByVariant:
    """On a ``Json`` object: its fields depend on the tool's variant field ``field``;
    ``forms`` maps each variant value to its TypedDict. Gate 1 checks the object against
    the chosen form and ``nx_get_tool_schemas("tool#value")`` shows only that form, so
    the listed schema stays small however many variants the tool has."""

    field: str
    forms: Mapping[str, Any]


def check_json(value, marker: Json) -> dict:
    """``value`` as a bounded JSON object, else ValueError (both gate-1 validators)."""
    import json as _json

    if not isinstance(value, dict):
        raise ValueError("expected an object")
    if len(value) > marker.max_keys:
        raise ValueError(f"at most {marker.max_keys} keys")
    try:
        text = _json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"must be finite JSON ({error})") from None
    if len(text) > marker.max_chars:
        raise ValueError(f"at most {marker.max_chars} characters of JSON")
    return value


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

# Short refs ("b:41") and object registry handles ("obj_...", see core.references).
REF_PATTERN = r"^([a-z]{1,3}:[1-9][0-9]{0,11}|obj_[A-Za-z0-9_]{8,120})$"
# Batch children may refer to an earlier step's result: "$2.data.feature".
PLACEHOLDER_PATTERN = r"^\$[0-9]{1,3}(\.[A-Za-z0-9_]{1,64}){1,8}$"


def expand_kinds(kinds) -> frozenset[str]:
    """Exact kinds named by ``kinds``, with groups such as ``datum`` expanded."""
    out: set[str] = set()
    for kind in kinds:
        out |= KIND_GROUPS.get(kind, {kind})
    return frozenset(out)


@dataclass(frozen=True, init=False)
class RefTo:
    """``Annotated[str, RefTo(*kinds)]``: a reference to one of ``kinds`` (any kind when
    empty). Shorthand for ``Text(128, REF_PATTERN)``, ``RefKind(*kinds)`` and a ``Doc``
    (``doc``, else "Ref: <kinds>"); ``core.validate.unwrap`` expands it in place. Static
    checkers see the field as ``str``, so aliases such as ``BodyRef`` narrow and compare."""

    kinds: tuple[str, ...]
    doc: str | None

    def __init__(self, *kinds: str, doc: str | None = None) -> None:
        object.__setattr__(self, "kinds", tuple(kinds))
        object.__setattr__(self, "doc", doc)

    def expand(self) -> tuple[Text, RefKind, Doc]:
        what = "/".join(self.kinds) if self.kinds else "any"
        return Text(128, REF_PATTERN), RefKind(*self.kinds), Doc(self.doc or f"Ref: {what}")


Ref = Annotated[str, RefTo()]
PartRef = Annotated[str, RefTo("part")]
BodyRef = Annotated[str, RefTo("body")]
FaceRef = Annotated[str, RefTo("face")]
EdgeRef = Annotated[str, RefTo("edge")]
CurveRef = Annotated[str, RefTo("curve")]
FeatureRef = Annotated[str, RefTo("feature")]
SketchRef = Annotated[str, RefTo("sketch")]
ComponentRef = Annotated[str, RefTo("component")]
ExplosionRef = Annotated[str, RefTo("explosion")]
DimensionRef = Annotated[str, RefTo("dimension")]
DrawingSheetRef = Annotated[str, RefTo("drawing_sheet")]
DrawingViewRef = Annotated[str, RefTo("drawing_view")]
DrawingTableRef = Annotated[str, RefTo("drawing_table")]

# ---------------------------------------------------------------------------
# Geometry

Point2 = Annotated[tuple[float, float], Doc("[x, y] sketch-local, part units")]
SheetPoint = Annotated[tuple[float, float], Doc("[x, y] on the sheet, sheet units")]
Point3 = Annotated[tuple[float, float, float], Doc("[x, y, z] absolute, part units")]
Vec3 = Annotated[tuple[float, float, float], Doc("Direction [x, y, z]")]
Mat3 = Annotated[
    tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    Doc("Rotation matrix, row-major"),
]

Length = Annotated[float | str, Quantity("length"), Doc("Part units, or '10 mm'/'0.5 in'")]
SheetLength = Annotated[float | str, Quantity("sheet_length"), Doc("Sheet units, or '10 mm'")]
Angle = Annotated[float | str, Quantity("angle"), Doc("Degrees, or '0.5 rad'")]
Units = Literal["mm", "in"]
# Hints in the target drawing sheet's units: a tool using one of them gets the
# sheet resolved from its arguments and ``tx.units["sheet_length"]`` (gate 9).
SHEET_HINTS = (SheetPoint, SheetLength)
# Reference kinds the kernel can map to the drawing sheet that shows them (annotations,
# dimensions and tables through the drawing domain's ``@sheet_locator`` hooks).
SHEET_KINDS = frozenset(
    {"drawing_sheet", "drawing_view", "annotation", "dimension", "drawing_table"}
)
Name = Annotated[str, Text(64, r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]{0,63}$"), Doc("Object name")]
Offset = Annotated[int, Range(ge=0, le=1_000_000), Doc("First row to return")]
PageLimit = Annotated[int, Range(ge=1, le=200), Doc("Rows per page")]


@dataclass(frozen=True, init=False)
class PathTo:
    """``Annotated[str, PathTo(mode, *suffixes)]``: a workspace path argument with ``mode``
    and optional allowed suffixes. Shorthand for ``Text(260)``, ``PathArg`` and a ``Doc``;
    ``core.validate.unwrap`` expands it in place."""

    mode: Literal["read", "new", "dir", "save", "replace"]
    suffixes: tuple[str, ...]

    def __init__(
        self, mode: Literal["read", "new", "dir", "save", "replace"], *suffixes: str
    ) -> None:
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "suffixes", tuple(s.lower() for s in suffixes))

    def expand(self) -> tuple[Text, PathArg, Doc]:
        return Text(260), PathArg(self.mode, self.suffixes), Doc("Workspace path")


# Markers that stand for several others (``core.validate.unwrap`` expands them).
SHORTHANDS = (RefTo, PathTo)

PathIn = Annotated[str, PathTo("read")]
# Another tool's arguments (batch and preview children), checked by that tool's schema.
Arguments = Annotated[dict, Json(), Doc("The tool's own arguments")]


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


Plane = Annotated[str, RefTo("datum_plane", "face")] | PrincipalPlane | PlaneByNormal
Axis = Annotated[str, RefTo("datum_axis", "edge", "face")] | PrincipalAxis | AxisByPoint
Direction = Vec3 | Annotated[str, RefTo("edge", "datum_axis", "face")] | PrincipalAxis


class SketchPoint(TypedDict):
    curve: CurveRef
    point: Literal["start", "end", "center", "mid"]


class LimitDistance(TypedDict):
    type: Literal["distance"]
    value: Length


class LimitThroughAll(TypedDict):
    type: Literal["through_all"]


class LimitUntilFace(TypedDict):
    type: Literal["until_face"]
    face: FaceRef


Limit = LimitDistance | LimitThroughAll | LimitUntilFace


class Combine(TypedDict):
    operation: Literal["unite", "subtract", "intersect"]
    targets: Annotated[
        list[BodyRef],
        Items(1, 20),
        Doc("Target bodies; unite takes exactly one (NX never merges several targets)"),
    ]


class _PoseRequired(TypedDict):
    translation: Point3


class Pose(_PoseRequired, total=False):
    rotation: Mat3


def forbid_extra(*typeddicts: Any) -> None:
    """Forbid extra fields on these TypedDict arguments: the sidecar's Pydantic adapters
    read ``__pydantic_config__`` (an attribute static checkers do not know on a class)."""
    for td in typeddicts:
        td.__pydantic_config__ = {"extra": "forbid"}


# Extra fields are forbidden on every TypedDict argument (read by the sidecar).
forbid_extra(
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
)

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
# Longest accepted unit string ("  -1.25e+3 mm "): gate 1 and the listings bound it.
QUANTITY_MAX = 40
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
    if (
        not isinstance(value, str)
        or len(value) > QUANTITY_MAX
        or not re.match(QUANTITY_PATTERNS[dim], value)
    ):
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
