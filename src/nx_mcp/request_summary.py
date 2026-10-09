"""One-line, engineer-readable summaries of queued bridge requests.

Standard library only: the NX-side control panel imports this. Summaries are
built from the request's real arguments, so they describe what will run. Object
IDs are shown by their registered NX name; paths relative to the workspace.
Every public tool has a template: model changes read as actions with their key
values, read-only tools as inspections ("Check ...", "Measure ...", "List ...").
"""

from __future__ import annotations

import math
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
    """nx_measure_distance -> Measure distance."""
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

    def scope(self, body: Any, scope: Any = None) -> str:
        if body:
            return self.obj(body)
        return "the whole assembly" if scope == "assembly" else "the work part"

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

    def steps(self, operations: Any, head: str) -> str:
        """head + first step's summary + (+N more), keeping the count visible."""
        operations = operations if isinstance(operations, list) else []
        first = operations[0] if operations and isinstance(operations[0], dict) else {}
        tail = _more(len(operations) - 1)
        text = self.describe(str(first.get("method", "")), first.get("params") or {})
        return head + _clip(text or "an unknown step", MAX_LENGTH - len(head) - len(tail)) + tail


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
    return f"Add {size}rectangle to {c.sketch(p.get('sketch_id'))}"


def _line(p, c):
    try:
        (x1, y1), (x2, y2) = _xy(p.get("start")), _xy(p.get("end"))
        size = f"{c.length(round(math.hypot(x2 - x1, y2 - y1), 3))} "
    except (KeyError, TypeError, ValueError, IndexError):
        size = ""
    return f"Add {size}line to {c.sketch(p.get('sketch_id'))}"


def _arc(p, c):
    shape = (
        "circle" if abs(_float(p.get("end_angle")) - _float(p.get("start_angle"))) >= 360 else "arc"
    )
    return f"Add {shape} R{c.length(p.get('radius'))} to {c.sketch(p.get('sketch_id'))}"


def _sketch(p, c):
    plane = "a custom plane" if p.get("origin") else f"the {p.get('plane', 'XY')} plane"
    name = f' "{p["name"]}"' if p.get("name") else ""
    return f"Create sketch{name} on {plane}"


def _datum_plane(p, c):
    method = p.get("method")
    if method == "offset":
        return (
            f"Create a datum plane {c.length(p.get('distance'))} from {c.obj(p.get('reference'))}"
        )
    if method == "three_points":
        return "Create a datum plane through three points"
    return "Create a datum plane through a point"


_CONTROL = {
    "agent": "Take control of NX",
    "manual": "Hand control of NX back to the engineer",
}


def _reposition(p, c):
    shifts = [f"{k[1].upper()}{_signed(p[k])}" for k in ("dx", "dy", "dz") if p.get(k)]
    turns = [f"{k[1].upper()}{_signed(p[k])}" for k in ("rx", "ry", "rz") if p.get(k)]
    text = f"Move component {c.obj(p.get('component'))}"
    if shifts:
        text += " " + " ".join(shifts) + c.unit
    if turns:
        text += (", rotate " if shifts else " rotate ") + " ".join(turns) + "°"
    return text


# --- Sketch edits, sheet metal and formatting details -----------------------


def _sketch_edit_step(op, c):
    action = op.get("action")
    if action == "add_line":
        try:
            (x1, y1), (x2, y2) = _xy(op.get("start")), _xy(op.get("end"))
            return f"add {c.length(round(math.hypot(x2 - x1, y2 - y1), 3))} line"
        except (TypeError, ValueError, IndexError):
            return "add a line"
    if action == "line":
        return f"move line {c.obj(op.get('curve'))}"
    if action == "arc":
        return f"reshape arc {c.obj(op.get('curve'))} to R{c.length(op.get('radius'))}"
    if action == "delete":
        return f"delete {c.obj(op.get('object'))}"
    if action == "constraint":
        kind = op.get("type")
        verb = "fix" if kind == "fixed" else "make"
        return f"{verb} {c.obj(op.get('curve'))}" + ("" if kind == "fixed" else f" {kind}")
    return _words(action or "edit")


def _edit_sketch(p, c):
    operations = p.get("operations") if isinstance(p.get("operations"), list) else []
    first = _sketch_edit_step(operations[0], c) if operations else "no changes"
    return f"Edit sketch {c.obj(p.get('sketch_id'))}: {first}" + _more(len(operations) - 1)


_SHEET_METAL_VERBS = {
    "convert": "Convert to sheet metal",
    "from_solid": "Make sheet metal from a solid",
    "unbend": "Unbend",
    "rebend": "Rebend",
    "edge_rip": "Rip edges",
    "break_corner": "Break corners",
    "resize_bend_angle": "Resize bend angle",
    "resize_bend_radius": "Resize bend radius",
    "resize_neutral_factor": "Resize neutral factor",
}
# Parameter -> how its value reads: "length" takes the part's units, "angle" degrees.
_SHEET_METAL_VALUES = (
    ("thickness", "{} thick", "length"),
    ("length", "{} long", "length"),
    ("first_flange_length", "{} long", "length"),
    ("flex_length", "{} long", "length"),
    ("height", "{} high", "length"),
    ("depth", "{} deep", "length"),
    ("cutout_depth", "{} deep", "length"),
    ("width", "{} wide", "length"),
    ("diameter", "Ø{}", "length"),
    ("bend_radius", "R{}", "length"),
    ("radius", "R{}", "length"),
    ("bend_angle", "{}°", "angle"),
    ("angle", "{}°", "angle"),
    ("neutral_factor", "neutral factor {}", "number"),
    ("value", "{}", "length"),
)


def _sheet_metal_values(params, c):
    return [
        text.format(c.length(params[key]) if kind == "length" else _number(params[key]))
        for key, text, kind in _SHEET_METAL_VALUES
        if isinstance(params.get(key), int | float) and not isinstance(params.get(key), bool)
    ]


def _sheet_metal_feature(p, c):
    operation = str(p.get("operation", "feature"))
    words = _words(operation)
    params = p.get("parameters") if isinstance(p.get("parameters"), dict) else {}
    if p.get("feature"):
        head = f"Edit sheet-metal {words} {c.obj(p.get('feature'))}"
    else:
        head = _SHEET_METAL_VERBS.get(operation) or f"Add sheet-metal {words}"
    values = _sheet_metal_values(params, c)
    return head + (f", {_listed(values)}" if values else "")


_SHEET_METAL_DEFAULTS = (
    ("thickness", "thickness {}", "length"),
    ("bend_radius", "bend radius {}", "length"),
    ("neutral_factor", "neutral factor {}", "number"),
    ("bend_relief_width", "relief width {}", "length"),
    ("bend_relief_depth", "relief depth {}", "length"),
    ("material", 'material "{}"', "text"),
    ("tool", 'tool "{}"', "text"),
    ("parameter_entry", "entry {}", "text"),
    ("bend_definition", "bend definition {}", "text"),
    ("bend_table", "bend table {}", "path"),
    ("bend_allowance_formula", "bend allowance formula", "flag"),
    ("bend_deduction_formula", "bend deduction formula", "flag"),
)


def _sheet_metal_defaults(p, c):
    shown = {"length": c.length, "number": _number, "text": str, "path": c.path}
    parts = [
        text.format(shown.get(kind, str)(p[key]))
        for key, text, kind in _SHEET_METAL_DEFAULTS
        if p.get(key) is not None
    ]
    return "Set sheet-metal defaults: " + (_listed(parts) if parts else "no changes")


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


def _dimension_format(p, c):
    parts = []
    if p.get("decimal_places") is not None:
        parts.append(f"{p['decimal_places']} decimals")
    if p.get("tolerance_type"):
        parts.append(_tolerance(p, c))
    if p.get("units"):
        parts.append(f"in {p['units']}")
    if p.get("decimal_separator"):
        parts.append(f"{p['decimal_separator']} separator")
    if p.get("trailing_zeros") is not None:
        parts.append("trailing zeros" if p["trailing_zeros"] else "no trailing zeros")
    if p.get("tolerance_decimal_places") is not None:
        parts.append(f"{p['tolerance_decimal_places']} tolerance decimals")
    head = f"Format dimension {c.obj(p.get('dimension'))}"
    return head + (f": {_listed(parts)}" if parts else "")


def _edit_drawing_view(p, c):
    parts = []
    if p.get("position"):
        parts.append(f"move to {_point(p['position'])}")
    if p.get("scale"):
        parts.append(f"scale {_scale(p['scale'])}")
    if p.get("style"):
        parts.append("restyle")
    return f"Edit view {c.obj(p.get('view'))}" + (f": {_listed(parts, 3)}" if parts else "")


def _edit_annotation(p, c):
    target = c.obj(p.get("annotation"))
    if p.get("delete"):
        return f"Delete annotation {target}"
    if p.get("name") and p.get("position"):
        return f'Move and rename annotation {target} to "{p["name"]}"'
    if p.get("name"):
        return f'Rename annotation {target} to "{p["name"]}"'
    if p.get("position"):
        return f"Move annotation {target} to {_point(p['position'])}"
    return f"Edit annotation {target}"


def _edit_faces(p, c):
    faces = c.some(p.get("faces"), "face")
    action = p.get("action")
    if action == "move":
        return f"Move {faces} {c.length(p.get('distance'))} along {_direction(p.get('direction'))}"
    if action == "offset":
        return f"Offset {faces} by {c.length(p.get('distance'))}"
    if action == "replace":
        return f"Replace {faces} with {c.obj(p.get('replacement'))}"
    return f"Heal {faces}"


def _component_action(p, c):
    target = c.obj(p.get("component"))
    action = p.get("action")
    if action == "rename":
        return f"Rename component {target}" + (f' to "{p["name"]}"' if p.get("name") else "")
    if action == "replace":
        return f"Replace component {target} with {c.path(p.get('part_path'))}"
    return f"{str(action).capitalize()} component {target}"


def _component_array(p, c):
    target = c.obj(p.get("component"))
    if p.get("pattern_type") == "circular":
        return f"Circular array of {target}: {p.get('count')} × {_number(p.get('angle'))}°"
    text = f"Array {target} {p.get('count')} × {c.length(p.get('spacing'))}"
    if _float(p.get("count_y")) > 1:
        text += f" by {p.get('count_y')} × {c.length(p.get('spacing_y'))}"
    return text


def _edit_component_pattern(p, c):
    parts = []
    if p.get("count") is not None:
        parts.append(f"count {p['count']}")
    if p.get("spacing") is not None:
        parts.append(f"spacing {c.length(p['spacing'])}")
    if p.get("angle") is not None:
        parts.append(f"pitch {_number(p['angle'])}°")
    if p.get("count_y") is not None:
        parts.append(f"count Y {p['count_y']}")
    if p.get("spacing_y") is not None:
        parts.append(f"spacing Y {c.length(p['spacing_y'])}")
    head = f"Edit component pattern {c.obj(p.get('pattern'))}"
    return head + (f": {_listed(parts)}" if parts else "")


def _edit_assembly_constraint(p, c):
    parts = []
    if p.get("value") is not None:
        parts.append(f"value {_number(p['value'])}")
    if p.get("suppressed") is not None:
        parts.append("suppress" if p["suppressed"] else "unsuppress")
    if p.get("alignment"):
        parts.append(f"alignment {p['alignment']}")
    head = f"Edit assembly constraint {c.obj(p.get('constraint'))}"
    return head + (f": {_listed(parts, 3)}" if parts else "")


def _edit_explosion(p, c):
    placements = p.get("placements") or []
    resets = p.get("reset_components") or []
    parts = []
    if placements:
        parts.append(f"move {c.some([x.get('component') for x in placements], 'component')}")
    if resets:
        parts.append(f"reset {c.some(resets, 'component')}")
    return f"Edit explosion {c.obj(p.get('explosion'))}: " + (" and ".join(parts) or "no changes")


def _edit_explosion_trace(p, c):
    keys = ("start_percent", "end_percent", "start_offset", "end_offset")
    changed = [_words(k) for k in keys if p.get(k) is not None]
    head = f"Edit explosion trace {c.obj(p.get('traceline'))}"
    return head + (f": {_listed(changed)}" if changed else "")


def _drawing_table(p, c):
    rows = p.get("rows") if isinstance(p.get("rows"), list) else []
    columns = len(rows[0]) if rows and isinstance(rows[0], list) else 0
    kind = "title block" if p.get("kind") == "title_block" else "revision table"
    verb = f"Edit {kind} {c.obj(p.get('table'))}" if p.get("table") else f"Add {kind}"
    return f"{verb} ({len(rows)} × {columns} cells) on drawing {c.obj(p.get('drawing'))}"


def _parts_list_column(p, c):
    target = c.obj(p.get("parts_list"))
    action = p.get("action")
    if action == "append":
        title = f' "{p["title"]}"' if p.get("title") else ""
        return f"Add column{title} to parts list {target}"
    verb = "Remove" if action == "remove" else "Edit"
    column = f"column {p['index']}" if p.get("index") is not None else "a column"
    return f"{verb} {column} of parts list {target}"


def _bend_table(p, c):
    columns = p.get("columns")
    tail = f" ({c.count(columns, 'column')})" if columns else ""
    if p.get("table"):
        return f"Edit bend table {c.obj(p.get('table'))}{tail}"
    return f"Add bend table for view {c.obj(p.get('view'))}{tail}"


def _pmi_fcf(p, c):
    characteristic = "".join(
        f" {ch.lower()}" if ch.isupper() else ch for ch in str(p.get("characteristic"))
    ).strip()
    zone = "Ø" if p.get("zone_shape") in ("diameter", "spherical_diameter") else ""
    datums = p.get("datums") or []
    verb = "Edit" if p.get("annotation") else "Add"
    text = (
        f"{verb} {characteristic} tolerance {zone}{c.length(p.get('tolerance'))} "
        f"on {c.some(p.get('faces'), 'face')}"
    )
    return text + (f" to {c.count(datums, 'datum')}" if datums else "")


def _set_display(p, c):
    parts = []
    if p.get("color"):
        parts.append(str(p["color"]))
    elif p.get("color_index") is not None:
        parts.append(f"color {p['color_index']}")
    if p.get("transparency") is not None:
        parts.append(f"{p['transparency']}% transparent")
    targets = c.some(p.get("objects"), "object")
    return f"Make {targets} {', '.join(parts)}" if parts else f"Restyle {targets}"


def _section_view(p, c):
    if p.get("section"):
        return f"Move section {c.obj(p.get('section'))} to {_point(p.get('origin'))}"
    name = p.get("name") or "MCP section"
    return f'Add section "{name}" at {_point(p.get("origin"))} normal {_direction(p.get("normal"))}'


def _spline(p, c):
    points = p.get("points") or []
    method = "poles" if p.get("method") == "poles" else "points"
    head = f"Edit spline {c.obj(p.get('feature'))}" if p.get("feature") else "Create spline"
    through = "with" if method == "poles" else "through"
    return f"{head} {through} {len(points)} {method}, degree {p.get('degree', 3)}"


def _transform_bodies(p, c):
    rotation = p.get("rotation_matrix")
    identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    turned = isinstance(rotation, list) and rotation != identity
    if p.get("feature"):
        head = f"Edit transform {c.obj(p.get('feature'))}"
    else:
        verb = "Copy" if p.get("copy") else "Move"
        bodies = c.some(p.get("bodies"), "body") if p.get("bodies") else "bodies"
        head = f"{verb} {bodies}"
    return f"{head} by {_point(p.get('translation'))}{c.unit}" + (" and rotate" if turned else "")


def _find_geometry(p, c):
    kind = p.get("kind") or "face"
    shape = p.get("geometry_type")
    what = f"{shape} {_plural(kind, 2)}" if shape and shape != "any" else _plural(kind, 2)
    text = f"Find {what} in {c.obj(p.get('owner'))}"
    if p.get("radius") is not None:
        text += f" R{c.length(p['radius'])}"
    if p.get("near") and p.get("order", "nearest") == "nearest":
        text += f" nearest {_point(p['near'])}"
    elif p.get("order") in ("highest", "lowest"):
        text += f", {p['order']} in {p.get('axis', 'Z')}"
    return text


def _capabilities(p, c):
    if p.get("tool"):
        return f"Check what {p['tool']} supports on this NX"
    if p.get("prefix"):
        return f"Check tested scope of {p['prefix']}* tools"
    return "Check tested scope of all tools"


def _thread_catalog(p, c):
    if p.get("size"):
        return f"Look up thread {p.get('standard')} {p['size']}"
    if p.get("standard"):
        return f'List thread sizes of "{p["standard"]}"'
    return "List installed thread standards"


def _activate_part(p, c):
    target = c.obj(p.get("part"))
    work, display = p.get("work", True), p.get("display", True)
    if work and display:
        return f"Make {target} the displayed work part"
    return f"Make {target} the work part" if work else f"Display {target}"


def _import_geometry(p, c):
    source = c.path(p.get("path"))
    if p.get("target") == "new_part":
        text = f"Import STEP {source} into new part {c.path(p.get('output_path'))}"
    else:
        text = f"Import STEP {source} into the work part"
    return text + (", flattened" if p.get("flatten") else "")


def _upload(p, c):
    size = _float(p.get("total_size"))
    shown = f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{math.ceil(size / 1024)} KB"
    verb = "Continue uploading" if _float(p.get("offset")) > 0 else "Upload"
    return f"{verb} {c.path(p.get('path'))} ({shown})"


def _clearance(p, c):
    objects = p.get("objects")
    if isinstance(objects, list) and len(objects) > 1:
        text = f"Check clearance between {c.count(objects, 'object')}"
    elif objects:
        text = f"Check clearance within {c.some(objects, 'object')}"
    else:
        text = "Check clearance across the whole assembly"
    minimum = _float(p.get("minimum_clearance"))
    return text + (f", minimum {c.length(minimum)}" if minimum > 0 else "")


def _inspection_report(p, c):
    objects = p.get("objects")
    scope = f" for {c.some(objects, 'object')}" if objects else ""
    return f"Write clearance report{scope} to {c.path(p.get('path'))}"


def _sketch_constraint(p, c):
    kind = _words(p.get("constraint_type", ""))
    if kind == "fixed":
        kind = "fix"
    text = f"Add {kind} constraint to {c.some(p.get('targets'), 'curve')}"
    value = p.get("value")
    if value is None:
        return text
    shown = f"{_number(value)}°" if kind == "angle" else c.length(value)
    return f"{text} = {shown}"


def _sketch_primitive(p, c):
    kind = p.get("primitive")
    where = c.sketch(p.get("sketch_id"))
    if kind == "circle":
        return f"Add circle R{c.length(p.get('radius'))} to {where}"
    size = f"{_number(p.get('width'))} × {_number(p.get('height'))}{c.unit}"
    if kind == "slot":
        return f"Add {size} slot to {where}"
    return f"Add {size} rounded rectangle R{_number(p.get('radius'))} to {where}"


def _sketch_relation(p, c):
    relation = _words(p.get("relation", ""))
    first, second = c.obj(p.get("curve1")), c.obj(p.get("curve2"))
    if p.get("point1") or p.get("point2"):
        first = f"{p.get('point1') or 'point'} of {first}"
        second = f"{p.get('point2') or 'point'} of {second}"
        return f"Make {first} {relation} with {second}"
    return f"Make {first} and {second} {relation}"


def _show_explosion(p, c):
    if not p.get("explosion"):
        text = "Show the assembly unexploded"
    else:
        text = f"Show explosion {c.obj(p.get('explosion'))}"
    if p.get("drawing_view"):
        return f"{text} in view {c.obj(p.get('drawing_view'))}"
    return text


def _material(p, c):
    return (
        f'Assign material "{p.get("name")}" ({_number(p.get("density"))} kg/m³) '
        f"to {c.some(p.get('bodies'), 'body')}"
    )


def _workspace_list(p, c):
    folder = p.get("path") or "."
    where = "the workspace root" if folder in (".", "./", "") else c.path(folder)
    prefix = f' matching "{p["prefix"]}"' if p.get("prefix") else ""
    return f"List files in {where}{prefix}"


def _list_components(p, c):
    text = "List assembly components"
    if p.get("name_contains"):
        text += f' named like "{p["name_contains"]}"'
    if p.get("suppressed") is not None:
        text += " (suppressed only)" if p["suppressed"] else " (unsuppressed only)"
    return text


def _list_open_parts(p, c):
    if p.get("active_only"):
        return "List the active parts"
    text = "List parts loaded in NX"
    if p.get("modified") is not None:
        text += " (modified only)" if p["modified"] else " (unmodified only)"
    return text


_READ_ONLY_FIXED = {
    "nx_status": "Check NX and bridge status",
    "nx_workspace_info": "Check the workspace location",
    "nx_checkpoint_state": "List undo checkpoints",
    "nx_list_bodies": "List bodies in the work part",
    "nx_list_features": "List features in the work part",
    "nx_list_sketches": "List sketches in the work part",
    "nx_list_datums": "List datums in the work part",
    "nx_list_annotations": "List annotations in the work part",
    "nx_list_dimensions": "List dimensions in the work part",
    "nx_list_drawings": "List drawing sheets and views",
    "nx_list_explosions": "List explosions in the work part",
    "nx_list_sections": "List section views",
    "nx_list_reference_sets": "List reference sets in the work part",
    "nx_list_assembly_constraints": "List constraints in the work assembly",
    "nx_list_component_patterns": "List component patterns in the work assembly",
    "nx_view_info": "Check the current view and camera",
    "nx_sheet_metal_defaults": "Read sheet-metal defaults",
    "nx_resolve_geometry": "Find geometry again from a saved selector",
    "nx_resolve_geometry_anchor": "Resolve a saved geometry anchor",
}

_ACTION_FIXED = {
    "nx_save_part": "Save the work part",
    "nx_fit_view": "Fit the view",
    "nx_undo": "Undo the agent's last operation",
    "nx_screenshot": "Capture a screenshot",
    "nx_render_view": "Render the view to an image",
    "nx_datum_csys": "Create a datum coordinate system",
    "nx_clear_highlights": "Clear the agent's highlights",
    "nx_rebuild_model": "Update the model",
    "nx_refresh_annotations": "Refresh sheet-metal annotations",
    "nx_update_assembly_documentation": "Update assembly mates, parts lists, traces and views",
    "nx_sheet_metal_context": "Switch NX to the Sheet Metal application",
    "nx_set_camera": "Set the view camera",
    "nx_restore_display": "Restore earlier display settings",
    "nx_rollback": "Roll back to an earlier checkpoint",
    "nx_cancel_operation": "Cancel a running batch",
    "nx_operation_status": "Check the outcome of an earlier request",
    "nx_read_result": "Read a stored result",
}

_TEMPLATES: dict[str, Callable[[dict[str, Any], _Context], str]] = {
    # Files, parts and session
    "nx_create_part": lambda p, c: f"Create part {c.path(p.get('path'))}",
    "nx_open_part": lambda p, c: f"Open {c.path(p.get('path'))}",
    "nx_save_as": lambda p, c: f"Save the work part as {c.path(p.get('path'))}",
    "nx_close_part": lambda p, c: (
        f"Close {c.obj(p.get('part'))}" + (" after saving" if p.get("save") else " without saving")
    ),
    "nx_activate_part": _activate_part,
    "nx_activate_drawing": lambda p, c: (
        f"Open drawing sheet {c.obj(p.get('drawing'))}"
        if p.get("drawing")
        else "Return from the drawing to modeling"
    ),
    "nx_export_step": lambda p, c: f"Export STEP to {c.path(p.get('path'))}",
    "nx_export_drawing_pdf": lambda p, c: f"Export drawing PDF to {c.path(p.get('path'))}",
    "nx_import_geometry": _import_geometry,
    "nx_export_planar_dxf": lambda p, c: (
        f"Export {c.obj(p.get('source'))} as DXF to {c.path(p.get('path'))}"
    ),
    "nx_export_flat_pattern": lambda p, c: (
        f"Export flat pattern {c.obj(p.get('flat_pattern'))} as "
        f"{str(p.get('format') or 'dxf').upper()} to {c.path(p.get('path'))}"
    ),
    "nx_export_explosion_animation": lambda p, c: (
        f"Export explosion {c.obj(p.get('explosion'))} animation "
        f"({p.get('frames', 12)} frames) to {c.path(p.get('path'))}"
    ),
    "nx_copy_project": lambda p, c: (
        f'Copy the assembly to {c.path(p.get("path"))} with prefix "{p.get("prefix")}"'
    ),
    "nx_package_assembly": lambda p, c: f"Package the assembly into {c.path(p.get('path'))}",
    "nx_create_directory": lambda p, c: f"Create folder {c.path(p.get('path'))}",
    "nx_download_file": lambda p, c: (
        f"Check file {c.path(p.get('path'))}"
        if p.get("delivery") == "metadata"
        else f"Read file {c.path(p.get('path'))}"
    ),
    "nx_upload_file": _upload,
    "nx_workspace_list": _workspace_list,
    "nx_save_presentation": lambda p, c: f"Save the view presentation to {c.path(p.get('path'))}",
    "nx_restore_presentation": lambda p, c: (
        f"Restore the view presentation from {c.path(p.get('path'))}"
    ),
    "nx_capabilities": _capabilities,
    "nx_ui_control": lambda p, c: _CONTROL.get(p.get("mode"), "Check who controls NX"),
    "nx_checkpoint": lambda p, c: f'Create undo checkpoint "{p.get("label", "checkpoint")}"',
    "nx_rename_object": lambda p, c: f'Rename {c.obj(p.get("object_id"))} to "{p.get("name")}"',
    "nx_batch": lambda p, c: (
        c.steps(p.get("operations"), f"Batch of {c.count(p.get('operations'), 'step')}: ")
        if p.get("operations")
        else "Run an empty batch"
    ),
    "nx_preview_change": lambda p, c: c.steps(
        p.get("operations"), f"Preview {c.count(p.get('operations'), 'change')}: "
    ),
    "nx_finish_preview": lambda p, c: (
        "Apply the previewed changes"
        if p.get("action") == "accept"
        else "Discard the previewed changes"
    ),
    # Sketches
    "nx_create_sketch": _sketch,
    "nx_create_sketch_on": lambda p, c: (
        f'Create sketch "{p["name"]}" on {c.obj(p.get("target"))}'
        if p.get("name")
        else f"Create a sketch on {c.obj(p.get('target'))}"
    ),
    "nx_create_path_sketch": lambda p, c: (
        "Create sketch"
        + (f' "{p["name"]}"' if p.get("name") else "")
        + f" along {c.some(p.get('edges'), 'edge')} at {_number(p.get('percent', 0))}%"
    ),
    "nx_finish_sketch": lambda p, c: f"Finish sketch {c.obj(p.get('sketch_id'))}",
    "nx_edit_sketch": _edit_sketch,
    "nx_sketch_line": _line,
    "nx_sketch_rectangle": _rectangle,
    "nx_sketch_arc": _arc,
    "nx_sketch_primitive": _sketch_primitive,
    "nx_sketch_constraint": _sketch_constraint,
    "nx_sketch_relation": _sketch_relation,
    "nx_sketch_tangent": lambda p, c: (
        f"Make {c.obj(p.get('curve2'))} tangent to {c.obj(p.get('curve1'))}"
    ),
    "nx_sketch_symmetry": lambda p, c: (
        f"Make {c.obj(p.get('curve1'))} and {c.obj(p.get('curve2'))} symmetric "
        f"about {c.obj(p.get('centerline'))}"
    ),
    "nx_sketch_angle": lambda p, c: (
        f"Dimension angle between {c.obj(p.get('line1'))} and {c.obj(p.get('line2'))} "
        f"to {_number(p.get('value'))}°"
    ),
    "nx_sketch_dimension": lambda p, c: (
        f"Dimension {p.get('dimension_type')} of {c.obj(p.get('curve'))} "
        + ("as a reference" if p.get("reference") else f"to {c.length(p.get('value'))}")
    ),
    "nx_sketch_trim_extend": lambda p, c: (
        f"{'Extend' if p.get('action') == 'extend' else 'Trim'} {c.obj(p.get('curve'))} "
        f"{'to' if p.get('action') == 'extend' else 'at'} "
        f"{c.some(p.get('boundaries'), 'boundary')}"
    ),
    "nx_sketch_info": lambda p, c: f"Inspect sketch {c.obj(p.get('sketch_id'))}",
    "nx_sketch_diagnostics": lambda p, c: (
        f"Check constraint status of sketch {c.obj(p.get('sketch_id'))}"
    ),
    "nx_sketch_conflicts": lambda p, c: (
        f"Find conflicting constraints in sketch {c.obj(p.get('sketch_id'))}"
    ),
    # Features and bodies
    "nx_extrude": _extrude,
    "nx_revolve": lambda p, c: (
        f'Revolve sketch "{p.get("sketch_name")}" '
        f"{_number(p.get('angle', 360))}° about {p.get('axis', 'Z')}"
    ),
    "nx_sweep": lambda p, c: (
        f"Sweep {c.obj(p.get('section'))} along {c.obj(p.get('guide'))}"
        + (
            ""
            if p.get("boolean") in (None, "none")
            else f", {p['boolean']} with {c.some(p.get('targets'), 'body')}"
        )
    ),
    "nx_loft": lambda p, c: (
        f"Loft through {c.count(p.get('sketches'), 'sketch')}"
        + ("" if p.get("solid", True) else " as a sheet")
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
    "nx_thread": lambda p, c: (
        f"Add thread Ø{_number(p.get('major_diameter'))} × P{_number(p.get('pitch'))}, "
        f"{c.length(p.get('length'))} long on {c.obj(p.get('face'))}"
    ),
    "nx_standard_thread": lambda p, c: (
        f"Add {p.get('size')} thread, {c.length(p.get('length'))} long on {c.obj(p.get('face'))}"
    ),
    "nx_boolean": lambda p, c: (
        f"{str(p.get('boolean_type', 'Boolean')).capitalize()} {c.count(p.get('targets'), 'body')}"
    ),
    "nx_pattern": lambda p, c: (
        f"Pattern {c.count(p.get('features'), 'feature')}: "
        f"{_number(p.get('count', 2))} × {c.length(p.get('spacing', 10))} "
        f"along {p.get('direction', 'X')}"
    ),
    "nx_circular_pattern": lambda p, c: (
        f"Circular pattern of {c.count(p.get('features'), 'feature')}: "
        f"{_number(p.get('count'))} × {_number(p.get('angle'))}°"
    ),
    "nx_shell": lambda p, c: f"Shell {c.obj(p.get('body'))} {c.length(p.get('thickness'))} thick",
    "nx_draft": lambda p, c: f"Draft {c.count(p.get('faces'), 'face')} {_number(p.get('angle'))}°",
    "nx_thicken": lambda p, c: (
        f"Thicken {c.count(p.get('faces'), 'face')} by {c.length(p.get('first_offset'))}"
    ),
    "nx_mirror_body": lambda p, c: (
        f"Mirror {c.obj(p.get('body'))} about the {p.get('plane')} plane"
    ),
    "nx_transform_bodies": _transform_bodies,
    "nx_edit_faces": _edit_faces,
    "nx_sew": lambda p, c: (
        f"Sew {c.obj(p.get('target'))} with {c.some(p.get('tools'), 'sheet')}"
        + (" into a solid" if p.get("solid") else "")
    ),
    "nx_trim_sheet": lambda p, c: (
        f"Trim sheet {c.obj(p.get('body'))} with {c.some(p.get('boundaries'), 'curve')}"
    ),
    "nx_bridge_surface": lambda p, c: (
        f"Bridge {c.obj(p.get('first'))} and {c.obj(p.get('second'))} ({p.get('continuity', 'G0')})"
    ),
    "nx_surface_mesh": lambda p, c: (
        f"Create mesh surface from {len(p.get('primary') or [])} × "
        f"{len(p.get('cross') or [])} curve sections"
    ),
    "nx_spline": _spline,
    "nx_delete_feature": lambda p, c: f"Delete feature {c.obj(p.get('name'))}",
    "nx_edit_feature": lambda p, c: (
        f"Edit feature {c.obj(p.get('name'))}: {c.assignments(p.get('params'))}"
    ),
    "nx_set_feature_parameters": lambda p, c: (
        f"Set {c.assignments(p.get('values'))} on {c.obj(p.get('feature'))}"
    ),
    "nx_bind_parameter": lambda p, c: (
        f"Bind {p.get('parameter')} of {c.obj(p.get('feature'))} "
        f"to expression {c.obj(p.get('expression'))}"
    ),
    "nx_set_expression": lambda p, c: f"Set {p.get('expression')} = {p.get('formula')}",
    "nx_set_material": _material,
    "nx_create_reference_set": lambda p, c: (
        f'Create reference set "{p.get("name")}" with {c.count(p.get("objects"), "object")}'
    ),
    "nx_geometry_anchor": lambda p, c: f"Capture a persistent anchor for {c.obj(p.get('object'))}",
    "nx_datum_plane": _datum_plane,
    "nx_datum_axis": lambda p, c: (
        "Create a datum axis"
        + (" through two points" if p.get("method") == "two_points" else " along a direction")
    ),
    "nx_datum_point": lambda p, c: (
        "Create a point at " + ", ".join(_number(v) for v in (p.get("position") or []))
    ),
    # Sheet metal
    "nx_sheet_metal_feature": _sheet_metal_feature,
    "nx_set_sheet_metal_defaults": _sheet_metal_defaults,
    "nx_sheet_metal_info": lambda p, c: (
        f"Inspect sheet-metal body {c.obj(p.get('body'))}"
        if p.get("body")
        else "Inspect sheet-metal bodies in the work part"
    ),
    "nx_sheet_metal_schema": lambda p, c: (
        f"Look up sheet-metal {_words(p['operation'])} parameters"
        if p.get("operation")
        else "List sheet-metal operations"
    ),
    "nx_flat_pattern_orientation_edges": lambda p, c: (
        f"List flat-pattern orientation edges of {c.obj(p.get('upward_face'))}"
    ),
    "nx_sheet_metal_annotation": lambda p, c: (
        f"Refresh sheet-metal annotation {c.obj(p.get('annotation'))}"
        if p.get("annotation")
        else (
            f"Annotate bends of {c.some(p.get('faces'), 'face')}"
            if p.get("kind") == "bend"
            else f"Annotate thickness of {c.obj(p.get('body'))}"
        )
    ),
    # Assemblies
    "nx_add_component": lambda p, c: (
        f"Add component {c.path(p.get('part_path'))}"
        + (f" at {_point(p['translation'])}{c.unit}" if p.get("translation") else "")
    ),
    "nx_reposition_component": _reposition,
    "nx_set_component_transform": lambda p, c: (
        f"Place component {c.obj(p.get('component'))} at {_point(p.get('translation'))}{c.unit}"
    ),
    "nx_assembly_constraint": lambda p, c: (
        f"Add a {p.get('constraint_type', '')} constraint on {c.obj(p.get('component'))}"
    ),
    "nx_mate_component": lambda p, c: (
        f"Add {_words(p.get('mate_type', ''))} mate on {c.obj(p.get('component'))}"
        + (f", offset {c.length(p['offset'])}" if _float(p.get("offset")) else "")
    ),
    "nx_edit_assembly_constraint": _edit_assembly_constraint,
    "nx_component_action": _component_action,
    "nx_component_array": _component_array,
    "nx_native_component_pattern": lambda p, c: (
        f"Pattern {c.obj(p.get('component'))} {p.get('count')} × "
        f"{c.length(p.get('spacing'))} along {_direction(p.get('direction'))}"
    ),
    "nx_pattern_components": lambda p, c: (
        f"Copy {c.obj(p.get('component'))} {p.get('count')} × "
        f"{c.length(p.get('spacing'))} along {_direction(p.get('direction'))}"
    ),
    "nx_edit_component_pattern": _edit_component_pattern,
    "nx_set_component_reference_set": lambda p, c: (
        f'Use reference set "{p.get("name")}" for {c.some(p.get("components"), "component")}'
    ),
    "nx_create_explosion": lambda p, c: f'Create explosion "{p.get("name")}"',
    "nx_delete_explosion": lambda p, c: f"Delete explosion {c.obj(p.get('explosion'))}",
    "nx_edit_explosion": _edit_explosion,
    "nx_show_explosion": _show_explosion,
    "nx_explosion_trace": lambda p, c: (
        f"Add trace line from {c.obj(p.get('start_edge'))} to {c.obj(p.get('end_edge'))}"
    ),
    "nx_edit_explosion_trace": _edit_explosion_trace,
    # Drawings and PMI
    "nx_create_drawing": lambda p, c: (
        f'Create drawing sheet "{p.get("name", "Sheet1")}" ({p.get("size", "A3")})'
    ),
    "nx_add_base_view": lambda p, c: (
        f"Add {_a(p.get('view') or 'isometric')} view to drawing {c.obj(p.get('drawing'))}"
    ),
    "nx_add_projection_view": lambda p, c: (
        f"Add a projected view {p.get('direction')} of view {c.obj(p.get('base_view'))}"
    ),
    "nx_add_section_drawing_view": lambda p, c: (
        f"Add a section view of {c.obj(p.get('parent_view'))} "
        f"through {c.obj(p.get('cut_object'))}"
        + (f" at {_scale(p['scale'])}" if p.get("scale") not in (None, 1, 1.0) else "")
    ),
    "nx_add_detail_drawing_view": lambda p, c: (
        f"Add a {_scale(p.get('scale', 2))} detail view R{c.length(p.get('radius'))} "
        f"of view {c.obj(p.get('parent_view'))}"
    ),
    "nx_add_flat_pattern_view": lambda p, c: (
        f"Add a flat-pattern view of {c.obj(p.get('flat_pattern'))} "
        f"to drawing {c.obj(p.get('drawing'))}"
    ),
    "nx_edit_drawing_view": _edit_drawing_view,
    "nx_add_dimension": lambda p, c: (
        ("Rebind" if p.get("dimension") else "Add")
        + f" {p.get('dim_type') or 'aligned'} dimension "
        + (
            f"between {c.obj(p.get('object1'))} and {c.obj(p.get('object2'))}"
            if p.get("object2")
            else f"on {c.obj(p.get('object1'))}"
        )
    ),
    "nx_edit_dimension_format": _dimension_format,
    "nx_dimension_format": lambda p, c: f"Read the format of dimension {c.obj(p.get('dimension'))}",
    "nx_edit_annotation": _edit_annotation,
    "nx_create_parts_list": lambda p, c: (
        "Add a parts list ("
        + {"top_level": "top level", "all": "all levels"}.get(p.get("scope"), "leaf parts")
        + f") to drawing {c.obj(p.get('drawing'))}"
    ),
    "nx_update_parts_list": lambda p, c: f"Update parts list {c.obj(p.get('parts_list'))}",
    "nx_parts_list_column": _parts_list_column,
    "nx_parts_list_balloons": lambda p, c: (
        f"Add parts-list balloons in view {c.obj(p.get('view'))}"
    ),
    "nx_parts_list_info": lambda p, c: f"Read parts list {c.obj(p.get('parts_list'))}",
    "nx_drawing_table": _drawing_table,
    "nx_bend_table": _bend_table,
    "nx_drawing_view_info": lambda p, c: f"Check scale and position of view {c.obj(p.get('view'))}",
    "nx_pmi_datum": lambda p, c: (
        f"{'Edit' if p.get('annotation') else 'Add'} datum {p.get('letter')} "
        f"on {c.some(p.get('faces'), 'face')}"
    ),
    "nx_pmi_fcf": _pmi_fcf,
    # Display and views
    "nx_set_view": lambda p, c: f"Set the view to {p.get('orientation')}",
    "nx_set_visibility": lambda p, c: (
        f"{str(p.get('mode') or 'show').capitalize()} {c.some(p.get('objects'), 'object')}"
    ),
    "nx_set_display": _set_display,
    "nx_set_datum_visibility": lambda p, c: (
        "Show all datums" if p.get("visible") else "Hide all datums"
    ),
    "nx_highlight_objects": lambda p, c: f"Highlight {c.some(p.get('objects'), 'object')}",
    "nx_highlight_collisions": lambda p, c: (
        f"Highlight collisions between {c.obj(p.get('obj1'))} and {c.obj(p.get('obj2'))}"
    ),
    "nx_section_view": _section_view,
    "nx_section_control": lambda p, c: (
        f"{str(p.get('action', 'enable')).capitalize()} section {c.obj(p.get('section'))}"
    ),
    "nx_display_info": lambda p, c: (
        f"Check display settings of {c.some(p.get('objects'), 'object')}"
    ),
    # Measurements and inspections
    "nx_measure_distance": lambda p, c: (
        f"Measure distance between {c.obj(p.get('obj1'))} and {c.obj(p.get('obj2'))}"
    ),
    "nx_measure_angle": lambda p, c: (
        f"Measure angle between {c.obj(p.get('obj1'))} and {c.obj(p.get('obj2'))}"
    ),
    "nx_measure_volume": lambda p, c: f"Measure volume of {c.scope(p.get('body'), p.get('scope'))}",
    "nx_mass_properties": lambda p, c: (
        f"Measure mass properties of {c.scope(p.get('body'), p.get('scope'))}"
    ),
    "nx_get_bounding_box": lambda p, c: (
        f"Measure the bounding box of {c.scope(p.get('body'), p.get('scope'))}"
    ),
    "nx_material_info": lambda p, c: f"Check material of {c.scope(p.get('body'), p.get('scope'))}",
    "nx_check_clearance": _clearance,
    "nx_check_interference": lambda p, c: (
        f"Check interference between {c.obj(p.get('obj1'))} and {c.obj(p.get('obj2'))}"
    ),
    "nx_inspection_report": _inspection_report,
    "nx_wall_thickness": lambda p, c: (
        f"Measure wall thickness of {c.obj(p.get('body'))}"
        + (f" on {c.count(p.get('faces'), 'face')}" if p.get("faces") else "")
    ),
    "nx_surface_continuity": lambda p, c: (
        f"Check continuity between {c.obj(p.get('first'))} and {c.obj(p.get('second'))}"
    ),
    "nx_face_analysis": lambda p, c: (
        f"Analyze {c.some(p.get('faces'), 'face')}"
        + (
            f" for draft (min {_number(p.get('minimum_draft', 1))}°)"
            if p.get("pull_direction")
            else ""
        )
    ),
    "nx_curve_analysis": lambda p, c: f"Analyze curvature of {c.obj(p.get('curve'))}",
    "nx_find_geometry": _find_geometry,
    "nx_recognize_holes": lambda p, c: f"Find holes in {c.obj(p.get('owner'))}",
    "nx_model_health": lambda p, c: (
        "Check model health" + (" of the assembly" if p.get("scope") == "assembly" else "")
    ),
    "nx_model_summary": lambda p, c: f"Summarize the work part ({p.get('section') or 'overview'})",
    "nx_get_feature_info": lambda p, c: f"Inspect feature {c.obj(p.get('name'))}",
    "nx_feature_parameters": lambda p, c: f"List parameters of feature {c.obj(p.get('feature'))}",
    "nx_list_topology": lambda p, c: (
        f"List faces and edges of {c.obj(p.get('face') or p.get('body'))}"
    ),
    "nx_list_components": _list_components,
    "nx_list_open_parts": _list_open_parts,
    "nx_list_expressions": lambda p, c: (
        f'List expressions named like "{p["name_contains"]}"'
        if p.get("name_contains")
        else "List expressions in the work part"
    ),
    "nx_explosion_info": lambda p, c: f"Inspect explosion {c.obj(p.get('explosion'))}",
    "nx_thread_catalog": _thread_catalog,
}


def _fixed(text: str) -> Callable[[dict[str, Any], _Context], str]:
    return lambda p, c: text


_TEMPLATES.update({name: _fixed(text) for name, text in _READ_ONLY_FIXED.items()})
_TEMPLATES.update({name: _fixed(text) for name, text in _ACTION_FIXED.items()})


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
