"""Schema generation: Pydantic adapters and JSON Schema from core types.

The parity test feeds the same generated samples to the sidecar's Pydantic
adapter and to ``core.validate`` (the NX-side gate 1): both must accept and
reject exactly the same arguments.
"""

import itertools
import json
import typing
from typing import Annotated, Literal, TypedDict

import jsonschema
import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import NXError
from nx_mcp.core.types import QUANTITY_MAX as T_QUANTITY_MAX
from nx_mcp.core.types import (
    Angle,
    Axis,
    Combine,
    Direction,
    Doc,
    Items,
    Length,
    Limit,
    Name,
    Offset,
    PageLimit,
    PathIn,
    Plane,
    Point2,
    Pose,
    Quantity,
    Range,
    Ref,
    SheetLength,
    SketchPoint,
    Text,
)
from nx_mcp.core.validate import is_typeddict, is_union, typeddict_fields, unwrap, validate
from nx_mcp.sidecar import schema


class Out(TypedDict):
    count: int


def nx_create_sample(
    tx,
    *,
    kind: Literal["a", "b"],
    plane: Plane | None = None,
    axis: Axis | None = None,
    direction: Direction | None = None,
    end: Limit | None = None,
    combine: Combine | None = None,
    pose: Pose | None = None,
    corner: SketchPoint | None = None,
    angle: Angle = 0,
    width: SheetLength | None = None,
    length: Length | None = None,
    points: Annotated[list[Point2], Items(1, 4)] | None = None,
    count: Annotated[int, Range(ge=1, le=9), Doc("How many")] = 1,
    ratio: Annotated[float, Range(gt=0, lt=1)] | None = None,
    flag: bool = False,
    label: Annotated[str, Text(8, r"^[a-z]+$")] | None = None,
    name: Name | None = None,
    target: Ref | None = None,
    path: PathIn | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> Out:
    """Create a sample object for schema tests."""


SAMPLE = make_spec(
    nx_create_sample,
    effect="model",
    label="Create sample",
    keywords=("sample", "test", "schema"),
    variants="kind",
    domain="modeling",
    example={"kind": "a"},
)


def specs():
    return [SAMPLE, *catalogue.load().values()]


# ---------------------------------------------------------------------------
# Sample generation from core-type hints


def valid_value(hint):
    base, markers = unwrap(hint)
    if Quantity in markers:
        return 30 if markers[Quantity].dim == "angle" else "2.5 mm"
    if is_union(base):
        alternatives = [a for a in typing.get_args(base) if a is not type(None)]
        return valid_value(alternatives[0])
    if typing.get_origin(base) is Literal:
        return typing.get_args(base)[0]
    if base is bool:
        return True
    if base is int:
        bounds = markers.get(Range)
        return int(bounds.ge) if bounds and bounds.ge is not None else 3
    if base is float:
        bounds = markers.get(Range)
        return 0.5 if bounds else 1.5
    if base is str:
        text = markers.get(Text)
        if text and text.pattern and "obj_" in text.pattern:
            return "b:41"
        if text and text.pattern and text.pattern.startswith("^[a-z]+"):
            return "abc"
        return "Name_1"
    origin = typing.get_origin(base)
    if origin is list:
        items = markers.get(Items)
        return [valid_value(typing.get_args(base)[0])] * (items.min if items else 1)
    if origin is tuple:
        return [valid_value(a) for a in typing.get_args(base)]
    if is_typeddict(base):
        fields = typeddict_fields(base)
        return {k: valid_value(fields[k]) for k in base.__required_keys__}
    if base is dict:  # Json: another tool's arguments (nx_run_batch)
        return {}
    raise AssertionError(base)


INVALID = [None, True, "x", 1e400, -1, 10**9, 2.5, [], {}, "10 deg", "10 mm", "b:0", "a" * 300]


def samples(spec):
    """Valid arguments, then one mutation per parameter and bad value."""
    base = {p.name: valid_value(p.hint) for p in spec.params.values() if p.required}
    yield dict(spec.example) or base
    yield base
    for name, param in spec.params.items():
        good = valid_value(param.hint)
        yield {**base, name: good}
        for bad in INVALID:
            yield {**base, name: bad}
        if isinstance(good, dict):
            yield {**base, name: {**good, "extra": 1}}
            for key in good:
                yield {**base, name: {k: v for k, v in good.items() if k != key}}
        if isinstance(good, list):
            yield {**base, name: [*good, *good, *good, *good, *good]}
            if good and isinstance(good[0], list):
                yield {**base, name: [good[0][:-1]]}
    yield {**base, "unknown": 1}
    yield {}


def accepts_core(spec, args):
    try:
        validate(spec.params, args)
        return True
    except NXError:
        return False


def accepts_pydantic(spec, args):
    try:
        schema.adapter(spec).validate_python(args)
        return True
    except Exception:
        return False


@pytest.mark.parametrize("spec", specs(), ids=lambda s: s.name)
def test_pydantic_and_core_validators_agree(spec):
    checked = 0
    total = sum(1 for _ in samples(spec))
    for args in samples(spec):
        assert accepts_pydantic(spec, args) == accepts_core(spec, args), (spec.name, args)
        checked += 1
    assert checked > min(20, total // 2)  # tools with 0-1 parameters yield few samples


def _walk(node, path="#"):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for name, sub in value.items():
                    yield from _walk(sub, f"{path}/{key}/{name}")
            elif isinstance(value, (dict, list)):
                yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}/{i}")


@pytest.mark.parametrize("spec", specs(), ids=lambda s: s.name)
def test_schema_is_bounded_untitled_and_its_refs_resolve(spec):
    generated = schema.input_schema(spec)
    defs = generated.get("$defs", {})
    for path, node in _walk(generated):
        if not isinstance(node, dict):
            continue
        assert "title" not in node, path
        ref = node.get("$ref")
        if ref is not None:
            assert ref.startswith("#/$defs/") and ref.split("/")[-1] in defs, path
        types = node.get("type")
        types = types if isinstance(types, list) else [types]
        if "array" in types:
            assert "maxItems" in node, path
        if "string" in types:
            assert "maxLength" in node or "enum" in node, path
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, path
    assert generated["additionalProperties"] is False
    text = json.dumps(generated)
    for unshown in schema.LISTING_UNSHOWN_PATTERNS:  # gate 1 checks these
        assert json.dumps(unshown) not in text


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("spec", specs(), ids=lambda s: s.name)
def test_listed_schema_accepts_what_gate_1_accepts(spec):
    """The compact listing never refuses the canonical form of a valid call except an
    explicit null for an optional field (documented by omission); it is looser only where
    gate 1 checks a reference or unit-string pattern that the listing leaves out (and JSON
    has no inf). Natural spellings gate 1 also takes ("Through All" for "through_all", one
    ref for a list of refs) are not listed: gate 1 turns them into the canonical form."""
    validator = jsonschema.Draft202012Validator(schema.input_schema(spec, call_keys=False))
    for args in samples(spec):
        if any(args.get(p.name, 0) is None for p in spec.params.values() if not p.required):
            continue
        if accepts_core(spec, args):
            args = validate(spec.params, args).args
        listed_ok = validator.is_valid(args)
        variant = spec.variant_of(args)
        if listed_ok and schema._by_variant(spec) and variant in spec.variant_values():
            # A ByVariant argument's fields are listed by tool#variant only.
            narrowed = schema.variant_schema(spec, variant, call_keys=False)
            listed_ok = jsonschema.Draft202012Validator(narrowed).is_valid(args)
        if accepts_pydantic(spec, args):
            assert listed_ok, (spec.name, args)
        elif listed_ok and "inf" not in repr(args):
            assert {"x", "b:0", "10 deg", "10 mm"} & set(_strings(args)), (spec.name, args)


def test_a_field_named_like_a_keyword_is_still_a_field():
    def nx_edit_sample(
        tx,
        *,
        pattern: Ref,
        type: Annotated[str, Text(8, r"^[a-z]+$")] | None = None,
        anyOf: Length | None = None,  # noqa: N803
    ) -> Out:
        """Edit a sample with awkward field names."""

    spec = make_spec(
        nx_edit_sample,
        effect="model",
        label="x",
        keywords=("a", "b", "c"),
        domain="modeling",
        example={"pattern": "f:1"},
    )
    props = schema.input_schema(spec, call_keys=False)["properties"]
    assert props["pattern"]["maxLength"] == 128 and "pattern" not in props["pattern"]
    assert props["type"]["pattern"] == "^[a-z]+$"
    assert set(props) == {"pattern", "type", "anyOf"}


def test_shared_definitions_are_named_after_core_types():
    generated = schema.input_schema(catalogue.get("nx_create_datum_axis"))
    assert generated["properties"]["origin"] == {"$ref": "#/$defs/Point3"}
    assert generated["$defs"]["Point3"]["maxItems"] == 3
    plain = schema.input_schema(SAMPLE)
    assert "$defs" not in plain or all(not k[0].isdigit() for k in plain["$defs"])


def test_unit_strings_refs_defaults_and_call_keys():
    generated = schema.input_schema(SAMPLE)
    props = generated["properties"]
    length = props["length"]
    length = generated["$defs"][length["$ref"].split("/")[-1]] if "$ref" in length else length
    assert length["type"] == ["number", "string"] and length["maxLength"] == T_QUANTITY_MAX
    assert "mm" in length["description"] and "deg" in props["angle"]["description"]
    assert props["target"]["description"].startswith("Ref") and "pattern" not in props["target"]
    assert props["label"]["pattern"] == "^[a-z]+$"  # a tool's own pattern stays listed
    assert props["count"]["default"] == 1 and props["count"]["maximum"] == 9
    assert props["points"]["maxItems"] == 4 and props["points"]["items"]["maxItems"] == 2
    assert "null" not in json.dumps(props["plane"])  # optional fields are documented by omission
    assert generated["required"] == ["kind"]
    assert {"operation_id", "intent"} <= set(props)
    assert "operation_id" not in schema.input_schema(SAMPLE, call_keys=False)["properties"]
    reader = catalogue.get("nx_list_objects")
    assert "operation_id" not in schema.input_schema(reader)["properties"]


def test_variant_schema_narrows_the_variant_field():
    narrowed = schema.variant_schema(SAMPLE, "b")
    assert narrowed["properties"]["kind"] == {"const": "b"}
    with pytest.raises(KeyError):
        schema.variant_schema(SAMPLE, "z")


def test_output_schema_describes_the_envelope_and_data():
    out = schema.output_schema(catalogue.get("nx_create_datum_plane"))
    data = out["properties"]["data"]["properties"]
    assert {"feature", "datum_plane", "origin", "normal"} <= set(data)
    assert "rolled_back" in out["properties"]["mutation_outcome"]["enum"]


@pytest.mark.parametrize(
    "args, pointer",
    [
        ({"kind": "a", "points": [[1, 2], [3]]}, "/points/1"),
        ({"kind": "a", "pose": {"translation": [1, 2]}}, "/pose/translation"),
        ({"kind": "a", "length": "3 deg"}, "/length"),
        ({}, "/kind"),
        ({"kind": "a", "bogus": 1}, "/bogus"),
    ],
)
def test_error_pointer_skips_union_tags(args, pointer):
    with pytest.raises(Exception) as caught:
        schema.adapter(SAMPLE).validate_python(args)
    first = caught.value.errors()[0]
    assert schema.error_pointer(args, first["loc"], first["type"]).startswith(pointer)


def test_every_catalogue_tool_converts():
    for spec in catalogue.load().values():
        assert schema.adapter(spec) is schema.adapter(spec)  # cached
        assert schema.input_schema(spec)
    assert list(itertools.islice(samples(SAMPLE), 1)) == [{"kind": "a"}]
