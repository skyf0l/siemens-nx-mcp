"""One-line, engineer-readable summaries of queued bridge requests.

Standard library only: the NX-side control panel imports this. Summaries are
built from the request's real arguments, so they describe what will run. Object
IDs are shown by their registered NX name; paths relative to the workspace.
Every public tool has a template: model changes read as actions with their key
values, read-only tools as inspections ("Check ...", "Measure ...", "List ...").
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_LENGTH = 90


def _number(value: Any) -> str:
    if value is None:
        return "?"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    return f"{value:g}"


def humanize(method: str) -> str:
    """nx_list_topology -> List topology."""
    words = method.removeprefix("nx_").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else method


def _clip(text: str, width: int) -> str:
    width = max(width, 8)
    return text if len(text) <= width else text[: width - 1] + "…"


def _plural(noun: str, n: int) -> str:
    if n == 1:
        return noun
    if noun.endswith("y") and noun[-2:-1] not in tuple("aeiou"):
        return noun[:-1] + "ies"
    if noun.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    return noun + "s"


def _a(word: Any) -> str:
    word = _words(word)
    return ("an " if word[:1].lower() in tuple("aeiou") else "a ") + word


def _more(n: int) -> str:
    return f" (+{n} more)" if n > 0 else ""


def _words(value: Any) -> str:
    return str(value).replace("_", " ")


def _point(value: Any) -> str:
    """[10, 20, 0] -> (10, 20, 0)."""
    if not isinstance(value, list | tuple):
        return "a point"
    return "(" + ", ".join(_number(v) for v in value) + ")"


def _direction(value: Any) -> str:
    """[0, 0, -1] -> -Z; other vectors as (x, y, z)."""
    try:
        vector = [float(v) for v in value]
    except (TypeError, ValueError):
        return "a direction"
    for index, axis in enumerate("XYZ"[: len(vector)]):
        others = [v for i, v in enumerate(vector) if i != index]
        if vector[index] and not any(others):
            return ("+" if vector[index] > 0 else "-") + axis
    return _point(vector)


def _scale(value: Any) -> str:
    """2 -> 2:1, 0.5 -> 1:2."""
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{scale:g}:1" if scale >= 1 else f"1:{1 / scale:g}"


def _signed(value: Any) -> str:
    return ("+" if isinstance(value, int | float) and value > 0 else "") + _number(value)


def _xy(value: Any) -> tuple[float, float]:
    if isinstance(value, dict):
        return float(value["x"]), float(value["y"])
    return float(value[0]), float(value[1])


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _listed(parts: list[str], keep: int = 2) -> str:
    """Join the first parts and count the rest: "a, b (+2 more)"."""
    return ", ".join(parts[:keep]) + _more(len(parts) - keep)


class _Context:
    def __init__(self, name_of, path_of, units):
        self.name_of = name_of
        self.path_of = path_of
        self.unit = f" {units}" if units else ""
        self.describe: Callable[[str, dict[str, Any]], str] = lambda method, params: humanize(
            method
        )

    def obj(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            return "the work part"
        name = self.name_of(value)
        if name:
            return f'"{name}"'
        if value.startswith("obj_"):
            return "an object from before the last handover"
        return f'"{value}"'

    def count(self, values: Any, noun: str) -> str:
        n = len(values) if isinstance(values, list | tuple) else 0
        return f"{n} {_plural(noun, n)}"

    def some(self, values: Any, noun: str) -> str:
        """One object by name, several as a count."""
        if isinstance(values, list | tuple) and len(values) == 1:
            return self.obj(values[0])
        return self.count(values, noun)

    def length(self, value: Any) -> str:
        return _number(value) + self.unit

    def path(self, value: Any) -> str:
        return self.path_of(value) if isinstance(value, str) and value else "a file"

    def sketch(self, value: Any) -> str:
        return f"sketch {self.obj(value)}" if value else "the active sketch"

    def key(self, value: Any) -> str:
        """Expression or parameter key: an ID by its name, a name as typed."""
        text = str(value)
        if text.startswith("obj_"):
            return self.name_of(text) or "a parameter"
        return text

    def assignments(self, values: Any) -> str:
        if not isinstance(values, dict) or not values:
            return "no values"
        key, value = next(iter(values.items()))
        return f"{self.key(key)} = {_number(value)}" + _more(len(values) - 1)


# --- Formatting details ---------------------------------------------------------


def _tolerance(p, c):
    kind = p.get("tolerance_type")
    upper, lower = p.get("upper_tolerance"), p.get("lower_tolerance")
    if kind == "symmetric" and upper is not None:
        return f"±{c.length(upper)}"
    if kind == "bilateral" and upper is not None:
        return f"+{_number(upper)}/-{_number(abs(_float(lower)))}{c.unit}"
    if kind == "none":
        return "no tolerance"
    return f"{kind} tolerance" if kind else ""


def _capabilities(p, c):
    if p.get("tool"):
        return f"Check what {p['tool']} supports on this NX"
    if p.get("prefix"):
        return f"Check tested scope of {p['prefix']}* tools"
    return "Check tested scope of all tools"


_READ_ONLY_FIXED = {
    "nx_list_bodies": "List bodies in the work part",
    "nx_list_features": "List features in the work part",
    "nx_list_sketches": "List sketches in the work part",
    "nx_list_datums": "List datums in the work part",
    "nx_list_annotations": "List annotations in the work part",
    "nx_list_dimensions": "List dimensions in the work part",
    "nx_list_drawings": "List drawing sheets and views",
}

_TEMPLATES: dict[str, Callable[[dict[str, Any], _Context], str]] = {
    # Files, parts and session
    "nx_capabilities": _capabilities,
    # Measurements and inspections
    "nx_list_topology": lambda p, c: (
        f"List faces and edges of {c.obj(p.get('face') or p.get('body'))}"
    ),
    "nx_list_expressions": lambda p, c: (
        f'List expressions named like "{p["name_contains"]}"'
        if p.get("name_contains")
        else "List expressions in the work part"
    ),
}


def _fixed(text: str) -> Callable[[dict[str, Any], _Context], str]:
    return lambda p, c: text


_TEMPLATES.update({name: _fixed(text) for name, text in _READ_ONLY_FIXED.items()})


def _group_templates():
    """SUMMARIES contributed by feature-group schema modules."""
    global _GROUP_TEMPLATES
    if _GROUP_TEMPLATES is None:
        templates = {}
        try:
            from nx_mcp.feature_groups import server_modules

            for module in server_modules():
                templates.update(getattr(module, "SUMMARIES", {}))
        except Exception:
            pass  # A broken group module must not break the panel.
        _GROUP_TEMPLATES = templates
    return _GROUP_TEMPLATES


_GROUP_TEMPLATES = None


def has_template(method: str) -> bool:
    """True when the panel describes this tool with its own words."""
    return method in _TEMPLATES or method in _group_templates()


def summarize(
    method: str,
    params: dict[str, Any],
    *,
    name_of: Callable[[str], str | None] = lambda _id: None,
    path_of: Callable[[str], str] = lambda path: path,
    units: str | None = None,
) -> str:
    """Describe one request in a single line. Never raises."""
    context = _Context(name_of, path_of, units)

    def one(name: str, arguments: dict[str, Any]) -> str:
        try:
            template = _TEMPLATES.get(name) or _group_templates().get(name)
            text = template(arguments, context) if template else humanize(name)
            return " ".join(text.split())
        except Exception:
            return humanize(name)

    context.describe = one
    text = one(method, params or {})
    return _clip(text, MAX_LENGTH)


def relative_path(root: Path) -> Callable[[str], str]:
    """Show workspace paths relative to the workspace root, others unchanged."""

    def show(path: str) -> str:
        try:
            return Path(path).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return path

    return show
