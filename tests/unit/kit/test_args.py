import math

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import args


def _error(call, *a, **k):
    with pytest.raises(NXError) as error:
        call(*a, **k)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    return error.value


def test_pointer():
    assert args.pointer("radius") == "/radius"
    assert args.pointer("/radius") == "/radius"
    assert args.pointer("points[1]") == "/points/1"
    assert args.pointer("limit.value") == "/limit/value"


def test_required_returns_the_value():
    assert args.required(0, "offset") == 0
    err = _error(args.required, None, "origin")
    assert (err.field, str(err.message)) == ("/origin", "origin is required")


def test_cross_field_rules():
    args.need(a=1, b=0)
    assert _error(args.need, a=1, b=None).field == "/b"
    assert args.exactly_one(a=None, b=2) == "b"
    assert _error(args.exactly_one, a=1, b=2).field == "/b"
    assert _error(args.exactly_one, a=None, b=None).field == "/a"
    assert args.distinct("/edges", ["a", "b"]) == ["a", "b"]
    err = _error(args.distinct, "/edges", ["a", "b", "a"])
    assert err.field == "/edges/2" and "duplicates" in err.message


def test_numbers():
    assert args.finite(2, "radius", True) == 2.0
    for bad in (0, -1, math.nan, math.inf, True, "3", None):
        err = _error(args.finite, bad, "radius", True)
        assert err.message == "radius must be finite and positive" and err.field == "/radius"
    assert args.number(None, "depth", allow_none=True) is None
    assert _error(args.number, "x", "depth").message == "depth must be a finite number"
    assert _error(args.number, 0, "depth", positive=True).message == "depth must be positive"
    err = _error(args.number, 1001, "ra", positive=True, maximum=1000)
    assert err.message == "ra must be positive and at most 1000"
    assert _error(args.number, 5, "x", maximum=4).message == "x must be at most 4"


def test_points_and_vectors():
    assert args.vec3((1, 2, 3), "origin") == [1.0, 2.0, 3.0]
    assert _error(args.vec3, [1, 2], "origin").message == "origin must contain three numbers"
    assert _error(args.vec3, [1, True, 2], "origin").message == "origin must be finite"
    assert args.point2([1, 2]) == [1.0, 2.0]
    assert _error(args.point2, [1, 2, 3], "center").message == "center must be local [x, y]"
    assert args.sheet_point([10, 20.5]) == [10.0, 20.5]
    assert "sheet units" in _error(args.sheet_point, [1], "origin").message
    assert args.points3([[0, 0, 0], [1, 1, 1]], minimum=2) == [[0, 0, 0], [1, 1, 1]]
    assert _error(args.points3, [[0, 0]]).field == "/points/0"
    assert _error(args.points3, [[0, 0, 0]], minimum=2).message == "Expected 2..1000 points"


def test_references():
    assert args.ref("b:1", "body") == "b:1"
    assert (
        _error(args.ref, " ", "feature", "a feature ID").message == "feature must be a feature ID"
    )
    assert args.refs(["a", "b"], "edges", 1, 50) == ["a", "b"]
    assert "non-empty list of 1-50 object IDs" in _error(args.refs, [], "edges", 1, 50).message
    assert _error(args.refs, "a", "edges").message == "edges must be a non-empty list of object IDs"
    assert _error(args.refs, ["a"], "objects", 2, 2).message == "objects must list 2 object IDs"
    assert "at most 3" in _error(args.refs, list("abcd"), "edges", 1, 3).message
    assert "non-empty" in _error(args.refs, ["a", ""], "edges").message
    err = _error(args.refs, ["a", "a"], "edges")
    assert "duplicates" in err.message and "distinct" in err.message and "repeat" in err.message


def test_choice():
    assert args.choice("mm", "units", {"mm", "in"}) == "mm"
    assert _error(args.choice, "cm", "units", {"mm", "in"}).message == "units must be one of in, mm"
