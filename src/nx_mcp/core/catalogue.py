"""``@tool``, ``ToolSpec`` and the registry (architecture.md 1.2).

A tool is one decorated function: its signature is the schema, its docstring the
description, and the decorator carries everything else. Every rule below is
checked at import; a violation raises :class:`CatalogueError`, so the sidecar
refuses to start and ``dev_reload.py`` prints the error.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import string
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from nx_mcp.core.types import (
    KIND_GROUPS,
    KINDS,
    SHEET_HINTS,
    ByVariant,
    Doc,
    Items,
    Json,
    PathArg,
    Quantity,
    RefKind,
    Text,
)
from nx_mcp.core.validate import is_typeddict, is_union, typeddict_fields, unwrap, validate

EFFECTS = ("read", "view", "model", "file", "session")
COSTS = ("instant", "light", "heavy", "long")
REQUIRES = ("work_part", "no_active_sketch", "drawing_sheet", "assembly", "interactive")
APPLICATIONS = ("modeling", "sheet_metal", "drafting")
VERBS = frozenset(
    {
        "list",
        "get",
        "find",
        "measure",
        "analyze",
        "check",
        "resolve",
        "create",
        "edit",
        "delete",
        "set",
        "update",
        "activate",
        "finish",
        "open",
        "close",
        "save",
        "import",
        "export",
        "upload",
        "download",
        "capture",
        "restore",
        "undo",
        "preview",
        "run",
        "cancel",
    }
)
GATEWAYS = frozenset({"nx_invoke", "nx_invoke_read"})
GEOMETRY_DOMAINS = frozenset({"modeling", "sketch", "freeform", "sheet_metal", "assembly"})
DOMAINS = frozenset(
    {
        "session",
        "parts_files",
        "sketch",
        "modeling",
        "freeform",
        "assembly",
        "inspection",
        "drawing",
        "sheet_metal",
        "display",
    }
)
HOOKS = ("lister", "deleter", "definition", "refresher", "sheet")
_DOMAIN_PACKAGE = "nx_mcp.domains."


class CatalogueError(Exception):
    """A tool or hook definition breaks a catalogue rule."""


@dataclass(frozen=True)
class Param:
    name: str
    hint: Any
    default: Any
    required: bool


@dataclass(frozen=True, eq=False)
class ToolSpec:
    name: str
    domain: str
    effect: str
    label: str
    summary: Any
    description: str
    keywords: tuple[str, ...]
    params: Mapping[str, Param]
    returns: Any
    requires: tuple[str, ...]
    batchable: bool
    previewable: bool
    paths: tuple[tuple[str, str, tuple[str, ...]], ...]
    variants: Any
    refresh_annotations: bool
    invalidates: str
    cost: str
    runs: str
    example: Mapping[str, Any]
    handler: Callable[..., Any] = field(repr=False)
    # Derived: an argument is in the target drawing sheet's units (gate 9).
    sheet: bool = False
    # Decorator, view tools only: a success returns restore_id for nx_restore_display.
    restorable: bool = False
    # Decorator, model tools: the `new` .prt path parameter that, when given, makes the
    # call work in a part it creates, so gate 6 does not check the current work part.
    new_part: str | None = None

    @property
    def evidence(self) -> dict:
        from nx_mcp.core import evidence

        return evidence.for_tool(self.name)

    def variant_of(self, args: Mapping) -> str | None:
        """The value of the variant field in ``args``, else its default (None without
        variants), so an omitted defaulted field is gated and remembered as the
        variant that actually runs."""
        if isinstance(self.variants, str):
            value = args.get(self.variants)
            if value is None:
                param = self.params.get(self.variants)
                value = None if param is None or param.required else param.default
            return value if isinstance(value, str) else None
        return None

    def variant_values(self) -> tuple[str, ...]:
        if isinstance(self.variants, str):
            base, _ = unwrap(self.params[self.variants].hint)
            if is_union(base):
                base = next(a for a in typing.get_args(base) if a is not type(None))
                base, _ = unwrap(base)
            return tuple(typing.get_args(base))
        if isinstance(self.variants, Mapping):
            return tuple(self.variants)
        return ()


_REGISTRY: dict[str, ToolSpec] = {}
_HOOKS: dict[str, dict[str, Callable]] = {name: {} for name in HOOKS}
_LOADED = False


# ---------------------------------------------------------------------------
# Description and summary rules


def normalise_doc(doc: str | None) -> str:
    return " ".join((doc or "").split())


def first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return match.group(1) if match else text


def summary_fields(template: str) -> list[str]:
    names = []
    for _, name, _, _ in string.Formatter().parse(template):
        if name:
            names.append(re.split(r"[.\[]", name, maxsplit=1)[0])
    return names


# ---------------------------------------------------------------------------
# Type checks


def _check_hint(hint, where: str, problems: list[str], *, seen=None) -> None:
    seen = seen if seen is not None else set()
    base, markers = unwrap(hint)
    doc = markers.get(Doc)
    if doc is not None and len(doc.text) > 80:
        problems.append(f"{where}: Doc longer than 80 characters")
    if RefKind in markers:
        unknown = [k for k in markers[RefKind].kinds if k not in KINDS and k not in KIND_GROUPS]
        if unknown:
            problems.append(f"{where}: unknown reference kind {unknown}")
    if Quantity in markers:
        return
    if base is dict and Json in markers:
        return
    if is_union(base):
        for alt in typing.get_args(base):
            _check_hint(alt, where, problems, seen=seen)
        return
    if base is type(None) or base in (bool, int, float):
        return
    if typing.get_origin(base) is Literal:
        if not all(isinstance(v, (str, int, bool)) for v in typing.get_args(base)):
            problems.append(f"{where}: Literal values must be str, int or bool")
        return
    if base is str:
        if Text not in markers:
            problems.append(f"{where}: string without Text(max) bound")
        return
    origin = typing.get_origin(base)
    if origin is list:
        if Items not in markers:
            problems.append(f"{where}: list without Items(min, max)")
        (item,) = typing.get_args(base)
        _check_hint(item, where + "/*", problems, seen=seen)
        return
    if origin is tuple:
        items = typing.get_args(base)
        if not items or items[-1] is Ellipsis:
            problems.append(f"{where}: tuple must have a fixed length")
            return
        for i, item in enumerate(items):
            _check_hint(item, f"{where}/{i}", problems, seen=seen)
        return
    if is_typeddict(base):
        if base in seen:
            return
        seen.add(base)
        for name, sub in typeddict_fields(base).items():
            _check_hint(sub, f"{where}/{name}", problems, seen=seen)
        return
    problems.append(f"{where}: {base!r} is not built from core types")


def _collect_paths(hint, where: str, out: list, seen=None) -> None:
    seen = seen if seen is not None else set()
    base, markers = unwrap(hint)
    if PathArg in markers:
        marker = markers[PathArg]
        out.append((where, marker.mode, marker.suffixes))
        return
    if is_union(base):
        for alt in typing.get_args(base):
            _collect_paths(alt, where, out, seen)
    elif typing.get_origin(base) is list:
        _collect_paths(typing.get_args(base)[0], where + "/*", out, seen)
    elif typing.get_origin(base) is tuple:
        for i, item in enumerate(typing.get_args(base)):
            _collect_paths(item, f"{where}/{i}", out, seen)
    elif is_typeddict(base) and base not in seen:
        seen.add(base)
        for name, sub in typeddict_fields(base).items():
            _collect_paths(sub, f"{where}/{name}", out, seen)


def _uses_sheet(hint, seen=None) -> bool:
    """True when ``hint`` contains a sheet-unit type (``SheetPoint``, ``SheetLength``)."""
    seen = seen if seen is not None else set()
    if any(hint == sheet for sheet in SHEET_HINTS):
        return True
    base, markers = unwrap(hint)
    quantity = markers.get(Quantity)
    if quantity is not None:
        return quantity.dim == "sheet_length"
    if is_union(base) or typing.get_origin(base) in (list, tuple):
        return any(_uses_sheet(a, seen) for a in typing.get_args(base) if a is not Ellipsis)
    if is_typeddict(base) and base not in seen:
        seen.add(base)
        return any(_uses_sheet(h, seen) for h in typeddict_fields(base).values())
    return False


def _literal_values(hint) -> tuple | None:
    base, _ = unwrap(hint)
    if is_union(base):
        alts = [a for a in typing.get_args(base) if a is not type(None)]
        if len(alts) != 1:
            return None
        base, _ = unwrap(alts[0])
    if typing.get_origin(base) is Literal:
        return typing.get_args(base)
    return None


# ---------------------------------------------------------------------------
# The decorator


def make_spec(
    fn: Callable,
    *,
    effect: str,
    label: str,
    summary: Any = None,
    keywords: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    variants: Any = None,
    cost: str = "light",
    batchable: bool = False,
    previewable: bool = False,
    invalidates: str = "topology",
    refresh_annotations: bool | None = None,
    runs: str = "nx",
    example: Mapping[str, Any] | None = None,
    domain: str | None = None,
    restorable: bool = False,
    new_part: str | None = None,
) -> ToolSpec:
    """Build and check a :class:`ToolSpec` without registering it."""
    name = fn.__name__
    problems: list[str] = []
    module = getattr(fn, "__module__", "") or ""
    if domain is None:
        if not module.startswith(_DOMAIN_PACKAGE):
            raise CatalogueError(f"{name}: tools live in nx_mcp.domains.<domain> (got {module})")
        domain = module[len(_DOMAIN_PACKAGE) :].split(".")[0]
    if domain not in DOMAINS:
        problems.append(f"unknown domain {domain!r}")
    # Name and verb (surface 1.2).
    if not re.fullmatch(r"nx_[a-z0-9]+(_[a-z0-9]+)*", name):
        problems.append("name must be nx_<verb>_<object> in lower snake case")
    elif name not in GATEWAYS:
        parts = name.split("_")
        if len(parts) < 3 or parts[1] not in VERBS:
            problems.append(f"verb {parts[1]!r} is not in the closed verb list")
    if effect not in EFFECTS:
        problems.append(f"unknown effect {effect!r}")
    if cost not in COSTS:
        problems.append(f"unknown cost {cost!r}")
    if runs not in ("nx", "sidecar"):
        problems.append(f"unknown runs {runs!r}")
    if invalidates not in ("topology", "deleted"):
        problems.append(f"unknown invalidates {invalidates!r}")
    for item in requires:
        if item.startswith("application:"):
            if item.split(":", 1)[1] not in APPLICATIONS:
                problems.append(f"unknown application in requires {item!r}")
        elif item not in REQUIRES:
            problems.append(f"unknown requires {item!r}")
    # Budgets (surface 1.7).
    if not label or len(label) > 48:
        problems.append("label must be 1-48 characters")
    description = normalise_doc(fn.__doc__)
    if not description:
        problems.append("the docstring is the description and is required")
    elif len(first_sentence(description)) > 120:
        problems.append("description first sentence exceeds 120 characters")
    if len(description) > 300:
        problems.append("description exceeds 300 characters")
    keywords = tuple(keywords)
    if not 3 <= len(keywords) <= 8 or len(set(keywords)) != len(keywords):
        problems.append("keywords: 3-8 distinct entries")
    if not all(isinstance(k, str) and k.strip() for k in keywords):
        problems.append("keywords must be non-empty strings")
    # Signature: tx first, then keyword-only core-typed parameters.
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception as error:
        raise CatalogueError(f"{name}: cannot resolve annotations: {error}") from error
    signature = inspect.signature(fn)
    sig_params = list(signature.parameters.values())
    if (
        not sig_params
        or sig_params[0].name != "tx"
        or sig_params[0].kind
        not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ):
        problems.append("the first parameter must be positional 'tx'")
    params: dict[str, Param] = {}
    for p in sig_params[1:]:
        if p.kind is not inspect.Parameter.KEYWORD_ONLY:
            problems.append(f"parameter {p.name} must be keyword-only")
            continue
        if p.name not in hints:
            problems.append(f"parameter {p.name} has no annotation")
            continue
        _check_hint(hints[p.name], "/" + p.name, problems)
        params[p.name] = Param(
            p.name,
            hints[p.name],
            None if p.default is inspect.Parameter.empty else p.default,
            p.default is inspect.Parameter.empty,
        )
    returns = hints.get("return")
    if not is_typeddict(returns):
        problems.append("the return annotation must be a TypedDict")
    if problems:
        raise CatalogueError(f"{name}: " + "; ".join(problems))
    # Defaults and example validate.
    for p in params.values():
        if not p.required and p.default is not None:
            try:
                validate({p.name: p}, {p.name: p.default})
            except Exception as error:
                problems.append(f"default of {p.name} does not validate: {error}")
    example = dict(example or {})
    try:
        validate(params, example)
    except Exception as error:
        problems.append(f"example does not validate: {error}")
    # Variants and summary.
    variant_values: tuple = ()
    if isinstance(variants, str):
        values = _literal_values(params[variants].hint) if variants in params else None
        if values is None:
            problems.append(f"variants must name a Literal parameter (got {variants!r})")
        else:
            variant_values = values
    elif variants is not None and not isinstance(variants, Mapping):
        problems.append("variants is a field name or a map of sub-schemas")
    for param in params.values():
        base, markers = unwrap(param.hint)
        by = markers.get(ByVariant)
        if by is None:
            continue
        if base is not dict or Json not in markers or by.field != variants:
            problems.append(f"{param.name}: ByVariant goes on a Json object keyed by the variants")
        elif set(by.forms) != set(variant_values):
            problems.append(f"{param.name}: ByVariant forms must equal the variant values")
        for value, form in by.forms.items():
            if not is_typeddict(form):
                problems.append(f"{param.name}#{value}: a ByVariant form is a TypedDict")
            else:
                _check_hint(form, f"/{param.name}#{value}", problems)
    templates: list[str] = []
    if isinstance(summary, str):
        templates = [summary]
    elif isinstance(summary, Mapping):
        if not variant_values:
            problems.append("a summary map needs variants naming a Literal field")
        elif set(summary) != set(variant_values):
            problems.append("summary map keys must equal the variant values")
        templates = list(summary.values())
    elif summary is not None and not callable(summary):
        problems.append("summary is a format string, a map by variant or a callable")
    for template in templates:
        try:
            names = summary_fields(template)
        except ValueError as error:
            problems.append(f"bad summary template {template!r}: {error}")
            continue
        missing = [n for n in names if n not in params]
        if missing:
            problems.append(f"summary names unknown parameters {missing}")
    # Admission rules.
    if batchable and (effect != "model" or cost not in ("instant", "light")):
        problems.append("batchable requires effect 'model' and cost instant/light")
    if previewable and effect != "model":
        problems.append("previewable requires effect 'model'")
    if restorable and effect != "view":
        problems.append("restorable requires effect 'view'")
    paths: list = []
    for p in params.values():
        _collect_paths(p.hint, "/" + p.name, paths)
    if effect == "read" and any(mode in ("new", "save") for _, mode, _ in paths):
        problems.append("a read tool cannot take a new or save path")
    if new_part is not None and (
        effect != "model"
        or not any(
            where == "/" + new_part and mode == "new" and ".prt" in suffixes
            for where, mode, suffixes in paths
        )
    ):
        problems.append("new_part must name a model tool's new .prt path parameter")
    if problems:
        raise CatalogueError(f"{name}: " + "; ".join(problems))
    if refresh_annotations is None:
        refresh_annotations = effect == "model" and domain in GEOMETRY_DOMAINS
    return ToolSpec(
        name=name,
        domain=domain,
        effect=effect,
        label=label,
        summary=summary,
        description=description,
        keywords=keywords,
        params=MappingProxyType(params),
        returns=returns,
        requires=tuple(requires),
        batchable=batchable,
        previewable=previewable,
        paths=tuple(paths),
        variants=variants,
        refresh_annotations=bool(refresh_annotations),
        invalidates=invalidates,
        cost=cost,
        runs=runs,
        example=MappingProxyType(example),
        handler=fn,
        sheet=any(_uses_sheet(p.hint) for p in params.values()),
        restorable=bool(restorable),
        new_part=new_part,
    )


def register(spec: ToolSpec) -> ToolSpec:
    existing = _REGISTRY.get(spec.name)
    if existing is not None and (
        existing.handler.__module__,
        existing.handler.__qualname__,
    ) != (spec.handler.__module__, spec.handler.__qualname__):
        raise CatalogueError(f"{spec.name}: defined twice ({existing.handler.__module__})")
    _REGISTRY[spec.name] = spec
    return spec


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def tool(**options) -> Callable[[Callable], Callable]:
    """Register the decorated function as a catalogue tool (see :func:`make_spec`)."""

    def decorate(fn: Callable) -> Callable:
        spec = register(make_spec(fn, **options))
        fn.spec = spec  # type: ignore[attr-defined]
        return fn

    return decorate


# ---------------------------------------------------------------------------
# Cross-domain hooks


def _hook(kind_of_hook: str, key: str) -> Callable[[Callable], Callable]:
    if (
        kind_of_hook in ("lister", "deleter", "refresher", "sheet")
        and key not in KINDS
        and key not in KIND_GROUPS
    ):
        raise CatalogueError(f"@{kind_of_hook}({key!r}): unknown object kind")
    if not isinstance(key, str) or not key:
        raise CatalogueError(f"@{kind_of_hook} needs a non-empty key")

    def decorate(fn: Callable) -> Callable:
        table = _HOOKS[kind_of_hook]
        existing = table.get(key)
        if existing is not None and (existing.__module__, existing.__qualname__) != (
            fn.__module__,
            fn.__qualname__,
        ):
            raise CatalogueError(f"@{kind_of_hook}({key!r}) is registered twice")
        table[key] = fn
        return fn

    return decorate


def lister(kind: str):
    """Rows of ``kind`` for ``nx_list_objects``: ``fn(tx, owner, filters) -> list``."""
    return _hook("lister", kind)


def deleter(kind: str):
    """Delete objects of ``kind`` for ``nx_delete_objects``."""
    return _hook("deleter", kind)


def definition(feature_type: str):
    """Replace a non-expression definition for ``nx_edit_feature(definition=...)``."""
    return _hook("definition", feature_type)


def refresher(kind: str):
    """Refresh managed annotations of ``kind`` (``nx_update_annotations``, gate 13)."""
    return _hook("refresher", kind)


def sheet_locator(kind: str):
    """The drawing sheet an object of ``kind`` lies on, for gate 9 (``services.sheet_of``):
    ``fn(services, obj) -> sheet or None``. Sheets and views are known to the host."""
    return _hook("sheet", kind)


def hooks(kind_of_hook: str) -> Mapping[str, Callable]:
    return MappingProxyType(_HOOKS[kind_of_hook])


# ---------------------------------------------------------------------------
# Registry access


def load() -> Mapping[str, ToolSpec]:
    """Import every module under ``nx_mcp.domains`` once and return the registry."""
    global _LOADED
    if not _LOADED:
        package = importlib.import_module("nx_mcp.domains")
        for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
            importlib.import_module(info.name)
        _LOADED = True
    return MappingProxyType(_REGISTRY)


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def specs() -> list[ToolSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: s.name)
