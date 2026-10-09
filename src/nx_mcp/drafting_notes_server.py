"""Typed schemas: Drawing annotations: notes and leaders, center marks, centerlines, surface finish and weld symbols.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are DraftingNotesMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.schema_types import Field

Point2 = Annotated[list[float], Field(min_length=2, max_length=2)]

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()


def _lines(p):
    text = p.get("text")
    first = text[0] if isinstance(text, list) and text else ""
    more = len(text or []) - 1
    return f'"{str(first)[:30]}"' + (
        f" (+{more} line{'s' if more > 1 else ''})" if more > 0 else ""
    )


SUMMARIES: dict = {
    "nx_add_note": lambda p, c: (
        f"Add drawing note {_lines(p)}"
        + (f" with leader to {c.obj(p.get('leader_to'))}" if p.get("leader_to") else "")
    ),
    "nx_add_center_mark": lambda p, c: (
        f"Add {c.count(p.get('edges'), 'center mark')} in view {c.obj(p.get('view'))}"
    ),
    "nx_add_centerline": lambda p, c: (
        f"Add {p.get('kind', '3d')} centerline in view {c.obj(p.get('view'))}"
    ),
    "nx_add_surface_finish": lambda p, c: (
        f"Add surface finish Ra {p.get('roughness')} on {c.obj(p.get('object'))}"
    ),
    "nx_add_weld_symbol": lambda p, c: (
        f"Add {str(p.get('weld')).replace('_', ' ')} weld symbol {p.get('size')} "
        f"on {c.obj(p.get('object'))}"
    ),
}


def nx_add_note(
    text: Annotated[list[str], Field(min_length=1, max_length=50)],
    origin: Point2,
    sheet: str | None = None,
    leader_to: str | None = None,
    view: str | None = None,
):
    """Create a native drafting note on a drawing sheet. text is a list of lines (1-50 lines, each at most 1024 characters, not all blank); NX control codes such as <$s> are passed through unchanged. origin=[x,y] is the note's anchor in sheet units (the sheet frame of nx_list_drawings). sheet is a drawing-sheet ID or unique name; default is the current sheet. leader_to optionally attaches a filled-arrow leader to an owned model edge or face ID shown in view (a drawing-view ID); view is required with leader_to and its sheet is used. Returns the annotation ID (usable with nx_list_annotations and nx_edit_annotation), the text NX stored, the actual origin and, with a leader, the attached object, view and the leader point on the sheet."""


def nx_add_center_mark(view: str, edges: Annotated[list[str], Field(min_length=1, max_length=50)]):
    """Create native drafting center marks in a drawing view: one center mark per circular model edge (1-50 distinct owned edge IDs whose circle is visible in the view, typically a hole or boss edge seen along its axis). Returns one annotation ID per mark with its actual center on the sheet (sheet units) and the edge it is associated with; the marks follow the edges when the model changes."""


def nx_add_centerline(
    view: str,
    objects: Annotated[list[str], Field(min_length=1, max_length=2)],
    kind: Literal["2d", "3d"] = "3d",
):
    """Create a native associative drafting centerline in a drawing view. kind=3d: objects is exactly one cylindrical or conical face ID (owned by the work part); NX draws the axis of the face, so use a view in which the axis lies in the view plane (a side view of a hole; for a hole seen end-on use nx_add_center_mark). kind=2d: objects is exactly two linear edge IDs shown in the view (for example the two silhouette or side edges of a slot); NX draws the centerline midway between them. Returns the annotation ID and its native type and the associated objects."""


def nx_add_surface_finish(
    view: str,
    object: str,
    roughness: float,
    origin: Point2,
    symbol: Literal[
        "basic", "material_removal_required", "material_removal_prohibited"
    ] = "material_removal_required",
):
    """Create a native drafting surface texture symbol (ISO 1302 style) with a leader to an owned model edge or face ID shown in view (drawing-view ID). roughness is the Ra value in micrometres (positive, at most 1000) and is written as the text "Ra <value>". symbol chooses the basic symbol, material removal required (bar) or prohibited (circle). origin=[x,y] places the symbol in sheet units. Returns the annotation ID, the symbol type and Ra text NX stored, the actual origin and the leader attachment."""


def nx_add_weld_symbol(
    view: str,
    object: str,
    weld: Literal["fillet", "square_butt", "v_butt"],
    size: float,
    origin: Point2,
    side: Literal["arrow", "other", "both"] = "arrow",
):
    """Create a native drafting weld symbol with a leader whose arrow points at an owned model edge or face ID shown in view (drawing-view ID). weld is the elementary symbol; size (positive, in drawing units, e.g. the fillet leg/throat or groove depth) is written beside it on the chosen side of the reference line: arrow side, other side, or both. origin=[x,y] places the symbol in sheet units. Returns the annotation ID, the symbol and size NX stored per side, the actual origin and the leader attachment."""
