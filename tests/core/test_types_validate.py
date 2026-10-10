"""Core argument types, the unit rule and the stdlib validator (gate 1)."""

import math
import re
from typing import Annotated, Literal

import pytest

from nx_mcp.core import types as T
from nx_mcp.core.catalogue import Param
from nx_mcp.core.errors import NXError
from nx_mcp.core.validate import validate


def params(**hints):
    return {k: Param(k, h, None, True) for k, h in hints.items()}


@pytest.mark.parametrize(
    ("value", "dim", "to", "expected"),
    [
        (10, "length", "mm", 10.0),
        ("25.4 mm", "length", "in", 1.0),
        ("1 in", "length", "mm", 25.4),
        (" .5in ", "length", "mm", 12.7),
        ("1e1 mm", "length", "mm", 10.0),
        ("90 deg", "angle", "deg", 90.0),
        ("3.141592653589793 rad", "angle", "deg", 180.0),
        ("2 mm", "sheet_length", "in", 2 / 25.4),
    ],
)
def test_unit_strings_convert_explicitly(value, dim, to, expected):
    assert T.convert_quantity(value, dim, to) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "dim"),
    [
        ("30 deg", "length"),
        ("10 mm", "angle"),
        ("10 inch", "length"),
        ("mm", "length"),
        (True, "length"),
        (math.inf, "length"),
        ("1e999 mm", "length"),
    ],
)
def test_wrong_dimension_or_spelling_is_rejected(value, dim):
    with pytest.raises(ValueError):
        T.parse_quantity(value, dim)


def test_patterns_match_the_architecture_rule():
    assert re.match(T.LENGTH_PATTERN, "-1.5e-3 in") and not re.match(T.LENGTH_PATTERN, "1 cm")
    assert re.match(T.ANGLE_PATTERN, "+.5 rad") and not re.match(T.ANGLE_PATTERN, "5 mm")


def test_validator_rejects_extra_missing_and_out_of_bounds():
    spec = params(n=Annotated[int, T.Range(ge=1, le=3)], p=T.Point3)
    with pytest.raises(NXError) as e:
        validate(spec, {"n": 2, "p": [0, 0, 0], "x": 1})
    assert e.value.field == "/x" and e.value.category == "invalid_argument"
    with pytest.raises(NXError) as e:
        validate(spec, {"n": 2})
    assert e.value.field == "/p"
    with pytest.raises(NXError) as e:
        validate(spec, {"n": 4, "p": [0, 0, 0]})
    assert e.value.field == "/n"
    with pytest.raises(NXError) as e:
        validate(spec, {"n": 1, "p": [0, 0]})
    assert e.value.field == "/p"
    with pytest.raises(NXError) as e:
        validate(spec, {"n": 1, "p": [0, math.nan, 0]})
    assert e.value.field == "/p/1"
    with pytest.raises(NXError):
        validate(spec, {"n": True, "p": [0, 0, 0]})


def test_items_text_and_literal():
    spec = params(
        refs=Annotated[list[Annotated[str, T.RefTo("edge")]], T.Items(1, 2)],
        mode=Literal["a", "b"],
        name=T.Name,
    )
    ok = validate(spec, {"refs": ["e:1", "obj_" + "a" * 20], "mode": "a", "name": "PLATE_1"})
    assert ok.refs == [(("refs", 0), ("edge",)), (("refs", 1), ("edge",))]
    for bad, field in [
        ({"refs": [], "mode": "a", "name": "x"}, "/refs"),
        ({"refs": ["edge 1"], "mode": "a", "name": "x"}, "/refs/0"),
        ({"refs": ["e:1"], "mode": "c", "name": "x"}, "/mode"),
        ({"refs": ["e:1"], "mode": "a", "name": "x" * 65}, "/name"),
    ]:
        with pytest.raises(NXError) as e:
            validate(spec, bad)
        assert e.value.field == field


def test_unions_and_typeddicts():
    spec = params(plane=T.Plane, limit=T.Limit, pose=T.Pose)
    ok = validate(
        spec,
        {
            "plane": {"principal": "xy"},
            "limit": {"type": "distance", "value": "5 mm"},
            "pose": {"translation": [1, 2, 3]},
        },
    )
    assert ok.quantities == [(("limit", "value"), "length")]
    assert validate(
        spec,
        {
            "plane": "d:3",
            "limit": {"type": "through_all"},
            "pose": {"translation": [0, 0, 0], "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]},
        },
    ).args
    with pytest.raises(NXError) as e:
        validate(
            spec,
            {
                "plane": {"principal": "xy", "extra": 1},
                "limit": {"type": "through_all"},
                "pose": {"translation": [0, 0, 0]},
            },
        )
    assert e.value.field == "/plane/extra"  # the principal form, its unknown field
    with pytest.raises(NXError) as e:
        validate(
            spec,
            {
                "plane": "d:3",
                "limit": {"type": "distance", "value": "3 deg"},
                "pose": {"translation": [0, 0, 0]},
            },
        )
    assert "limit" in e.value.field


def test_union_errors_name_the_best_matching_form():
    from typing import TypedDict

    class Path(TypedDict):
        path: Annotated[list[Annotated[str, T.RefTo("edge")]], T.Items(1, 5)]
        help_point: T.Point3

    class PathMore(Path, total=False):
        percent: Annotated[float, T.Range(ge=0, le=100)]

    class On(TypedDict):
        on: Annotated[str, T.RefTo("face")]

    spec = params(placement=T.PrincipalPlane | On | PathMore)
    bad = {"path": ["e:1"], "help_point": [0, 0, 0], "percent": 150}
    with pytest.raises(NXError) as e:
        validate(spec, {"placement": bad})
    assert e.value.field == "/placement/percent" and "<= 100" in e.value.message
    with pytest.raises(NXError) as e:  # missing required key of the path form
        validate(spec, {"placement": {"path": ["e:1"]}})
    assert e.value.field == "/placement/help_point"
    with pytest.raises(NXError) as e:  # nothing fits better than anything else
        validate(spec, {"placement": {"bogus": 1}})
    assert e.value.field == "/placement" and "any accepted form" in e.value.message


def test_null_is_refused_for_required_fields_and_allowed_for_optional_ones():
    spec = {
        "need": Param("need", T.Length, None, True),
        "maybe": Param("maybe", T.Length | None, None, False),
    }
    assert validate(spec, {"need": 1, "maybe": None}).args == {"need": 1, "maybe": None}
    with pytest.raises(NXError) as e:
        validate(spec, {"need": None})
    assert e.value.field == "/need"


def test_placeholders_only_in_ref_fields():
    spec = params(feature=Annotated[str, T.RefTo("feature")], name=T.Name)
    ok = validate(spec, {"feature": "$2.data.feature", "name": "A"}, placeholders=True)
    assert ok.placeholders == [(("feature",), "$2.data.feature")]
    with pytest.raises(NXError):
        validate(spec, {"feature": "f:1", "name": "$2.data.x"}, placeholders=True)
    with pytest.raises(NXError):
        validate(spec, {"feature": "$2.data.feature", "name": "A"})


def test_paths_are_recorded_with_mode():
    spec = params(path=Annotated[str, T.PathTo("new", ".step", ".stp")])
    ok = validate(spec, {"path": "out/a.step"})
    ((path, marker),) = ok.paths
    assert path == ("path",) and marker.mode == "new" and marker.suffixes == (".step", ".stp")


def test_ref_and_path_shorthands_expand_in_place():
    from nx_mcp.core.validate import unwrap

    _, markers = unwrap(T.BodyRef)
    assert markers == {
        T.Text: T.Text(128, T.REF_PATTERN),
        T.RefKind: T.RefKind("body"),
        T.Doc: T.Doc("Ref: body"),
    }
    # An explicit Doc beats the one a shorthand or an alias implies, wherever it stands.
    assert unwrap(Annotated[T.BodyRef, T.Doc("Target body")])[1][T.Doc] == T.Doc("Target body")
    assert unwrap(Annotated[T.Length, T.Doc("Gap")])[1][T.Doc] == T.Doc("Gap")
    first = Annotated[str, T.Doc("Out file"), T.PathTo("new")]
    assert unwrap(first)[1][T.Doc] == T.Doc("Out file")
    assert unwrap(Annotated[str, T.PathTo("new", ".zip")])[1][T.Doc] == T.Doc(
        "Workspace path (.zip)"
    )
    _, markers = unwrap(Annotated[str, T.RefTo("face", doc="Ref: bend face")])
    assert markers[T.Doc] == T.Doc("Ref: bend face")
    assert unwrap(T.Ref)[1][T.Doc] == T.Doc("Ref: any")
    _, markers = unwrap(Annotated[str, T.PathTo("new", ".STEP")])
    assert markers == {
        T.Text: T.Text(260),
        T.PathArg: T.PathArg("new", (".step",)),
        T.Doc: T.Doc("Workspace path (.step)"),
    }


def test_forbid_extra_marks_typeddicts_for_pydantic():
    from typing import TypedDict

    class Spec(TypedDict):
        a: int

    T.forbid_extra(Spec)
    assert Spec.__pydantic_config__ == {"extra": "forbid"}
