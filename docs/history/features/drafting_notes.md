# Drafting notes and symbols (`drafting_notes`)

Native drawing annotations: notes with optional leaders, center marks,
centerlines, surface texture symbols and weld symbols. Schemas live in
`src/nx_mcp/drafting_notes_server.py`, handlers in `src/nx_mcp/drafting_notes.py`
(`DraftingNotesMixin`). Live acceptance: `acceptance/live/drafting_notes.json`.

Common rules:

- `origin` is `[x, y]` in **sheet units** (the frame of `nx_list_drawings` and
  of the `position` returned by `nx_list_annotations`).
- `view` is a drawing-view ID (or unique view name such as `Top@1`); its sheet
  is opened if it is not current.
- Geometry (`leader_to`, `object`, `edges`, `objects`) is an edge/face ID owned
  by the work part (or a work-assembly occurrence). Face/edge IDs go stale after
  any model change or rejected call: re-find them (`nx_find_geometry`,
  `nx_list_topology`) right before each call.
- Leaders attach to the object at its parametric midpoint (edges) or at an
  interior point of the face clear of holes; the returned `leader.point` is that
  point mapped into the view (sheet units). The leader attachment is read back
  from the native builder and verified.
- Every result returns `annotation` (or `annotations`) IDs of kind
  `annotation`; they are listed by `nx_list_annotations` and can be moved,
  renamed or deleted with `nx_edit_annotation`.

## nx_add_note

`nx_add_note(text, origin, sheet=None, leader_to=None, view=None)`

Multi-line note (`text` = list of 1-50 lines). Without a leader NX creates a
`Note`; with `leader_to` + `view` a `Label` with a filled-arrow leader.

```json
{"tool": "nx_add_note", "args": {"text": ["GENERAL NOTES", "1. BREAK SHARP EDGES 0.5"], "origin": [250, 270]}}
{"tool": "nx_add_note", "args": {"text": ["TOP FACE"], "origin": [200, 230], "leader_to": "<face id>", "view": "<view id>"}}
```

Verified: stored text equals the request line by line, origin exact, leader
attached to the requested object in the requested view.

## nx_add_center_mark

`nx_add_center_mark(view, edges)` - one native `CenterMark` per circular edge
(1-50) seen true-shape in the view (looking along its axis); a circle seen
edge-on or oblique returns `NX_UNSUPPORTED_ARGUMENT`. Each mark is matched to its edge through native associativity and its
center is checked against the arc center mapped into the view. Marks are
associative (verified: blend radius 8 -> 12 moved the mark from [162,207] to
[158,203]). Note: NX drafting preferences may already create automatic center
marks for holes when a view is added; this tool does not remove those.

## nx_add_centerline

`nx_add_centerline(view, objects, kind="3d")`

- `3d`: one cylindrical/conical face; creates a `Centerline3d` along its axis.
  The axis must lie in the view plane - a face seen end-on is rejected with
  `NX_UNSUPPORTED_ARGUMENT` (use `nx_add_center_mark` there).
- `2d`: two linear edges that appear parallel and distinct in the view;
  creates a `Centerline2d` (`FromCurves`) on their mid-line.

The origin NX reports is checked to lie on the projected axis / mid-line.

## nx_add_surface_finish

`nx_add_surface_finish(view, object, roughness, origin, symbol="material_removal_required")`

ISO-1302-style `DraftingSurfaceFinishSymbol` with a leader. `roughness` is Ra
in micrometres (0 < Ra <= 1000) written as `Ra <value>` in native field `a2`;
`symbol` = `basic`, `material_removal_required` or
`material_removal_prohibited`. Symbol type, text and leader are read back.

## nx_add_weld_symbol

`nx_add_weld_symbol(view, object, weld, size, origin, side="arrow")`

Native `LineWeld` with a leader to the edge/face. `weld` = `fillet`,
`square_butt` or `v_butt`; `size` (positive, drawing units) is written beside
the symbol on the `arrow`, `other` or `both` sides of the reference line. The
per-side symbol and size are read back (unused side must read empty). Uses the
part's native weld standard and default style; no tail, field-weld flag or
contour/finish symbols.

## Verified scope (NX 2406)

mm part 80x50x20 block with a diameter-20 through hole and an R8 vertical
blend; A3 mm sheet at 1:1, Top base view plus a projected side view. All five
tools verified with numeric readback and an exported PDF (all symbols placed at
the requested origins, leaders on the intended geometry, text readable).

## Known limits / NX quirks

- `nx_edit_annotation(position=...)` on an associative center mark reports
  success, but NX keeps the mark at its edge's center (the shared tool does not
  read the position back).

- Leader/selection points passed to `SelectObject.SetValue(obj, view, point)`
  are **model coordinates**, not sheet coordinates; sheet coordinates make NX
  snap to an extension of the edge or the face boundary.
- `UF_VIEW_map_model_to_drawing` lives in `UFSession.View` (not `Draw`); curve
  evaluation is `UFSession.ModlGeneral.AskCurveProps`.
- NX enum members stringify as integers; the weld symbol member `None` is not
  reachable as a Python attribute (value 0).
- The surface finish collection is `part.Annotations.DraftingSurfaceFinishSymbol`
  (singular); `DraftingSurfaceFinishSymbols` is the older `DraftingSurfaceFinish`
  collection.
- Center marks, centerlines, surface finish and weld symbols are enumerated by
  the shared annotation inventory (`_documentation_annotations` in
  `assembly_documentation.py`: `Annotations.Centerlines`,
  `DraftingSurfaceFinishSymbol`, `Welds`), so `nx_list_annotations` lists them
  and `nx_edit_annotation` moves, renames or deletes them.
