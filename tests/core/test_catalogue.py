"""Import-time catalogue rules, hooks, summaries and the loaded registry."""

from typing import Annotated, Literal, TypedDict

import pytest

from nx_mcp.core import catalogue, summary
from nx_mcp.core.catalogue import CatalogueError, make_spec
from nx_mcp.core.types import ByVariant, Doc, Items, Json, Length, PathTo, RefTo, Text


class Out(TypedDict):
    n: int


KW = ("one", "two", "three")


def nx_create_blend(
    tx, *, size: Length, edges: Annotated[list[Annotated[str, RefTo("edge")]], Items(1, 5)]
) -> Out:
    """Blend edges."""
    return {"n": 1}


ok_tool = nx_create_blend


def spec(fn, **options):
    options = {
        "effect": "model",
        "label": "Blend",
        "keywords": KW,
        "domain": "modeling",
        "example": {"size": 1, "edges": ["e:1"]} if "size" in fn.__annotations__ else {},
        **options,
    }
    return make_spec(fn, **options)


def test_valid_spec_derives_fields():
    s = spec(ok_tool, summary="Blend {edges} R{size}", example={"size": 1, "edges": ["e:1"]})
    assert s.name == "nx_create_blend" and s.domain == "modeling"
    assert s.refresh_annotations and s.invalidates == "topology" and s.cost == "light"
    rendered = summary.render(s, {"size": "2 mm", "edges": ["e:1", "e:2"]}, strict=True)
    assert rendered == "Blend 2 edges R2 mm"
    assert summary.render(s, {"size": 3, "edges": ["e:1"]}, units={"length": "in"}) == (
        "Blend 1 edge R3 in"
    )


def _documented(doc: str, field_doc: str = "Size"):
    def nx_create_sized(tx, *, size: Annotated[int, Doc(field_doc)]) -> Out:
        return {"n": 1}

    nx_create_sized.__doc__ = doc
    return nx_create_sized


def test_text_limits_follow_tool_surface_1_7():
    """Description at most 500 characters (first sentence 120), field doc at most 120."""
    first = "Size the sample " + "x" * (catalogue.FIRST_SENTENCE_MAX - 17) + "."
    assert len(first) == catalogue.FIRST_SENTENCE_MAX
    whole = first + " " + "y" * (catalogue.DESCRIPTION_MAX - len(first) - 2) + "."
    assert len(whole) == catalogue.DESCRIPTION_MAX
    spec(_documented(whole, "z" * catalogue.FIELD_DOC_MAX), example={"size": 1})
    for doc, field_doc, message in (
        ("Size " + "x" * catalogue.FIRST_SENTENCE_MAX + ".", "Size", "first sentence"),
        (whole + " More.", "Size", "exceeds 500"),
        ("Size it.", "z" * (catalogue.FIELD_DOC_MAX + 1), "Doc longer than 120"),
    ):
        with pytest.raises(CatalogueError, match=message):
            spec(_documented(doc, field_doc), example={"size": 1})


def _named(fn, name):
    fn.__name__ = name
    return fn


@pytest.mark.parametrize(
    ("factory", "options", "message"),
    [
        (lambda: _named(lambda tx, *, size: None, "nx_blend_edges"), {}, "verb"),
        (lambda: ok_tool, {"effect": "write"}, "effect"),
        (lambda: ok_tool, {"requires": ("sketch",)}, "requires"),
        (lambda: ok_tool, {"label": "x" * 49}, "label"),
        (lambda: ok_tool, {"keywords": ("a",)}, "keywords"),
        (lambda: ok_tool, {"summary": "Blend {radius}"}, "unknown parameters"),
        (lambda: ok_tool, {"batchable": True, "cost": "heavy"}, "batchable"),
        (lambda: ok_tool, {"cost": "instant"}, "unknown cost"),  # merged into light
        (lambda: ok_tool, {"previewable": True, "effect": "file"}, "previewable"),
        (lambda: ok_tool, {"variants": "size"}, "Literal"),
        (lambda: ok_tool, {"example": {"size": "3 deg", "edges": ["e:1"]}}, "example"),
    ],
)
def test_rule_violations_raise(factory, options, message):
    fn = factory()
    if fn is ok_tool:
        fn = _copy(ok_tool)
    with pytest.raises(CatalogueError, match=message):
        spec(fn, **options)


def _copy(fn):
    import types

    out = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__, fn.__defaults__)
    out.__kwdefaults__ = fn.__kwdefaults__
    out.__annotations__ = dict(fn.__annotations__)
    out.__doc__ = fn.__doc__
    out.__qualname__ = fn.__qualname__
    return out


def test_signature_rules():
    def nx_create_a(tx, size: Length) -> Out:
        """Doc."""

    def nx_create_b(tx, *, items: list[Length]) -> Out:
        """Doc."""

    def nx_create_c(tx, *, text: str) -> Out:
        """Doc."""

    def nx_create_d(tx, *, mode: Literal["a", "b"]) -> dict:
        """Doc."""

    def nx_create_e(tx, *, x: int) -> Out:
        """Doc. """ + "x" * 300

    for fn, message in [
        (nx_create_a, "keyword-only"),
        (nx_create_b, "Items"),
        (nx_create_c, "Text"),
        (nx_create_d, "TypedDict"),
    ]:
        with pytest.raises(CatalogueError, match=message):
            spec(fn)


def test_read_tool_cannot_take_new_path_and_summary_map_needs_variants():
    def nx_get_a(tx, *, path: Annotated[str, PathTo("new")]) -> Out:
        """Doc."""

    with pytest.raises(CatalogueError, match="read tool"):
        spec(nx_get_a, effect="read")

    def nx_create_v(tx, *, method: Literal["a", "b"], t: Annotated[str, Text(5)] = "x") -> Out:
        """Doc."""

    with pytest.raises(CatalogueError, match="variant"):
        spec(nx_create_v, summary={"a": "A", "b": "B"})
    with pytest.raises(CatalogueError, match="keys"):
        spec(nx_create_v, summary={"a": "A"}, variants="method", example={"method": "a"})
    ok = spec(
        nx_create_v, summary={"a": "A", "b": "B {t}"}, variants="method", example={"method": "a"}
    )
    assert ok.variant_values() == ("a", "b")
    assert summary.render(ok, {"method": "b", "t": "q"}) == "B q"


def test_tools_must_live_in_domains_and_names_are_unique():
    with pytest.raises(CatalogueError, match="nx_mcp.domains"):
        make_spec(ok_tool, effect="read", label="x", keywords=KW)
    first = catalogue.register(spec(_named(_copy(ok_tool), "nx_create_dup")))
    try:
        other = _named(_copy(ok_tool), "nx_create_dup")
        other.__qualname__ = "elsewhere"
        with pytest.raises(CatalogueError, match="twice"):
            catalogue.register(spec(other))
    finally:
        catalogue.unregister(first.name)


def test_hook_kinds_are_checked():
    with pytest.raises(CatalogueError, match="unknown object kind"):
        catalogue.lister("widget")


def test_summary_never_raises():
    s = spec(_named(_copy(ok_tool), "nx_create_blend"), summary=lambda a, say: 1 / 0)
    assert summary.render(s, {"size": 1, "edges": ["e:1"]}) == "Blend"
    long = spec(_named(_copy(ok_tool), "nx_create_blend"), summary="Blend {edges}" + "!" * 80)
    assert len(summary.render(long, {"size": 1, "edges": ["e:1"]})) == 60


def test_loaded_registry_is_valid():
    registry = catalogue.load()
    assert {"nx_create_datum_plane", "nx_list_objects"} <= set(registry)
    for s in registry.values():
        assert s.domain in catalogue.DOMAINS
        rendered = summary.render(s, dict(s.example), strict=True)
        assert rendered and rendered != s.label and len(rendered) <= 60
    assert {"body", "face", "edge", "feature", "datum", "sketch"} <= set(catalogue.hooks("lister"))


class _FormA(TypedDict):
    size: Length


class _FormB(TypedDict, total=False):
    edges: Annotated[list[Annotated[str, RefTo("edge")]], Items(1, 5)]


_FORMS = {"a": _FormA, "b": _FormB}


def nx_create_by(
    tx,
    *,
    method: Literal["a", "b"],
    p: Annotated[dict, Json(), ByVariant("method", _FORMS), Doc("Fields per method")],
) -> Out:
    """Doc."""


def test_by_variant_forms_are_checked_by_gate_1_and_listed_per_variant():
    from nx_mcp.core.errors import NXError
    from nx_mcp.core.validate import validate
    from nx_mcp.sidecar import schema

    ok = spec(nx_create_by, variants="method", example={"method": "a", "p": {"size": 2}})
    checked = validate(ok.params, {"method": "a", "p": {"size": "2 mm"}})
    assert checked.quantities == [(("p", "size"), "length")]
    with pytest.raises(NXError, match="/p/edges: unknown field"):
        validate(ok.params, {"method": "a", "p": {"size": 1, "edges": ["e:1"]}})
    assert validate(ok.params, {"method": "b", "p": {}}).args == {"method": "b", "p": {}}
    listed = schema.input_schema(ok, call_keys=False)["properties"]["p"]
    assert listed["type"] == "object" and "properties" not in listed
    narrowed = schema.variant_schema(ok, "a", call_keys=False)["properties"]
    assert narrowed["method"] == {"const": "a"} and narrowed["p"]["required"] == ["size"]
    assert not schema.adapter(ok).validate_python({"method": "b", "p": {}})["p"]
    with pytest.raises(ValueError):
        schema.adapter(ok).validate_python({"method": "a", "p": {}})
    # The forms must be keyed by the variant field's values.
    with pytest.raises(CatalogueError, match="ByVariant"):
        spec(nx_create_by, example={"method": "a", "p": {"size": 2}})


def nx_create_kind(tx, *, size: Length, kind: Literal["a", "b"] = "b") -> Out:
    """Doc."""


def test_variant_of_uses_the_default_of_an_omitted_variant_field():
    s = spec(nx_create_kind, variants="kind", example={"size": 1})
    assert s.variant_of({"size": 1}) == "b"  # omitted: the default variant runs
    assert s.variant_of({"size": 1, "kind": None}) == "b"
    assert s.variant_of({"size": 1, "kind": "a"}) == "a"
    required = spec(nx_create_by, variants="method", example={"method": "a", "p": {"size": 2}})
    assert required.variant_of({}) is None


def test_cost_classes_are_light_heavy_and_long():
    """``instant`` was merged into ``light``: both had the same budget and panel text."""
    assert catalogue.COSTS == ("light", "heavy", "long")
    assert {s.cost for s in catalogue.load().values()} <= set(catalogue.COSTS)
