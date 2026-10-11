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
    """A length, sheet length or angle that also accepts a unit string, and any of them (or
    a ``count``) given as an NX expression name or formula (see :class:`Formula`)."""

    dim: Literal["length", "sheet_length", "angle", "count"]


@dataclass(frozen=True)
class Ratio:
    """On a number: it also takes a percent string, ``'50%'`` = ``50 * per / 100`` (a
    percent field has ``per=100``, a fraction ``per=1``), and a ``factor`` also ``'2x'``.
    Gate 1 applies the same rule, without the marker, to fields whose name or ``Doc``
    says percent, fraction or factor/scale (see ``core.validate.ratio_of``)."""

    per: float = 100.0
    factor: bool = False


@dataclass(frozen=True, init=False)
class Aliases:
    """On a ``Literal``: other spellings of its options, ``Aliases(mate="touch")``. Gate 1
    matches them like the options themselves (case, spaces, ``-`` and ``_`` ignored) and
    passes the canonical option on; listings never show them."""

    pairs: tuple[tuple[str, str], ...]

    def __init__(self, mapping: Mapping[str, str] | None = None, /, **pairs: str) -> None:
        object.__setattr__(self, "pairs", tuple({**(mapping or {}), **pairs}.items()))


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
Angle = Annotated[float | str, Quantity("angle"), Doc("Degrees, or '90 deg'")]
# An integer count that may also be an NX expression name or formula (``"n_holes"``).
CountValue = Annotated[int | str, Quantity("count")]
# Numbers that also take '50%' (and a factor '2x'); add Range and Doc where used.
Percent = Annotated[float, Ratio(100.0)]
Fraction = Annotated[float, Ratio(1.0)]
Factor = Annotated[float, Ratio(1.0, factor=True)]
Units = Annotated[
    Literal["mm", "in"],
    Aliases(
        inch="in",
        inches="in",
        millimeter="mm",
        millimeters="mm",
        millimetre="mm",
        millimetres="mm",
    ),
]
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
        # Up to three suffixes are named ("Workspace path (.zip)"); longer lists are in the
        # tool's description or its error hint.
        kinds = "/".join(self.suffixes) if len(self.suffixes) <= 3 else ""
        doc = f"Workspace path ({kinds})" if kinds else "Workspace path"
        return Text(260), PathArg(self.mode, self.suffixes), Doc(doc)


# Markers that stand for several others (``core.validate.unwrap`` expands them).
SHORTHANDS = (RefTo, PathTo)

PathIn = Annotated[str, PathTo("read")]
# Another tool's arguments (batch and preview children), checked by that tool's schema.
Arguments = Annotated[dict, Json(), Doc("The tool's own arguments")]


class PrincipalPlane(TypedDict):
    # NX view names: the front view looks at XZ, the top view at XY, the right view at YZ.
    principal: Annotated[Literal["xy", "yz", "xz"], Aliases(front="xz", top="xy", right="yz")]


class PlaneByNormal(TypedDict):
    origin: Point3
    normal: Vec3


class PrincipalAxis(TypedDict):
    principal: Literal["x", "y", "z", "-x", "-y", "-z"]


class AxisByPoint(TypedDict):
    origin: Point3
    direction: Vec3


# A bare principal name stands for its one-field form: 'x', '-Z', '+y', 'z axis' for
# {"principal": ...} of an axis or direction, 'xy', 'XY plane', 'top' of a plane (gate 1,
# ``core.validate.bare_object``); a string shaped like a ref ('e:3') stays a ref.
Plane = Annotated[str, RefTo("datum_plane", "face")] | PrincipalPlane | PlaneByNormal
Axis = Annotated[str, RefTo("datum_axis", "edge", "face")] | PrincipalAxis | AxisByPoint
Direction = Vec3 | Annotated[str, RefTo("edge", "datum_axis", "face")] | PrincipalAxis
AxisName = Literal["x", "y", "z", "-x", "-y", "-z"]
PRINCIPAL_VECTORS = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "z": (0.0, 0.0, 1.0),
    "-x": (-1.0, 0.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "-z": (0.0, 0.0, -1.0),
}


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


# Boolean synonyms agents use for NX's unite/subtract/intersect.
BOOLEAN_ALIASES = Aliases(
    union="unite",
    merge="unite",
    add="unite",
    join="unite",
    cut="subtract",
    difference="subtract",
    minus="subtract",
    remove="subtract",
    intersection="intersect",
    common="intersect",
)


class Combine(TypedDict):
    operation: Annotated[Literal["unite", "subtract", "intersect"], BOOLEAN_ALIASES]
    targets: Annotated[
        list[BodyRef],
        Items(1, 20),
        Doc("Target bodies; unite takes one"),
    ]


class _PoseRequired(TypedDict):
    translation: Point3


class PoseMatrix(_PoseRequired, total=False):
    rotation: Mat3


# Undocumented vectors keep the listed Pose short: the matrix form documents them.
_XYZ = tuple[float, float, float]


class _AxisAngleRequired(TypedDict):
    axis: AxisName | _XYZ
    angle: Angle


class PoseAxisAngle(_AxisAngleRequired, total=False):
    """A rotation by ``angle`` (right hand) about ``axis`` through ``origin`` (default the
    absolute origin), then ``translation``. Gate 9 turns it into the matrix form."""

    origin: _XYZ
    translation: _XYZ


# A pose: {translation, rotation?} (row-major matrix, p_out = R p + t) or the axis-angle
# form {axis, angle, origin?, translation?}; handlers always see the matrix form.
Pose = PoseMatrix | PoseAxisAngle


def axis_rotation(axis, angle_deg: float) -> list[list[float]]:
    """Row-major rotation by ``angle_deg`` about ``axis`` (a principal name or a vector)."""
    vector = PRINCIPAL_VECTORS[axis] if isinstance(axis, str) else axis
    x, y, z = (float(v) for v in vector)
    norm = math.sqrt(x * x + y * y + z * z)
    if not norm > 1e-12:
        raise ValueError("the rotation axis has zero length")
    x, y, z = x / norm, y / norm, z / norm
    a = math.radians(float(angle_deg))
    c, s, k = math.cos(a), math.sin(a), 1.0 - math.cos(a)
    return [
        [c + x * x * k, x * y * k - z * s, x * z * k + y * s],
        [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
        [z * x * k - y * s, z * y * k + x * s, c + z * z * k],
    ]


def pose_matrix(value: Mapping) -> dict:
    """The matrix form ``{translation, rotation}`` of an axis-angle pose (gate 9, after
    the angle is in degrees): rotate about the axis through ``origin``, then translate."""
    rotation = axis_rotation(value["axis"], value["angle"])
    origin = [float(v) for v in value.get("origin") or (0.0, 0.0, 0.0)]
    shift = [float(v) for v in value.get("translation") or (0.0, 0.0, 0.0)]
    turned = [sum(rotation[i][j] * origin[j] for j in range(3)) for i in range(3)]
    translation = [shift[i] + origin[i] - turned[i] for i in range(3)]
    return {"translation": translation, "rotation": rotation}


# TypedDict forms gate 9 replaces with a canonical value (after unit conversion), so the
# handler sees one form: ``{form: fn(value) -> canonical value}``.
CANONICAL: dict[Any, Any] = {PoseAxisAngle: pose_matrix}


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
    PoseMatrix,
    PoseAxisAngle,
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
# Longest accepted quantity string (a unit string such as "  -1.25e+3 mm ", or an NX
# expression formula): gate 1 and the listings bound it.
QUANTITY_MAX = 80
MM_PER_IN = 25.4
_SPLIT = re.compile(r"^\s*(" + _NUMBER + r")\s*([a-z]+)\s*$")
_PLAIN = re.compile(r"^\s*" + _NUMBER + r"\s*$")
# A number followed by a word: a unit string, of a unit this dimension does not take.
_UNIT_LIKE = re.compile(r"^\s*" + _NUMBER + r"\s*[A-Za-z]+\s*$")
_TOKEN = re.compile(
    r"\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)|([A-Za-z_]\w*)|(.))"
)
_OPERATORS = "+-*/^"
UNIT_WORDS = frozenset({"mm", "in", "deg", "rad", "cm", "m", "inch", "inches", "degrees"})
_EXAMPLES = {
    "length": "'10 mm' (mm or in)",
    "sheet_length": "'10 mm' (mm or in)",
    "angle": "'90 deg' (deg or rad)",
}


class Formula(float):
    """A quantity given as an NX expression name or formula (``"thickness"``,
    ``"thickness/2"``): gate 9 evaluates it in NX, in the units it converts quantities to,
    and passes this float on. ``formula`` is the text for a builder expression's
    ``RightHandSide`` (``kit.expr.rhs``), which keeps the feature driven by the expression.
    """

    formula: str

    def __new__(cls, value: float, formula: str) -> Formula:
        out = super().__new__(cls, value)
        out.formula = formula
        return out

    def __getnewargs__(self):  # copy/deepcopy keep the formula
        return float(self), self.formula

    def __neg__(self) -> Formula:
        return Formula(-float(self), f"-({self.formula})")


class CountFormula(int):
    """An integer count given as an NX expression name or formula (see :class:`Formula`)."""

    formula: str

    def __new__(cls, value: int, formula: str) -> CountFormula:
        out = super().__new__(cls, value)
        out.formula = formula
        return out

    def __getnewargs__(self):  # copy/deepcopy keep the formula
        return int(self), self.formula


def formula_problem(text: str) -> str | None:
    """Why ``text`` is not an NX expression name or arithmetic formula, else None: names,
    numbers, ``+ - * / ^``, parentheses and function calls (``sqrt(a)``, ``max(a, b)``)."""
    text = text.strip()
    if not text:
        return "is empty"
    calls: list[bool] = []  # open parentheses: a function call's?
    previous = None  # None (start), "operand", "operator", "function", "(" or ","
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            return "is not a formula"
        number, name, other = match.groups()
        position = match.end()
        if number or name:
            if previous in ("operand", "function"):
                return "has two values without an operator between them"
            follows = text[position:].lstrip()
            previous = "function" if name and follows.startswith("(") else "operand"
        elif other == "(":
            if previous == "operand":
                return "has a value directly before '('"
            calls.append(previous == "function")
            previous = "("
        elif other == ")":
            if not calls or previous in ("operator", ",") or (previous == "(" and not calls[-1]):
                return "has unbalanced or empty parentheses"
            calls.pop()
            previous = "operand"
        elif other == ",":
            if not calls or not calls[-1] or previous != "operand":
                return "has a misplaced ','"
            previous = ","
        elif other in _OPERATORS:
            if previous in ("operator", "function") or (
                previous in (None, "(", ",") and other not in "+-"
            ):
                return f"has a misplaced {other!r}"
            previous = "operator"
        else:
            return f"contains {other!r}, which a formula does not take"
    if calls:
        return "has unbalanced or empty parentheses"
    if previous in ("operator", "function", "(", ","):
        return "ends without a value"
    return None


def _expected(dim: str) -> str:
    if dim == "count":
        return "expected a whole number, or an NX expression name or formula"
    return (
        f"expected a number, a string like {_EXAMPLES[dim]}, or an NX expression name or "
        "formula ('thickness', 'thickness/2')"
    )


def quantity_formula(value, dim: str) -> str | None:
    """Gate 1 for a quantity: None for a number, a numeric string or (not for counts) a
    unit string of the dimension; the formula text for an NX expression name or formula.

    Raises ValueError for anything else: the wrong unit ("30 deg" for a length), a bare
    unit word, or text that is not a formula.
    """
    if isinstance(value, bool):
        raise ValueError(_expected(dim))
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("must be finite")
        if dim == "count" and not float(value).is_integer():
            raise ValueError("expected a whole number")
        return None
    if not isinstance(value, str) or len(value) > QUANTITY_MAX:
        raise ValueError(_expected(dim))
    if _PLAIN.match(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite")
        if dim == "count" and not number.is_integer():
            raise ValueError("expected a whole number")
        return None
    split = _SPLIT.match(value)
    if dim in QUANTITY_PATTERNS and split and re.match(QUANTITY_PATTERNS[dim], value):
        if not math.isfinite(float(split.group(1))):
            raise ValueError("must be finite")
        return None
    text = value.strip()
    if _UNIT_LIKE.match(value) or text.casefold() in UNIT_WORDS:
        raise ValueError(_expected(dim))
    problem = formula_problem(text)
    if problem is not None:
        raise ValueError(f"{_expected(dim)}; {text!r} {problem}")
    return text


def parse_quantity(value, dim: str) -> tuple[float, str | None]:
    """``(number, unit)`` of a number or unit-string quantity; unit is None for a plain
    number (or a numeric string). An evaluated :class:`Formula` is its number.

    Raises ValueError for anything else, an expression formula included (NX evaluates
    those, see :func:`convert_quantity`).
    """
    if isinstance(value, bool):
        raise ValueError("expected a number or a unit string")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite")
        return number, None
    if isinstance(value, str) and len(value) <= QUANTITY_MAX and _PLAIN.match(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite")
        return number, None
    if (
        not isinstance(value, str)
        or len(value) > QUANTITY_MAX
        or dim not in QUANTITY_PATTERNS
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


def convert_quantity(value, dim: str, to: str, evaluate=None, field: str | None = None):
    """Value of a quantity in ``to`` (``mm``/``in`` for lengths, ``deg`` for angles,
    ``unitless`` for counts): a float, an int for a count.

    A plain number is already in the target units (part/sheet units, degrees). An NX
    expression name or formula needs ``evaluate(formula, dim, to, field=field) -> float``
    (``Services.evaluate_formula``, gate 9) and comes back as a :class:`Formula` (a
    :class:`CountFormula` for a count) that carries its text.
    """
    if isinstance(value, (Formula, CountFormula)):
        return value
    formula = quantity_formula(value, dim) if isinstance(value, str) else None
    if formula is not None:
        if evaluate is None:
            raise ValueError("an NX expression formula needs NX to evaluate it")
        number = float(evaluate(formula, dim, to, field=field))
        if dim == "count":
            if not number.is_integer():
                raise ValueError(f"{formula!r} is {number:g}, not a whole number")
            return CountFormula(int(number), formula)
        return Formula(number, formula)
    if dim == "count":
        number, _ = parse_quantity(value, "count")
        if not number.is_integer():
            raise ValueError("expected a whole number")
        return int(number)
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
