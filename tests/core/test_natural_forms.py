"""Natural argument forms (architecture.md 1.1): bare principal names for planes, axes and
directions, every invalid field in one error, per-variant required fields, a variant taken
from the referenced object, variant errors that list only the offered variants, percent and
factor strings, JSON-array strings and the axis-angle pose. The sidecar's Pydantic adapter
accepts the same values (``tests/sidecar/test_natural_forms_sidecar.py``)."""

import math
from typing import Annotated, Literal, TypedDict

import pytest

from nx_mcp.core import evidence, kernel
from nx_mcp.core import types as T
from nx_mcp.core.catalogue import CatalogueError, Param, make_spec
from nx_mcp.core.errors import NXError
from nx_mcp.core.validate import match_option, validate
from tests.core.fakes import FakeServices


def params(**hints):
    return {k: Param(k, h, None, True) for k, h in hints.items()}


def refused(spec, args, **options) -> NXError:
    with pytest.raises(NXError) as error:
        validate(spec, args, **options)
    return error.value


# -- bare principal names ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "principal"),
    [("x", "x"), ("-z", "-z"), ("Z", "z"), ("+y", "y"), ("-X", "-x"), ("z axis", "z")],
)
def test_bare_names_stand_for_principal_axes_and_directions(value, principal):
    spec = params(axis=T.Axis, direction=T.Direction)
    out = validate(spec, {"axis": value, "direction": value}).args
    assert out == {"axis": {"principal": principal}, "direction": {"principal": principal}}


@pytest.mark.parametrize(
    ("value", "principal"),
    [("xy", "xy"), ("XY", "xy"), ("XY plane", "xy"), ("yz-plane", "yz"), ("top", "xy")],
)
def test_bare_names_stand_for_principal_planes(value, principal):
    assert validate(params(p=T.Plane), {"p": value}).args["p"] == {"principal": principal}


def test_ref_shaped_strings_stay_refs_and_the_dict_form_takes_natural_spellings():
    spec = params(axis=T.Axis, plane=T.Plane, direction=T.Direction)
    out = validate(spec, {"axis": "e:3", "plane": "d:12", "direction": "f:12"}).args
    assert out == {"axis": "e:3", "plane": "d:12", "direction": "f:12"}
    out = validate(
        spec,
        {"axis": {"principal": "+Z"}, "plane": {"principal": "XY plane"}, "direction": [0, 0, 1]},
    ).args
    assert out["axis"] == {"principal": "z"} and out["plane"] == {"principal": "xy"}
    assert match_option("+y", ("y", "-y")) == "y" and match_option("-y axis", ("y", "-y")) == "-y"


def test_a_wrong_bare_name_lists_every_accepted_form():
    error = refused(params(axis=T.Axis), {"axis": "w"})
    assert error.field == "/axis"
    for form in ("ref like", "'x', 'y', 'z', '-x', '-y', '-z' or {principal}", "{direction"):
        assert form in error.message
    plane = refused(params(p=T.Plane), {"p": "xx"}).message
    assert "'xy', 'yz', 'xz' or {principal}" in plane and "{normal, origin}" in plane


# -- every invalid field at once -------------------------------------------------------------


class Box(TypedDict):
    size: T.Length
    count: int


def test_every_invalid_field_is_reported_in_one_error():
    spec = {
        "axis": Param("axis", T.Axis, None, True),
        "box": Param("box", Box, None, True),
        "points": Param("points", Annotated[list[T.Point3], T.Items(1, 4)], None, True),
        "name": Param("name", T.Name | None, None, False),
    }
    args = {"axis": "w", "box": {"size": "10 kg", "cnt": 2}, "points": [[0, 0, 0], [1]]}
    error = refused(spec, {**args, "nme": "a"})
    rows = error.details["errors"]
    fields = [r["field"] for r in rows]
    assert fields == ["/nme", "/axis", "/box/cnt", "/box/count", "/box/size", "/points/1"]
    assert error.field == "/nme" and error.message.startswith("/nme: unknown field")
    assert "5 more invalid: /axis, /box/cnt" in error.message
    assert error.details["accepted"] == ["axis", "box", "points", "name"]  # the first's details
    assert all(r["message"] and not r["message"].startswith("/") for r in rows)
    missing = refused(params(a=int, b=int), {})
    assert [r["field"] for r in missing.details["errors"]] == ["/a", "/b"]
    single = refused(params(a=int), {"a": "x"})
    assert "errors" not in single.details  # one error stays as it was


def test_union_alternatives_still_pick_the_meant_form():
    error = refused(params(end=T.Limit), {"end": {"type": "distance", "value": "10 kg"}})
    assert error.field == "/end/value" and "errors" not in error.details


# -- required fields per variant -----------------------------------------------------------


class Out(TypedDict):
    ok: bool


def nx_create_widget(
    tx,
    *,
    kind: Literal["hole", "bend", "plain"],
    view: Annotated[str, T.RefTo("drawing_view")] | None = None,
    origin: T.EdgeRef | None = None,
    note: Annotated[str, T.Text(20)] | None = None,
) -> Out:
    """Create a widget."""
    return {"ok": True}


def widget(**options):
    options.setdefault("example", {"kind": "plain"})
    return make_spec(
        nx_create_widget,
        effect="model",
        label="Create widget",
        keywords=("a", "b", "c"),
        variants="kind",
        domain="modeling",
        **options,
    )


def test_required_by_names_the_fields_a_variant_needs():
    spec = widget(required_by={"hole": ("view", "origin"), "bend": ("view",)})
    error = pytest.raises(NXError, spec.check, {"kind": "hole", "origin": "e:3"}).value
    assert error.field == "/view" and "required when kind is 'hole'" in error.message
    both = pytest.raises(NXError, spec.check, {"kind": "Hole", "note": 5}).value
    assert [r["field"] for r in both.details["errors"]] == ["/note", "/view", "/origin"]
    assert spec.check({"kind": "plain"}).args == {"kind": "plain"}
    with pytest.raises(CatalogueError, match="'nope' is not a value"):
        widget(required_by={"nope": ("view",)})
    with pytest.raises(CatalogueError, match="'kind' is no optional parameter"):
        widget(required_by={"hole": ("kind",)})
    with pytest.raises(CatalogueError, match="example does not meet required_by"):
        widget(required_by={"hole": ("view",)}, example={"kind": "hole"})


def test_variant_errors_list_only_the_offered_variants():
    spec = widget()
    error = pytest.raises(NXError, spec.check, {"kind": "bent"}, shown=("hole", "plain")).value
    assert "expected one of 'hole', 'plain'" in error.message and "bend" not in error.message
    assert spec.check({"kind": "bend"}, shown=("hole",)).args["kind"] == "bend"  # gate 3 refuses


def test_the_evidence_refusal_names_the_available_variants(monkeypatch):
    spec = widget()
    monkeypatch.setattr(evidence, "_CACHE", {spec.name: {"v2406": {"variants": ["hole"]}}})
    refusal = evidence.check(spec, "v2406", {"kind": "bend"})
    assert "variant 'bend' is not tested; available here: 'hole'" in refusal.message
    assert refusal.details["available"] == ["hole"]


def test_the_kernel_lists_the_offered_variants_in_production(tmp_path, monkeypatch):
    spec = widget()
    monkeypatch.setattr(evidence, "_CACHE", {spec.name: {"v2406": {"variants": ["plain"]}}})
    svc = FakeServices(tmp_path, dev=False)
    out = kernel.run(spec, {"kind": "holes!"}, kernel.Context(svc))
    assert out["error"]["field"] == "/kind" and "expected one of 'plain'" in out["error"]["message"]


# -- the variant from the referenced object --------------------------------------------------


class Edited(TypedDict):
    kind: str


def nx_edit_widget(
    tx,
    *,
    table: Annotated[str, T.RefTo("drawing_table", "feature")],
    kind: Literal["parts_list", "revision", "feature"],
    rows: Annotated[list[Annotated[str, T.Text(9)]], T.Items(1, 3)] | None = None,
) -> Edited:
    """Edit a widget."""
    return {"kind": kind}


def edit_widget(**options):
    return make_spec(
        nx_edit_widget,
        effect="model",
        label="Edit widget",
        keywords=("a", "b", "c"),
        variants="kind",
        domain="modeling",
        example={"table": "t:4", "kind": "revision", "rows": ["A"]},
        **options,
    )


def test_an_omitted_variant_comes_from_the_referenced_object(tmp_path):
    seen = []

    def kind_of(tx, obj):
        seen.append(obj)
        return "revision"

    spec = edit_widget(variant_from=("table", kind_of), required_by={"revision": ("rows",)})
    assert not spec.params["kind"].required  # gate 1 lets it be left out
    svc = FakeServices(tmp_path)
    table = svc.part.add("drawing_table")
    ref = svc.references.row(table, "drawing_table")["id"]
    ok = kernel.run(spec, {"table": ref, "rows": ["A"]}, kernel.Context(svc))
    assert ok["ok"] and ok["data"] == {"kind": "revision"} and seen == [table]
    needs = kernel.run(spec, {"table": ref}, kernel.Context(svc))
    assert needs["error"]["field"] == "/rows" and needs["mutation_outcome"] == "not_started"
    given = kernel.run(spec, {"table": ref, "kind": "parts_list"}, kernel.Context(svc))
    assert given["data"] == {"kind": "parts_list"} and len(seen) == 2  # given: not inferred


def test_without_a_resolver_the_ref_kind_is_the_variant(tmp_path):
    spec = edit_widget(variant_from="table")
    svc = FakeServices(tmp_path)
    feature = svc.part.add("feature")
    table = svc.part.add("drawing_table")
    by_kind = kernel.run(
        spec, {"table": svc.references.row(feature, "feature")["id"]}, kernel.Context(svc)
    )
    assert by_kind["data"] == {"kind": "feature"}
    unknown = kernel.run(
        spec, {"table": svc.references.row(table, "drawing_table")["id"]}, kernel.Context(svc)
    )
    assert unknown["error"]["field"] == "/kind"
    assert "cannot be taken from the drawing_table" in unknown["error"]["message"]
    with pytest.raises(CatalogueError, match="reference parameter"):
        edit_widget(variant_from="rows")


# -- percent, factor and JSON-array strings ---------------------------------------------------


def test_percent_fraction_and_factor_strings():
    spec = {
        "percent": Param("percent", Annotated[float, T.Range(ge=0, le=100)], None, True),
        "at": Param("at", Annotated[float, T.Range(ge=0, le=1)], None, True),
        "neutral_factor": Param("neutral_factor", float, None, True),
        "zoom": Param("zoom", Annotated[float, T.Doc("x current scale")], None, True),
        "share": Param("share", T.Percent, None, True),
        "scale": Param("scale", T.Factor | None, None, False),
        "opacity": Param("opacity", Annotated[int, T.Doc("0-100 %")], None, True),
    }
    args = {
        "percent": "50%",
        "at": "25 %",
        "neutral_factor": "44%",
        "zoom": "2x",
        "share": "12.5%",
        "scale": "200%",
        "opacity": "40%",
    }
    out = validate(spec, args).args
    assert out == {
        "percent": 50.0,
        "at": 0.25,
        "neutral_factor": 0.44,
        "zoom": 2.0,
        "share": 12.5,
        "scale": 2.0,
        "opacity": 40,
    }
    assert validate({"scale": spec["scale"]}, {"scale": "x3"}).args["scale"] == 3.0
    assert refused(params(depth=float), {"depth": "50%"}).field == "/depth"  # not a ratio
    assert refused(params(share=T.Percent), {"share": "2x"}).field == "/share"  # no factor


def test_a_factor_that_also_takes_an_expression_reads_percent_and_times():
    factor = Param("factor", Annotated[T.ScalarValue, T.Range(gt=0)], None, True)
    assert validate({"factor": factor}, {"factor": "200%"}).args["factor"] == 2.0
    assert validate({"factor": factor}, {"factor": "1.5x"}).args["factor"] == 1.5
    # a bare name stays an expression name, a formula stays a formula
    assert validate({"factor": factor}, {"factor": "x2"}).args["factor"] == "x2"
    assert validate({"factor": factor}, {"factor": "k*2"}).args["factor"] == "k*2"
    other = Param("depth_value", T.ScalarValue, None, True)  # not a factor: no percent
    assert refused({"depth_value": other}, {"depth_value": "50%"}).field == "/depth_value"


def test_json_array_strings_for_points_and_lists():
    spec = params(p=T.Point3, refs=Annotated[list[T.BodyRef], T.Items(1, 3)])
    out = validate(spec, {"p": "[1, 2.5, -3]", "refs": '["b:1", "b:2"]'}).args
    assert out == {"p": [1.0, 2.5, -3.0], "refs": ["b:1", "b:2"]}
    assert validate(spec, {"p": [0, 0, 0], "refs": "b:4"}).args["refs"] == ["b:4"]
    assert refused(spec, {"p": "[1, 2]", "refs": "b:1"}).field == "/p"


# -- the axis-angle pose ---------------------------------------------------------------------


def flat(matrix):
    return [v for row in matrix for v in row]


def test_axis_angle_pose_becomes_the_matrix_form_at_gate_9():
    checked = validate(
        params(pose=T.Pose), {"pose": {"axis": "Z", "angle": "90 deg", "origin": [10, 0, 0]}}
    )
    assert [p for p, _ in checked.forms] == [("pose",)]
    assert checked.quantities == [(("pose", "angle"), "angle")]
    matrix = validate(params(pose=T.Pose), {"pose": {"translation": [1, 2, 3]}})
    assert matrix.forms == [] and matrix.args["pose"] == {"translation": [1.0, 2.0, 3.0]}
    pose = T.pose_matrix(
        {"axis": "z", "angle": 90.0, "origin": [10, 0, 0], "translation": [0, 0, 5]}
    )
    assert pose["translation"] == pytest.approx([10, -10, 5])
    assert flat(pose["rotation"]) == pytest.approx([0, -1, 0, 1, 0, 0, 0, 0, 1], abs=1e-12)
    tilted = T.pose_matrix({"axis": [1, 1, 0], "angle": 180})
    assert flat(tilted["rotation"]) == pytest.approx([0, 1, 0, 1, 0, 0, 0, 0, -1], abs=1e-12)
    with pytest.raises(ValueError, match="zero length"):
        T.axis_rotation([0, 0, 0], 10)


class Posed(TypedDict):
    pose: dict


def nx_set_widget_pose(tx, *, pose: T.Pose) -> Posed:
    """Place a widget."""
    return {"pose": pose}


def test_the_handler_sees_the_matrix_form(tmp_path):
    spec = make_spec(
        nx_set_widget_pose,
        effect="model",
        label="Place widget",
        keywords=("a", "b", "c"),
        domain="modeling",
        example={"pose": {"axis": "z", "angle": "30deg"}},
    )
    svc = FakeServices(tmp_path)
    args = {"pose": {"axis": "-x", "angle": f"{math.pi / 2} rad"}}
    pose = kernel.run(spec, args, kernel.Context(svc))["data"]["pose"]
    assert pose["translation"] == pytest.approx([0, 0, 0])
    assert flat(pose["rotation"]) == pytest.approx([1, 0, 0, 0, 0, 1, 0, -1, 0], abs=1e-9)


def test_batch_child_errors_point_every_row_at_the_step():
    from nx_mcp.core.validate import at_child

    error = refused(params(a=int, b=int), {"a": "x"})
    moved = at_child(error, 2)
    assert moved.field == "/operations/2/arguments/a"
    assert [r["field"] for r in moved.details["errors"]] == [
        "/operations/2/arguments/a",
        "/operations/2/arguments/b",
    ]
    assert at_child(NXError("NX_INVALID_ARGUMENT", "x", field="/tool"), 0).field == (
        "/operations/0/tool"
    )
