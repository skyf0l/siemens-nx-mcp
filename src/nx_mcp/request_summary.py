"""One-line, engineer-readable summaries of queued bridge requests.

Standard library only: the NX-side control panel imports this. Summaries are
built from the request's real arguments, so they describe what will run. Object
IDs are shown by their registered NX name; paths relative to the workspace.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_LENGTH = 90


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    return f"{value:g}"


def humanize(method: str) -> str:
    """nx_measure_distance -> Measure distance."""
    words = method.removeprefix("nx_").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else method


class _Context:
    def __init__(self, name_of, path_of, units):
        self.name_of = name_of
        self.path_of = path_of
        self.unit = f" {units}" if units else ""

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
        return f"{n} {noun}" + ("" if n == 1 else "s")

    def length(self, value: Any) -> str:
        return _number(value) + self.unit

    def path(self, value: Any) -> str:
        return self.path_of(value) if isinstance(value, str) and value else "a file"


def _extrude(p, c):
    target = c.obj(p.get("sketch_id"))
    end = p.get("end_type", "distance")
    if end == "through_all":
        extent = "through all"
    elif end == "up_to_face":
        extent = "up to a face"
    elif p.get("symmetric"):
        extent = f"{c.length(p.get('distance'))} symmetric"
    else:
        extent = c.length(p.get("distance"))
    boolean = p.get("boolean", "none")
    tail = (
        "" if boolean in (None, "none") else f", {boolean} with {c.count(p.get('targets'), 'body')}"
    )
    return f"Extrude {target} {extent}{tail}"


def _rectangle(p, c):
    a, b = p.get("corner1") or {}, p.get("corner2") or {}
    try:
        size = f"{_number(abs(b['x'] - a['x']))} × {_number(abs(b['y'] - a['y']))}{c.unit} "
    except (KeyError, TypeError):
        size = ""
    return f"Add {size}rectangle to sketch {c.obj(p.get('sketch_id'))}"


def _arc(p, c):
    shape = (
        "circle" if abs(_float(p.get("end_angle")) - _float(p.get("start_angle"))) >= 360 else "arc"
    )
    return f"Add {shape} R{c.length(p.get('radius'))} to sketch {c.obj(p.get('sketch_id'))}"


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sketch(p, c):
    plane = "a custom plane" if p.get("origin") else f"the {p.get('plane', 'XY')} plane"
    name = f' "{p["name"]}"' if p.get("name") else ""
    return f"Create sketch{name} on {plane}"


def _batch(p, c, summarize_one):
    operations = p.get("operations") or []
    if not operations:
        return "Run an empty batch"
    first = summarize_one(operations[0].get("method", ""), operations[0].get("params") or {})
    more = f" (+{len(operations) - 1} more in the same step)" if len(operations) > 1 else ""
    return f"{first}{more}"


def _control(p, c):
    mode = p.get("mode", "status")
    return "Take control of NX" if mode == "agent" else humanize("nx_ui_control")


_TEMPLATES: dict[str, Callable[[dict[str, Any], _Context], str]] = {
    "nx_create_part": lambda p, c: f"Create part {c.path(p.get('path'))}",
    "nx_open_part": lambda p, c: f"Open {c.path(p.get('path'))}",
    "nx_save_part": lambda p, c: "Save the work part",
    "nx_save_as": lambda p, c: f"Save the work part as {c.path(p.get('path'))}",
    "nx_close_part": lambda p, c: (
        f"Close {c.obj(p.get('part'))}" + (" after saving" if p.get("save") else " without saving")
    ),
    "nx_export_step": lambda p, c: f"Export STEP to {c.path(p.get('path'))}",
    "nx_export_drawing_pdf": lambda p, c: f"Export drawing PDF to {c.path(p.get('path'))}",
    "nx_create_sketch": _sketch,
    "nx_finish_sketch": lambda p, c: f"Finish sketch {c.obj(p.get('sketch_id'))}",
    "nx_sketch_line": lambda p, c: f"Add a line to sketch {c.obj(p.get('sketch_id'))}",
    "nx_sketch_rectangle": _rectangle,
    "nx_sketch_arc": _arc,
    "nx_extrude": _extrude,
    "nx_revolve": lambda p, c: (
        f'Revolve sketch "{p.get("sketch_name")}" '
        f"{_number(p.get('angle', 360))}° about {p.get('axis', 'an axis')}"
    ),
    "nx_blend": lambda p, c: (
        f"Blend {c.count(p.get('edges'), 'edge')} R{c.length(p.get('radius'))}"
    ),
    "nx_chamfer": lambda p, c: (
        f"Chamfer {c.count(p.get('edges'), 'edge')} {c.length(p.get('offset'))}"
    ),
    "nx_hole": lambda p, c: (
        f"Drill hole Ø{c.length(p.get('diameter'))} × {c.length(p.get('depth'))} deep"
    ),
    "nx_boolean": lambda p, c: (
        f"{str(p.get('boolean_type', 'Boolean')).capitalize()} {c.count(p.get('targets'), 'body')}"
    ),
    "nx_pattern": lambda p, c: (
        f"Pattern {c.count(p.get('features'), 'feature')}: "
        f"{_number(p.get('count'))} × {c.length(p.get('spacing'))}"
    ),
    "nx_shell": lambda p, c: f"Shell {c.obj(p.get('body'))} {c.length(p.get('thickness'))} thick",
    "nx_draft": lambda p, c: f"Draft {c.count(p.get('faces'), 'face')} {_number(p.get('angle'))}°",
    "nx_thicken": lambda p, c: f"Thicken {c.count(p.get('faces'), 'face')}",
    "nx_delete_feature": lambda p, c: f"Delete feature {c.obj(p.get('name'))}",
    "nx_edit_feature": lambda p, c: f"Edit feature {c.obj(p.get('name'))}",
    "nx_set_expression": lambda p, c: f"Set {p.get('expression')} = {p.get('formula')}",
    "nx_add_component": lambda p, c: f"Add component {c.path(p.get('part_path'))}",
    "nx_reposition_component": lambda p, c: f"Move component {c.obj(p.get('component'))}",
    "nx_set_component_transform": lambda p, c: f"Place component {c.obj(p.get('component'))}",
    "nx_assembly_constraint": lambda p, c: (
        f"Add a {p.get('constraint_type', '')} constraint on {c.obj(p.get('component'))}"
    ),
    "nx_create_drawing": lambda p, c: f'Create drawing sheet "{p.get("name")}" ({p.get("size")})',
    "nx_add_base_view": lambda p, c: (
        f"Add a {p.get('view', 'base')} view to drawing {c.obj(p.get('drawing'))}"
    ),
    "nx_set_view": lambda p, c: f"Set the view to {p.get('orientation')}",
    "nx_fit_view": lambda p, c: "Fit the view",
    "nx_undo": lambda p, c: "Undo the agent's last operation",
    "nx_screenshot": lambda p, c: "Capture a screenshot",
    "nx_render_view": lambda p, c: "Render the view to an image",
    "nx_ui_control": _control,
}


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
            if name == "nx_batch":
                return _batch(arguments, context, one)
            template = _TEMPLATES.get(name)
            return template(arguments, context) if template else humanize(name)
        except Exception:
            return humanize(name)

    text = " ".join(one(method, params or {}).split())
    return text if len(text) <= MAX_LENGTH else text[: MAX_LENGTH - 1] + "…"


def relative_path(root: Path) -> Callable[[str], str]:
    """Show workspace paths relative to the workspace root, others unchanged."""

    def show(path: str) -> str:
        try:
            return Path(path).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return path

    return show
