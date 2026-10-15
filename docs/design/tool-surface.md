# NX MCP tool surface: target design

Status: design of record, implemented. The generated [tool reference](../tools.md) is the
authoritative list of tools and arguments; section 3 groups the tools by domain with their
purpose and rationale. Scope: the public tool surface (names, arguments,
results, errors, profiles) of the catalogue-based re-architecture. Names and
arguments change freely: no aliases, no backwards compatibility.

**Summary.** The catalogue has **261 catalogue tools** in 12 domains (3.13), reached in the
agent profile through 4 gateway tools (2.1). Errors use 22 categories and 72 codes (1.8).
Code layout and the kernel are specified in [architecture.md](architecture.md). The
recommended agent profile costs about 4K tokens (measured: architecture.md 6).

Notation in tables: `a/b/c` is an enum, `?` marks an optional argument, `=x`
gives the default, `T[1..n]` is a bounded list, and `{...}` is an inline object.
Tool names omit nothing: every name is written in full.

---

## 1. Conventions

### 1.1 Catalogue entry

Every tool is defined once, as one `@tool`-decorated function in its domain
package (architecture.md 1.2 has the full field list). The kernel builds the MCP
listing, discovery, panel, evidence gating and bridge dispatch from that entry.

| Field | Content |
|---|---|
| `name` | `nx_<verb>_<object>[_<qualifier>]` (1.2) |
| `domain` | `session`, `parts_files`, `sketch`, `modeling`, `freeform`, `assembly`, `motion`, `inspection`, `drawing`, `sheet_metal`, `display`, `optimization` |
| `effect` | `read`, `view`, `model`, `file` or `session` (1.3). One fixed value per tool, never dependent on arguments |
| `label` | Static name, imperative, at most 48 characters ("Extrude sketch", "Measure distance"); discovery rows, undo-mark name, panel fallback |
| `summary` | Panel line rendered from the arguments (template, per-variant templates or function), at most 60 characters rendered (`Extrude "profile" 10 mm`) |
| `description` | Model-facing text (1.7), the handler's docstring |
| `keywords` | 3-8 synonyms and NX command names used by search ("pad, boss, protrude, cut") |
| `input` / `output` | The handler signature and its returned `TypedDict`, built from the shared types (1.4); stdlib only, the sidecar derives Pydantic models and JSON Schema |
| `requires` | Preconditions checked by the kernel before NX: `work_part`, `no_active_sketch` (an active drafting sheet sketch is deactivated, not refused), `application:sheet_metal`, `drawing_sheet`, `interactive`, `assembly` |
| `batchable` / `previewable` | Whether `nx_run_batch` / `nx_preview_changes` may run it |
| `variants` | Named sub-schemas for discriminated unions (`nx_create_drawing_view#section`) |
| `evidence` | Not written by hand: the acceptance runner records, per NX build, the variants that passed machine-checked scenarios (`evidence/<domain>.json`). A build key present means tested; there is no other visible state (decision 11) |
| `handler` | The decorated function itself (`domains/modeling/datums.py: nx_create_datum_plane`) |
| `cost`, `invalidates`, `refresh_annotations`, `runs`, `example` | Long-operation class, reference impact, managed-annotation refresh, sidecar-only tools, one minimal example (architecture.md 1.2) |

The kernel runs every call, including each batch and preview child, through
fixed gates in this order: schema validation → workspace path validation →
evidence/version gate → target part and target switch (1.10) → `requires`
preconditions → engineer control hold →
part ownership → receipt → undo mark (for `model`) → unit conversion →
handler → read-back verification → reference invalidation → annotation refresh →
result projection → error mapping (architecture.md 3.1). Handlers never repeat
these gates.

### 1.2 Naming and closed verb list

Rules:

- `nx_<verb>_<object>`, lower snake case, singular object (`nx_create_sketch_curves`
  is plural because it creates 1-100 curves in one call).
- One verb per job. A name never contains a mode that an argument can carry
  (no `_advanced`, `_about`, `_on`, `_feature` suffixes).
- Objects use NX terms: `body`, `face`, `edge`, `feature`, `sketch`, `datum_plane`,
  `component`, `assembly_constraint`, `explosion`, `drawing_sheet`, `drawing_view`,
  `dimension`, `annotation`, `drawing_table`, `section_view`.

| Verb | Meaning | Effect |
|---|---|---|
| `list` | Paged inventory of objects of one kind | read |
| `get` | Full detail of one object or of the session | read |
| `find` | Search for geometry by rule (type, normal, radius, nearest) | read |
| `measure` | Exact numeric quantities | read |
| `analyze` | Sampled field data (curvature, draft, derivatives), or a topology study's strength without changing its design | read; model for `nx_analyze_topology_study` |
| `check` | Pass/fail verdicts against tolerances or rules; temporary geometry is always rolled back | read |
| `resolve` | Turn a selector or anchor into a current reference | read |
| `create` | New persistent NX object (feature, sketch, view, annotation, ...) | model |
| `edit` | Change an existing object; omitted fields stay unchanged | model |
| `delete` | Remove objects | model |
| `set` | Assign a state or property idempotently (same call twice = same result) | model, view or session |
| `update` | Re-evaluate derived data from its sources (model update, annotation refresh) | model |
| `activate` | Change the current part, sheet or application | session |
| `finish` | End a mode the agent opened (sketch, preview) | session or model |
| `open`, `close`, `save` | Part files in the NX session | session or file |
| `import`, `export` | Translate between NX and other formats in the workspace | model or file |
| `upload`, `download` | Move bytes between client and workspace | file or read |
| `capture` | Produce an image of the current view | view |
| `restore` | Return to a saved state (checkpoint, display snapshot) | model or view |
| `undo` | Undo the last agent operation | model |
| `redo` | Repeat agent operations an undo or restore reverted, each as its own call | session |
| `preview` | Apply temporarily, report, roll back | session |
| `run` | Execute a batch, a motion sweep or a topology study solve | model; read for `nx_run_motion_sweep` (poses put back) |
| `cancel` | Request cooperative cancellation | session |

The two gateway tools `nx_invoke` and `nx_invoke_read` are the only names
without an object. `add_`, `make_`, `show_`, `rebuild_`, `refresh_`, `recognize_`, `reposition_`,
`rename_`, `suppress_`, `mate_` and `highlight_` no longer exist.

### 1.3 Effects

| Effect | Meaning | Undo mark | Approval guidance for hosts |
|---|---|---|---|
| `read` | Changes nothing in NX, the session or the workspace (snapshots under `.nx-mcp/` excepted) | no | auto-approve |
| `view` | Changes only what the engineer sees (camera, visibility, highlight, captures). Returns `restore_id` when a later restore makes sense | no | auto-approve or approve once |
| `model` | Changes part data. Visible undo mark, rollback on failure, `changes` populated | yes | approve |
| `file` | Writes new workspace files or saves parts. Never overwrites an existing file except a part's own save | no | approve |
| `session` | Changes session state: loaded or active parts, application, control, checkpoints, previews | no | approve |

`nx_set_appearance` is `view` although colours persist when the part is saved;
the result carries `marks_part_modified: true` (open question 9).

### 1.4 Argument types

All types are shared stdlib `Annotated`/`TypedDict` types in `core/types.py`
with extra fields forbidden (the NX side has no Pydantic; the sidecar derives
Pydantic models from them). Fixed-length arrays have `minItems = maxItems`, so
no vector is unbounded.

| Type | JSON shape | Meaning |
|---|---|---|
| `Ref` | `"b:41"` | Opaque NX object reference returned by an earlier call. Never a display name. A ref no tool returned in this NX session fails `NX_NOT_FOUND` (`details.reason: never_issued`, with `issued_as` when the number belongs to a ref of another kind); one numbered below the session's floor (before the last NX start or host reload) fails `NX_STALE_REFERENCE` |
| `Point2` | `[x, y]` | Sketch-local point (part units). The only 2D point shape: no `{x,y}`, no `cx/cy` |
| `SheetPoint` | `[x, y]` | Drawing-sheet point in sheet units |
| `Point3` | `[x, y, z]` | Work-part absolute point in part units; numbers only (no unit strings) |
| `Vec3` | `[x, y, z]` | Nonzero direction; the kernel normalises it and reports the normalised value |
| `Mat3` | `[[r00,r01,r02],[r10,r11,r12],[r20,r21,r22]]` | Right-handed orthonormal rotation, row-major, `p_out = R·p + t` |
| `Pose` | `{translation: Point3, rotation?: Mat3}` or `{axis: "z"/Vec3, angle: Angle, origin?, translation?}` | Absolute placement; rotation defaults to identity. The axis-angle form (right hand, about `axis` through `origin`, then `translation`) becomes the matrix form at gate 9 |
| `Length` | number, `"10 mm"` or `"thickness"` | Plain number: part (work-part) length units; a string with `mm`/`in` is converted (decision 12); an NX expression name or formula is evaluated in the work part |
| `SheetLength` | number or `"10 mm"` | Plain number: sheet units of the target sheet |
| `Angle` | number, `"90 deg"` or `"0.5 rad"` | Plain number: degrees; a string with `deg`/`rad` is converted; an expression name or formula as for `Length` |
| `Units` | `"mm"` or `"in"` | Everywhere (parts, sheets, exports). `"inch"` disappears. Expressions add `"deg"`, `"rad"`, `"unitless"` |
| `Plane` | `Ref` (datum plane or planar face) or `{principal: "xy"/"yz"/"xz"}` or `{origin: Point3, normal: Vec3}` | One plane shape for sketches, mirrors, datums, trims. A bare name (`"XY"`, `"XY plane"`, `"top"`) stands for the `{principal}` form |
| `Axis` | `Ref` (datum axis, linear edge, cylindrical face) or `{principal: "x"/"y"/"z"/"-x"/"-y"/"-z"}` or `{origin: Point3, direction: Vec3}` | One axis shape for revolve, circular patterns, rotations. A bare name (`"-Z"`, `"+y"`, `"z axis"`) stands for the `{principal}` form; a ref-shaped string (`"e:3"`) stays a ref |
| `Direction` | `Vec3` or `Ref` (linear edge, datum axis, planar face normal) or `{principal: "x"/.../"-z"}` | Directions for extrude, draft, patterns; bare names as for `Axis` |
| `SketchPoint` | `{curve: Ref, point: "start"/"end"/"center"/"mid"}` | A characteristic point of a sketch curve |
| `Limit` | `{type: "distance", value: Length}` or `{type: "through_all"}` or `{type: "until_face", face: Ref}` | Extrude and hole limits |
| `Combine` | `{operation: "unite"/"subtract"/"intersect", targets: Ref[1..20]}` | Boolean of a new feature with existing bodies; omitted = new body. A combine that NX leaves undone (the new body does not touch the target, or the tool body stays separate and the target keeps its volume) fails `NX_BUILD_FAILED` and rolls back |
| `Percent`, `Fraction`, `Factor` | number, `"50%"`; `Factor` also `"2x"` | `"50%"` is 50 for a percent and 0.5 for a fraction or factor |
| `Page` | `offset=0`, `limit=20` (max 200) | Every inventory argument pair |

Enum values are lower snake case (`"first_angle"`, `"mmc"`, `"profile_of_a_surface"`).
Gate 1 also accepts natural spellings and passes the canonical value on: an option in any
case and with spaces, `-` or `_` (`"Through All"`), a declared alias or plural, `"true"`/
`"false"` for a boolean, a numeric string for a number and one string for a list of
strings. Wherever a quantity is expected (lengths, angles, counts), an NX expression name or
formula (`"thickness"`, `"thickness/2"`) is accepted; the feature stays driven by it. A
quantity given as a unit string or a formula is checked against the field's range once it
is converted (gate 9), like a plain number at gate 1. An
unknown option or field fails with a did-you-mean (`details.accepted` lists the fields).
Every invalid, unknown or missing field of a call is reported in one error: `field` and
`message` name the first, `details.errors` lists all as `{field, message}`. A variant may
require optional fields (`required_by`, architecture.md 1.2; on `nx_create_datum_plane`,
`nx_create_datum_axis`, `nx_create_sketch_corner`, `nx_create_sketch_copy`,
`nx_create_component`, `nx_create_drawing_dimension`, `nx_create_drawing_symbol` and
`nx_create_drawing_table`): a call without them is refused before NX, and
`nx_get_tool_schemas` lists them (2.1). A tool with `variant_from` takes an
omitted variant from the referenced object (`nx_edit_drawing_table` without `kind`).

### 1.5 Reference parameter names

| Role | Name | Replaces today |
|---|---|---|
| The one object acted on, by kind | `body`, `face`, `edge`, `feature`, `sketch`, `component`, `view`, `sheet`, `dimension`, `annotation`, `explosion`, `section`, `table`, `constraint`, `pattern` | `sketch_id`, `sketch_name`, `name` (for features), `object_id`, `base_view`, `parent_view`, `drawing`, `traceline`, `parts_list` |
| A list of one kind | `bodies`, `faces`, `edges`, `features`, `curves`, `components` | `targets` (booleans), `objects` (when one kind) |
| Mixed kinds | `objects` | unchanged |
| Two operands of a measure, constraint or dimension | `first`, `second` | `obj1/obj2`, `curve1/curve2`, `line1/line2`, `object1/object2`, `first/second` |
| Body receiving a boolean or feature | `target` (one) / `targets` (several, inside `Combine`) | `targets[0]`, `target_body`, `body` |
| Bodies or planes applied to a target | `tools` (bodies) / `tool` (plane or face) | `targets[1:]`, `tool` |
| Parent of a new drawing view | `parent` | `base_view`, `parent_view` |
| Workspace path | `path` | `part_path`, `output_path` |
| Name of a new object | `name` | unchanged, but never used to look up an existing object |
| Service tokens (not NX objects) | `operation_id`, `result_id`, `preview_id`, `restore_id`, `checkpoint_id` | only these keep an `_id` suffix |

Lookup by name is a `list`/`find` job; mutating tools accept only `Ref`.

### 1.6 Result shape

Every tool returns the same envelope. Tool-specific data lives in `data`.

```json
{
  "ok": true,
  "tool": "nx_create_extrude",
  "operation_id": "op_7f3a",
  "mutation_outcome": "committed",
  "units": {"length": "mm", "angle": "deg"},
  "frame": "work_part_absolute",
  "data": {"feature": {"id": "f:12", "kind": "feature", "name": "EXTRUDE(4)", "part": "p:1"}},
  "changes": {
    "created": [{"id": "f:12", "kind": "feature", "name": "EXTRUDE(4)", "part": "p:1"}],
    "modified": [{"id": "b:3", "kind": "body", "name": "", "part": "p:1"}],
    "deleted": [],
    "references": "preserved"
  },
  "warnings": [{"code": "NX_UPDATE_WARNING", "message": "..."}],
  "page": null,
  "result_id": "res_91c2",
  "omitted": {}
}
```

| Field | Rule |
|---|---|
| `ok` | `true` on success, `false` on error (then `error` replaces `data`) |
| Reference | `{id, kind, name, part}` plus `path` for assembly occurrences. Repeated reference metadata is omitted in compact detail |
| `mutation_outcome` | Always present (1.9) |
| `units`, `frame` | Always present when any length, angle or coordinate is returned. `frame` is `work_part_absolute`, `sketch_local`, `sheet` or `display_part_absolute` |
| `changes` | `model` tools only. Refs follow NX's object identity, so after a commit only refs of objects NX no longer has go stale. `references`: `preserved` (all earlier refs still valid), `listed` (only the refs in `deleted` and in `stale_refs` are invalid), `part_invalidated` (re-list the part). `stale_refs` (up to 50) and `stale_references` (their count) name earlier refs that went stale without a `deleted` row (consumed faces and edges, objects an undo removed); `stale_kinds: ["face", "edge"]` appears only when the services cannot tell which faces and edges died. A deleted object that never had a ref is a row without `id`, with `gone: true`; unnamed internal NX objects are only counted (`internal: {created, deleted}`). `nx_undo_operation` and `nx_restore_checkpoint` keep the refs of every object that remains (the part, components, untouched features, faces and edges) and report `preserved` or `listed`. An engineer's edit between calls re-validates refs the same way instead of dropping them. After a rollback that renumbered refs, `references` is `part_invalidated` and `stale_references` gives how many refs went stale; the error then carries `details.references: "renumbered"`, `details.stale_references` (a count, or `"all"` when NX cannot say which), `details.stale_refs` (up to 20 ids) and `details.rebound_references` (refs re-bound to their restored objects), and its hint says to list the objects again |
| `warnings` | `{code, message}`; never dropped by compaction. Warning codes (`NX_UPDATE_WARNING`, `NX_APPROXIMATED`, `NX_PARTIAL_PAGE`, `NX_MODAL_DIALOG` = NX showed a modal dialog during the call, `NX_VIEW_OVERLAP` = drawing views overlap, `NX_NO_EFFECT` = the call succeeded but changed nothing visible yet, `NX_NOTE` = an informational note) are a separate short list |
| `page` | Inventories: `{offset, limit, returned, total, next_offset}` |
| `result_id`, `omitted` | Compact projection: arrays over 20 items are cut and floats rounded to 12 significant digits. `created`/`modified` keep their rows even when `data` shows them (`changes` alone say what a call changed): the rows `data` names at its top level when it names the tool's main objects there (feature, body, sketch...), else the rows that are not NX helpers (auto `pNN` expressions, sketch `ENTITY` geometry); `deleted` drops the rows `data` shows and keeps the others. A kept list of more than 3 rows, and a committed mutation's top-level list of more than 3 body references in `data` (seed or target body first), show their first and last row (`omitted` says `shown: first_last`); `changes.counts` gives every row of a cut list by kind. `nx_get_result` expands what was cut; one snapshot store for bridge and sidecar results |
| `part` | `{id, file, from}`: the part the call acted on (`from`: `work`, `only`, `refs`, `part`, `arg`, `resolver`, `parent`, `any`), when the call named its part or two or more parts are loaded (gate 3t) |
| `restore_id` | `view` tools that change display state |
| `marks_part_modified` | When a non-model call nevertheless marks a part modified |

### 1.7 Description style and length budget

Tools are explicit and schemas detailed. A failed call and its retry cost about 1-3K
tokens; about 100 more schema characters cost about 26 tokens per fetch. The limits below
catch bloat (repetition, filler, bytes no agent uses), never a rule an agent needs: when
such a rule does not fit, the limit changes, with the reason.

| Item | Budget | Rule |
|---|---|---|
| Description first sentence | at most 120 characters | What the tool does in engineer terms, starting with a verb |
| Whole description | at most 500 characters | Then the non-obvious constraints: preconditions, which reference to reacquire, the gotchas an agent would otherwise learn from a failed call. No units or field docs (fields carry them) and no advertising ("native", "associative" only when it changes behaviour) |
| Field description | at most 120 characters | Units, frame, range, the field's own gotcha |
| Tool definition (name + description + input schema) | cap 1,600 characters; cap 3,000 for discriminated unions; the 75th percentile of each kind checked for drift | Checked by `tests/sidecar/test_budget.py` (architecture.md 6) |
| Panel label / summary | label at most 48 characters; rendered summary at most 60 | Separate fields; the summary is never shown to the model |
| Examples | one minimal example per tool | Returned by `nx_get_tool_schemas`, not in the listing |

Cross-references name the tool to call next only when the agent cannot infer it:
"Reacquire faces with `nx_find_geometry` after this edit."

### 1.8 Error taxonomy

Error shape:

```json
{
  "ok": false,
  "tool": "nx_create_blend",
  "operation_id": "op_7f3b",
  "mutation_outcome": "rolled_back",
  "error": {
    "category": "geometry_failed",
    "code": "NX_BUILD_FAILED",
    "message": "Blend radius 8 mm exceeds the adjacent face width",
    "hint": "Use a radius below 4.9 mm or blend fewer edges",
    "field": "/radius",
    "retry": "after_fix",
    "nx_code": 670015,
    "details": {}
  },
  "changes": {"references": "preserved"}
}
```

`category` is what the agent branches on; `code` is stable and specific;
`retry` tells the agent what to do; `nx_code` preserves the native code.
All codes start with `NX_`.

| # | Category | Codes | `retry` | Agent action | Absorbs current codes |
|---|---|---|---|---|---|
| 1 | `invalid_argument` | `NX_INVALID_ARGUMENT`, `NX_INVALID_FORMULA` | after_fix | Fix the argument named in `field`; same call never succeeds | `NX_INVALID_ARGUMENT`, `NX_INVALID_REQUEST`, `NX_INVALID_FORMULA`, `NX_INVALID_STEP`, `NX_CATALOG_INVALID` |
| 2 | `unsupported` | `NX_UNSUPPORTED_OPTION`, `NX_UNSUPPORTED_GEOMETRY`, `NX_UNSUPPORTED_FILE_TYPE`, `NX_NOT_RUNNING` (the operation already finished: nothing to cancel) | never | Choose another tool or approach | `NX_UNSUPPORTED_ARGUMENT`, `NX_UNSUPPORTED_EDIT`, `NX_UNSUPPORTED_SCOPE`, `NX_UNSUPPORTED_UNITS`, `NX_UNSUPPORTED_FRAME`, `NX_UNSUPPORTED_GEOMETRY`, `NX_UNSUPPORTED_IMAGE`, `NX_UNSUPPORTED_FILE_TYPE`, `NX_NOT_IMPLEMENTED` |
| 3 | `not_found` | `NX_NOT_FOUND`, `NX_FILE_NOT_FOUND`, `NX_RESULT_EXPIRED`, `NX_UNKNOWN_TOOL` | after_fix | Re-list; for expired results re-read (never repeat a mutation) | `NX_NOT_FOUND`, `NX_OBJECT_NOT_FOUND`, `NX_FILE_NOT_FOUND`, `NX_DIRECTORY_NOT_FOUND`, `NX_RESULT_EXPIRED`, `NX_TOOL_NOT_FOUND`, `NX_NO_TABLE_HEADER` |
| 4 | `stale_reference` | `NX_STALE_REFERENCE`, `NX_STALE_CHECKPOINT`, `NX_STALE_PREVIEW`, `NX_STALE_SOURCE` | after_reacquire | Reacquire references (`nx_list_objects`, `nx_find_geometry`, `nx_resolve_reference`) and retry | `NX_OBJECT_STALE`, `NX_STALE_REFERENCE`, `NX_SAVED_REFERENCE_STALE`, `NX_STALE_TRACE_ANCHOR`, `NX_STALE_ANNOTATION_SOURCE`, `NX_DISPLAY_SNAPSHOT_STALE`, `NX_CHECKPOINT_STALE`, `NX_PREVIEW_STALE`, `NX_SOURCE_CHANGED`, `NX_ARTIFACT_CHANGED`, `NX_EXPLOSION_ASSEMBLY_CHANGED` |
| 5 | `wrong_kind` | `NX_WRONG_KIND`, `NX_WRONG_GEOMETRY` | after_fix | Pass a reference of the kind or geometry the field names | `NX_OBJECT_TYPE_MISMATCH`, `NX_NOT_SOLID`, `NX_NOT_SHEET`, `NX_NON_PLANAR`, `NX_NOT_SHEET_METAL`, `NX_NOT_DIRECTORY`, `NX_INVALID_GEOMETRY` (input side) |
| 6 | `wrong_owner` | `NX_WRONG_OWNER`, `NX_WRONG_PART_CONTEXT` | after_fix | Use geometry of the right body/part; with `part`, name the part the refs belong to, or drop it (1.10) | `NX_OBJECT_OWNER_MISMATCH`, `NX_DISPLAY_PART_MISMATCH`, `NX_PART_CONTEXT`, `NX_EXPLOSION_VIEW_MISMATCH` |
| 7 | `ambiguous` | `NX_AMBIGUOUS_REFERENCE`, `NX_TARGET_REQUIRED`, `NX_AMBIGUOUS_PART` (two or more of the agent's parts loaded, components included, and a call without refs names no `part`; the hint lists the files with a neutral example, `details.loaded_parts`, `details.count`) | after_fix | Pass the explicit reference; `details.candidates` lists choices; for a part add `part` (a file name) to the arguments | `NX_AMBIGUOUS_REFERENCE`, `NX_AMBIGUOUS_GEOMETRY`, `NX_AMBIGUOUS_TARGET`, `NX_AMBIGUOUS_THREAD`, `NX_OBJECT_OWNER_AMBIGUOUS`, `NX_NO_TARGET_BODY` |
| 8 | `precondition` | `NX_SKETCH_ACTIVE`, `NX_SKETCH_NOT_ACTIVE`, `NX_NO_WORK_PART`, `NX_WRONG_APPLICATION`, `NX_NOT_LOADED`, `NX_UNSAVED_PART`, `NX_PART_IN_USE` | after_fix | Run the tool named in `hint` (finish sketch, activate part, save, load components), then retry | `NX_SKETCH_ACTIVE`, `NX_SKETCH_NOT_ACTIVE`, `NX_NO_ACTIVE_SKETCH`, `NX_NO_PART_OPEN`, `NX_NO_WORK_PART`, `NX_NO_DISPLAY_PART`, `NX_DRAWING_CONTEXT`, `NX_DRAWING_ACTIVE`, `NX_NO_DRAWING`, `NX_NO_COMPONENTS`, `NX_UNLOADED_COMPONENT`, `NX_ASSEMBLY_NOT_LOADED`, `NX_UNSAVED_PART`, `NX_PART_IN_USE`, `NX_APPLICATION_UNAVAILABLE` |
| 9 | `already_exists` | `NX_ALREADY_EXISTS`, `NX_NAME_CONFLICT` | after_fix | Choose another name or path, or use the existing object in `details` | `NX_FILE_EXISTS`, `NX_ALREADY_EXISTS`, `NX_NAME_EXISTS`, `NX_NAME_CONFLICT`, `NX_IMPORT_NAME_CONFLICT`, `NX_MATERIAL_EXISTS`, `NX_SECTION_EXISTS`, `NX_PARTS_LIST_EXISTS` |
| 10 | `blocked` | `NX_DEPENDENCY_ORDER`, `NX_IN_USE`, `NX_READ_ONLY`, `NX_CROSS_PART`, `NX_PART_BUSY` (closing or activating another agent's part, or discarding recent unsaved work on a shared connection; `force` goes ahead), `NX_ENGINEER_PART` (closing, activating or saving the engineer's part; no override) | after_fix | Resolve the blocker listed in `details` (dependent views, parent order, locked expression); ask the engineer when it is their data | `NX_REORDER_DEPENDENCY`, `NX_REORDER_REJECTED`, `NX_EXPLOSION_IN_USE`, `NX_EXPRESSION_READ_ONLY`, `NX_CROSS_PART_ROLLBACK`, `NX_RESTORE_ORDER`, `NX_NOT_CANCELLABLE` |
| 11 | `geometry_failed` | `NX_BUILD_FAILED`, `NX_UPDATE_FAILED`, `NX_INCOMPLETE_RESULT` | after_fix | Model rolled back; change dimensions or inputs (smaller radius, other face). Do not repeat unchanged | `NX_OPERATION_FAILED`, `NX_UPDATE_FAILED`, `NX_FACE_EDIT_FAILED`, `NX_INCOMPLETE_SEW`, `NX_SINGULAR_SURFACE`, `NX_SHEET_METAL_INVALID`, `NX_SKETCH_INVALID`, `NX_DRAWING_INVALID`, `NX_ANNOTATION_INVALID`, `NX_INTERFERENCE_UNRESOLVED`, `NX_IMPORT_NO_OUTPUT`, `NX_EXPORT_NO_SOLIDS`, `NX_MOTION_RESULT_MISSING`, `NX_MOTION_NOT_ASSOCIATIVE` |
| 12 | `solver_failed` | `NX_CONSTRAINT_UNSATISFIED`, `NX_SKETCH_OVERCONSTRAINED` | after_fix | Diagnose with `nx_check_sketch` or `nx_list_objects(kind=assembly_constraint)`; remove the conflicting constraint | `NX_CONSTRAINT_UNSATISFIED`, `NX_RELATION_UNSATISFIED`, `NX_SKETCH_CONFLICT`, `NX_CONSTRAINT_REMOVED` |
| 13 | `verification_failed` | `NX_VERIFICATION_FAILED` (`details.check` names the failed read-back) | ask_engineer | NX built something that differs from the request; it was rolled back. Report, do not loop | `NX_VERIFICATION_FAILED`, `NX_PATTERN_VERIFICATION_FAILED`, `NX_SAVE_VERIFICATION_FAILED`, `NX_MATERIAL_VERIFICATION_FAILED`, `NX_PLACEMENT_MISMATCH`, `NX_PLACEMENT_CHANGED`, `NX_EXPLOSION_POSITION_MISMATCH`, `NX_FRAME_MISMATCH`, `NX_CAPTURE_RESOLUTION_MISMATCH`, `NX_CLONE_REFERENCE_MISMATCH` |
| 14 | `limit_exceeded` | `NX_TOO_MANY_OBJECTS`, `NX_REQUEST_TOO_LARGE`, `NX_RESPONSE_TOO_LARGE` | after_fix | Split the request or page smaller | `NX_OBJECT_LIMIT`, `NX_SAMPLE_LIMIT`, `NX_PAIR_LIMIT`, `NX_REQUEST_TOO_LARGE`, `NX_RESPONSE_TOO_LARGE`, `NX_IMAGE_TOO_LARGE` |
| 15 | `path_rejected` | `NX_PATH_OUTSIDE_WORKSPACE`, `NX_PATH_RESERVED` | after_fix | Use a workspace-relative path (`nx_get_status` shows the root) | `NX_PATH_OUTSIDE_WORKSPACE`, `NX_PATH_RESERVED` |
| 16 | `file_failed` | `NX_IMPORT_FAILED`, `NX_EXPORT_FAILED`, `NX_SAVE_FAILED`, `NX_CAPTURE_FAILED`, `NX_TRANSFER_FAILED` | after_fix | Read `details` (translator log, checksum); fix input; ask the engineer for save failures | `NX_IMPORT_FAILED`, `NX_EXPORT_FAILED`, `NX_SAVE_FAILED`, `NX_SAVE_PREFLIGHT_FAILED`, `NX_CLONE_FAILED`, `NX_CAPTURE_FAILED`, `NX_SCREENSHOT_FAILED`, `NX_COMPONENT_LOAD_FAILED`, `NX_DIRECTORY_ERROR`, `NX_CHECKSUM_MISMATCH`, `NX_UPLOAD_GAP`, `NX_UPLOAD_CONFLICT`, `NX_RECEIPT_UNREADABLE`, `NX_RESULT_STORAGE_FAILED` |
| 17 | `unavailable` | `NX_LICENSE_UNAVAILABLE`, `NX_VERSION_UNSUPPORTED`, `NX_VIEWPORT_UNAVAILABLE`, `NX_CATALOG_UNAVAILABLE` | never | Tell the engineer which license or build is missing; use an alternative if one exists | `NX_LICENSE_UNAVAILABLE`, `NX_UNAVAILABLE_ON_VERSION`, `NX_API_UNAVAILABLE`, `NX_VIEWPORT_UNAVAILABLE`, `NX_CATALOG_UNAVAILABLE`, `NX_UNDO_UNAVAILABLE` |
| 18 | `engineer_control` | `NX_CONTROL_DECLINED`, `NX_CONTROL_PAUSED`, `NX_UI_BUSY`, `NX_PART_NOT_GRANTED`, `NX_MODAL_DIALOG_OPEN` | ask_engineer | The engineer has NX. Ask in chat before retrying; `NX_UI_BUSY` = wait and retry; `NX_PART_NOT_GRANTED` = the part is the engineer's: ask them to grant it on the panel or work on a copy; `NX_MODAL_DIALOG_OPEN` = a modal NX dialog blocks NX: open as a non-read step starts, opened during a running call (that call: outcome unknown; every queued or new call: answered at once, not started; `details.blocked_call` names the call that opened it; `nx_get_status` shows `control.blocked_by_dialog`), or seen by the sidecar after a timeout (`details.dialogs`; reads still answer; an engineer's command or dialog first delays the step as for `NX_UI_BUSY`): ask the engineer to answer it, the agent never clicks it | `NX_UI_DECLINED`, `NX_UI_PAUSED`, `NX_UI_BUSY` (the ownership gate is new) |
| 19 | `bridge` | `NX_BRIDGE_UNAVAILABLE`, `NX_BUSY` (NX runs, but a call waited `QUEUE_TIMEOUT` behind another call and did not start: `details.running` names that tool), `NX_PROTOCOL_MISMATCH`, `NX_AUTH_FAILED` | after_wait | `NX_BRIDGE_UNAVAILABLE`: NX or the bridge is not running or was restarted (the sidecar says which: NX process gone, or NX running with its host not answering); `NX_BUSY`: retry later, nothing ran; a call NX started but did not finish within its run time is `NX_OUTCOME_UNKNOWN`; for mutations check `nx_get_operation` first; ask the engineer if it persists | `NX_BRIDGE_UNAVAILABLE`, `NX_PROTOCOL_ERROR`, `NX_PROTOCOL_VERSION_MISMATCH`, `NX_AUTH_FAILED`, `NX_MAIN_THREAD_UNAVAILABLE`, `NX_BRIDGE_OUTCOME_UNKNOWN` |
| 20 | `interrupted` | `NX_CANCELLED`, `NX_OUTCOME_UNKNOWN`, `NX_IDEMPOTENCY_CONFLICT` (also: the same id and arguments in another part), `NX_CALL_EXPIRED` (the call's `expires_at` passed before NX ran it: not started, no receipt; `retry: after_fix`) | after_status_check | Query `nx_get_operation(operation_id)`; never retry a mutation blindly; reuse an `operation_id` only with identical arguments in the same part | `NX_CANCELLED`, `NX_IDEMPOTENCY_CONFLICT`, `NX_BRIDGE_OUTCOME_UNKNOWN` |
| 21 | `rollback_failed` | `NX_ROLLBACK_FAILED`, `NX_RESTORE_FAILED` | ask_engineer | The model or view may be partly changed. Stop mutating, capture a screenshot, tell the engineer | `NX_ROLLBACK_FAILED`, `NX_SAVE_VIEW_RESTORE_FAILED`, `NX_RENDER_RESTORE_FAILED` |
| 22 | `nx_error` | `NX_ERROR` | ask_engineer | Unclassified native exception; `nx_code` and message preserved. Retry once only after changing inputs | `NX_API_ERROR`, `NX_INTERNAL_ERROR` |

`NX_MCP_*` strings in the code are environment variables and marker names, not
error codes. The code list has 72 entries (the "Codes" column); a
catalogue test fails when a handler raises a code that is not in the list.
A handler may override `retry` for one error (`fail(..., retry="never")`) when no fix
of the same call can succeed, e.g. `nx_undo_operation` with an empty history.
Tracebacks never reach the agent or `error.details`, not even with `NX_MCP_DEV=1`: they go
to the operation receipt (`traceback`) and `.nx-mcp/logs/tracebacks.log`. A path that already
exists where a new file goes fails `NX_ALREADY_EXISTS` with `details.suggested_path`; a path
without a suffix gets the tool's suffix (`parts/plate` → `parts/plate.prt`). Path errors show
paths workspace-relative (`details.path`, `suggested_path`), never the host's absolute path.

### 1.9 `mutation_outcome`

Present in every result, success or error.

| Value | Meaning | References | Agent may |
|---|---|---|---|
| `not_applicable` | Nothing in NX changed: tool effect `read`, or a `view` call that changes nothing in the session, such as `nx_capture_view` (it writes a PNG to the workspace and puts the view back) | preserved | continue |
| `committed` | Every change applied and verified | `changes.references` says which survive | continue |
| `previewed` | Changes applied temporarily and reverted (`nx_preview_changes`) | refs in the preview result are stale | accept or discard the preview |
| `not_started` | Failed before NX, the session or the workspace changed | preserved | fix and retry |
| `rolled_back` | Started, failed, undo mark restored | `changes.references` = `preserved` or `part_invalidated` (with `changes.stale_references`; the error's `details.references` = `renumbered`, 1.6) | fix, reacquire if needed, retry |
| `partial` | Some changes remain (rollback failed, or a non-undoable step such as a translator file or a session change ran) | `part_invalidated` | inspect (`nx_check_model`, screenshot); ask the engineer before more mutations |
| `unknown` | Transport lost or timeout; NX may still finish | unknown | query `nx_get_operation(operation_id)` only; never retry until it reports `committed` or `failed` |

`operation_id` is optional; the kernel generates one when absent and returns it. A caller's own id is 8-128 characters from `A-Z a-z 0-9 _ -` (listed schemas carry `minLength` and `pattern`); a bad one is refused before NX with what is wrong ("too short: 7 of at least 8 characters").
Supply your own when you might need to retry after a transport failure.

### 1.10 Several parts and parallel agents

The full rules are in [architecture.md 3.4](architecture.md#34-several-parts-and-parallel-agents).
What an agent needs:

- A call acts on the part its refs name. It never needs `part`; a `part` it gives must
  name the same part (reads too), else `NX_WRONG_PART_CONTEXT`.
- A call without refs acts on the work part while at most one of the agent's parts is
  loaded (the `owned` rows of `nx_list_parts`, components included; never a part the
  engineer opened). With two or more it needs `part`, else `NX_AMBIGUOUS_PART` (the hint
  lists the files). The load that brings the second part says so (`NX_NOTE` "2 parts are
  loaded: calls without refs now take part"). There is no mode and no time window.
- Never activate or display a part to target it: calls reach a part by refs or `part`,
  and `nx_activate_part` only shows a part to the engineer.
- Tabs: every part NX MCP shows gets its own NX window tab; no call replaces the part of
  another tab. **Agents never take the engineer's active tab**: while the displayed part
  is the engineer's, `nx_create_part`, `nx_open_part`, `nx_activate_part` (and an import
  into a new part, a clone with `activate`) leave the engineer's tab and work part active
  whatever `display` and `work` say; `display: true` (and `nx_activate_part` by default)
  gives the part its own tab behind it, else it stays in the background; `data.display`
  and `data.work` are false and an `NX_NOTE` says where the part is and how calls reach
  it (`"part": "<file>"`). A call without refs or `part` then acts on the agent's only
  part. Without an engineer part displayed: with two or more agent parts loaded,
  `nx_create_part` does not display the new part, and `nx_open_part` / `nx_activate_part`
  give the part its tab but keep the active tab (`display: true` leaves the part's tab
  active, `false` gives it no tab; `work: true` without display becomes the work part only
  where NX allows it, never a refusal). Closing the active tab brings the previously
  active tab back (`data.displayed`).
- Closing an assembly unloads only component parts it brought in itself; parts created
  or opened by name stay loaded until closed (`kept_components`, reason `created or
  opened by name`).
- **Whose part** (architecture 3.4 "Part ownership"): the sidecar tells NX who sends each
  call (`caller`: its process, MCP session and an id the client gives in `_meta`). The
  engineer's parts (`owned: false`) are never closed, activated or saved by an agent
  (`NX_ENGINEER_PART`; save only when the engineer granted the part); reads still read
  them. Another agent's part is not closed or activated without `force`
  (`NX_PART_BUSY`, `details.reason: other_agent`). Agents sharing one sidecar have one
  caller id: their closes are refused only when they would discard another call's recent
  unsaved work (`reason: recent_change`, the message names the part); your own parts
  close without `force`.
- The kernel makes the part current for the call (a model call on a background part
  runs there while the display stays; drawing, capture, camera, explosion and Sheet
  Metal calls work in the part's own tab, also when the part they name is NX's work part
  without being displayed, e.g. beside the engineer's displayed part) and restores the engineer's tab, work part and
  application afterwards. A restore NX refused is an `NX_UPDATE_WARNING`; drawing views
  of a part that is not displayed refresh later (`NX_NOTE`).
- `envelope.part` (1.6) names the part when `part` was given or two or more parts are
  loaded. In `nx_run_batch` and `nx_preview_changes` each step acts on its own part (its
  refs, its own `part`, else the batch's part), a step result in another part carries
  `part`, and one undo mark rolls back every part.
- Undo, redo and checkpoints act per part; the engineer's edits end every part's undo
  history. When other work blocks NX's undo (other parts' later operations, a load, a
  close or evicted marks), the default undo compensates when the operations
  qualify, else it refuses with a hint; it never crosses the engineer's edits.
- `nx_get_status` `control`: `waiting` = `queue.waiting` (agent calls waiting for NX) and
  `busy` agrees with it; `engineer_busy` and `waiting_for_answer` are the engineer-only
  and Ask me first counts.

**Call keys inside `arguments`**, like `operation_id`. They are not schema properties,
also not in the strict listed schemas of the core tools, which take them all the same
(the server does not validate input against the listed schemas). A tool's own `part`
parameter (`nx_activate_part`, `nx_save_part`, `nx_close_part`) stays its argument.

| Key | Value |
|---|---|
| `part` | A loaded part's file name with or without `.prt`, its workspace path, or `p:N` |
| `expires_at` | Epoch seconds or ISO 8601. A changing call that reaches NX later never runs (`NX_CALL_EXPIRED`); reads still answer. The sidecar sets send time + 135 s unless the agent passed its own |
| `caller` | Set by the sidecar on every NX call, never by the agent (one it passes is replaced): `{"id", "shared"}` (architecture 3.4) |

**Error codes.**

| Code | When |
|---|---|
| `NX_AMBIGUOUS_PART` | A call without refs and without `part` while two or more agent parts are loaded ("N agent parts are loaded (and M of the engineer's)", `details.loaded_parts`, `details.count`); refs in several parts that do not decide; two loaded parts share the file name in `part` |
| `NX_WRONG_PART_CONTEXT` | `part` contradicts the refs or the call's own object; NX refused to make the part current (not started) |
| `NX_NOT_FOUND`, `NX_PATH_OUTSIDE_WORKSPACE`, `NX_INVALID_ARGUMENT` | `part` names no loaded part, a path outside the workspace, or is malformed (field `/part`) |
| `NX_CALL_EXPIRED` | The call's `expires_at` passed, or its client closed the connection, before NX ran it: not started, no receipt |
| `NX_ENGINEER_PART` | `nx_close_part`, `nx_activate_part` or `nx_save_part` (not granted) of the engineer's part; no `force`, development mode included (`details.action`) |
| `NX_PART_BUSY` | `nx_close_part`/`nx_activate_part` of another agent's part (`reason: other_agent`), or on a shared connection a `save: false` close of a part with unsaved changes from the last 300 s that a call worked in within 30 s (`reason: recent_change`); `force: true` goes ahead |
| `NX_CROSS_PART` | An undo or restore that other parts' later work blocks and that deletion cannot replace (`details.in_the_way`) |
| `NX_IDEMPOTENCY_CONFLICT` | The same `operation_id` and arguments in another part |

---

## 2. Surface profiles

### 2.1 How agents see tools

| Profile | Listed directly | Reached through gateway | Use |
|---|---|---|---|
| `agent` (default, recommended) | 4 gateway tools + 14 core tools | all 261 catalogue tools | Any MCP client; smallest context, read-only gateway can be auto-approved |
| `full` | the 4 gateways and every catalogue tool tested on the connected build, with `readOnlyHint`/`destructiveHint` from `effect` | none needed | Clients with native deferred tool search (they index names and load schemas on demand) |

Gateway tools (always listed):

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_find_tools` | A scored OR search after stemming, stop words and synonym expansion ("fillet" finds `nx_create_blend`, "mate" finds `nx_create_assembly_constraint`): every tool matching at least one query word is a hit. Per word: the best field (name > keyword/label > variant > description; a synonym or a prefix counts less than the word itself, plus a little when the word itself is in the name, a keyword or the label) times the word's rarity `1 + log2(N/n)` over the catalogue (a word few tools match, "save", outweighs a common one, "part"). The sum is scaled by the share of query words matched (a tool matching every word beats partial ones of similar weight), plus bonuses for the share of the tool's name words named, a keyword or label phrase in the query (a whole one-word keyword counts less: "pocket" finds `nx_create_extrude`; a query that is exactly one of the tool's keyword phrases or its label counts most: "create assembly" finds `nx_create_part`), a word naming the tool's domain and, when the query has an action word (create, make, add, new, set, insert, build, place, define, generate), a tool whose verb is `create` or `set` over one that reads, lists, exports or activates ("create expression" finds `nx_set_expression` before `nx_export_expressions`); an exact name wins. `query` is 1-4 keywords for one task; `queries` (1-20, exclusive with `query`) returns `results: [{query, tools, total}]`, 5 rows per query by default. With two or more words each row lists the words it `matched`; `partial: true` means no tool matched every word; a `hint` says to split the tasks into `queries` when a query has more than five words (stop words not counted), or three or more, is partial and joins tasks (`,`, `;`, `+`, `&`, "and", "then", "also"); a `note` says what to do for a word naming something NX MCP does as another tool ("pocket": a sketch and a subtracting extrude). An unknown tool name (`nx_fillet_edges`, `NX_UNKNOWN_TOOL`) is answered with a typo match, the tools its words find and close spellings (`details.close_matches`). Never just empty: when no word matches, the close spellings of names, labels and keywords are searched instead (`did_you_mean`); if none, the domain overview comes with a hint. An empty query returns the domain overview (tool counts and key tools). Rows: name, summary, effect, domain, evidence for the connected build; `call_with` says to call them through `nx_invoke`/`nx_invoke_read` | `query` or `queries[1..20]`, `domain?`, `effect?`, `limit` (10, or 5 per query; max 20), `offset` | read |
| `nx_get_tool_schemas` | Exact input schema, example, prerequisites and evidence for 1-10 tools or variants in one call; output schema on request. A tool whose variants need fields gets a compact `required_by` map next to `input_schema`, one key per group of offered variants needing the same fields (`{"hole": ["view", "origin"], "title_block\|revision": ["sheet", "rows", "widths"]}`; variants needing nothing are left out); a `tool#variant` schema lists them in `required`. One `tool#variant` is a self-contained entry. Two or more variants of one tool in one call come as one entry at the first one's place: `{"name": tool, <keys they share: description, effect, requires, evidence>, "input_schema": <what their schemas share>, "variants": {"<v>": {"input_schema": <v's own properties, required, x-variant>, <v's own keys: example>}}}`; the schema of `tool#v` is `input_schema` with `variants.<v>.input_schema`'s properties added and its `required` appended (2026-10-13: three drawing-view variants in 2,760 characters instead of 7,529 in three calls) | `names[1..10]` (e.g. `nx_create_drawing_view#section`), `include_output=false` | read |
| `nx_invoke` | Call any task tool; kernel gates apply; `intent` (at most 120 characters) is shown in the NX panel | `tool`, `arguments`, `intent?`, `detail=compact/full` | per tool (declared destructive) |
| `nx_invoke_read` | Same, but refuses tools whose effect is not `read` before validation; hosts can auto-approve it | `tool`, `arguments`, `detail` | read |

Core tools listed directly in the `agent` profile (most-used, mostly read):
`nx_get_status`, `nx_get_operation`, `nx_get_result`, `nx_list_parts`,
`nx_open_part`, `nx_list_components`, `nx_list_objects`, `nx_get_model_summary`,
`nx_find_geometry`, `nx_measure_distance`, `nx_check_model`, `nx_capture_view`,
`nx_undo_operation`, `nx_download_file`. The seven that act on a part without refs
(`nx_list_components`, `nx_list_objects`, `nx_get_model_summary`, `nx_find_geometry`,
`nx_check_model`, `nx_capture_view`, `nx_undo_operation`) list the `part` call key in
their schemas ("Target part's file name; needed when 2+ parts are loaded").

### 2.2 Token cost

Estimates at 3.8 characters per token for JSON schemas. The caps and today's measured
values are the generated table in architecture.md section 6 (`tests/sidecar/test_budget.py`
measures, `scripts/generate_budget_table.py` writes the table); they are not repeated here.
The budgets catch bloat, never a rule an agent needs (section 1.7): a failed call and its
retry cost far more than the schema characters that prevent it. Each cap is a measured value
plus headroom and moves only when what it measures changes in kind, never because a tool or
a domain was added.

| Item | Before the catalogue (measured then) | Design (caps and measured values: architecture.md 6) |
|---|---|---|
| Full listing (name + description + input schema) | 218,951 characters, about 55-60K tokens for 227 tools (median 851, max 6,077 characters per tool) | A mean per listed tool, call keys (`operation_id`, `intent`) included, so adding tools does not move the cap; per-tool caps bound each definition. Opt-in for clients that load schemas on demand |
| `agent` profile listing | 13 tools, 11,588 characters, about 3K tokens | 18 tools (4 gateways + 14 core), about 4K tokens, plus the server instructions once per session; cap a base plus a little per domain |
| One discovery call (10 rows) | full descriptions, 1-3K tokens | name + summary + effect rows; each row, the rest of the page and the 90th percentile page capped |
| Schema fetch | one tool per call, 250-1,500 tokens each | 1-10 tools per call; each entry a little over the tool's definition; variants of one tool share their schema once |
| Typical modeling task (overview, 3 searches, 4 schemas) | about 8-10K tokens of tool metadata | a few K tokens, capped, plus the session's listing and instructions |

---

## 3. Tool catalogue

One table per domain, as designed; the gateway tools of 2.1 are listed under session.
Tools added after the design (3.13) are described only in the [tool reference](../tools.md).

### 3.1 Session and kernel (`session`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_get_status` | Bridge, NX build, work/display part, top-level part count and the agent's loaded-part count (`parts.agent_loaded`: from two on, calls without refs take `part`, 1.10), application, panel mode (`auto` or `ask`, with the panel's `mode_label` Auto or Ask me first; only the engineer changes it), the agent calls queued for NX (`control.queue`: `waiting`, `oldest_seconds`), workspace root and path rules | `include?: bridge/parts/control/workspace` | read |
| `nx_find_tools` | Search the catalogue (2.1) | `query \| queries, domain?, effect?, Page` | read |
| `nx_get_tool_schemas` | Schemas, examples, evidence, sub-schemas such as sheet-metal operations, per-variant `required_by` fields; several variants of one tool come as one entry (shared schema once, each variant's own fields) | `names[1..10], include_output` | read |
| `nx_invoke` | Gateway call | `tool, arguments, intent?, detail` | per tool |
| `nx_invoke_read` | Gateway call for read tools only | `tool, arguments, detail` | read |
| `nx_get_operation` | Durable state of an operation: running, committed, failed, unknown; echoes the request's `arguments` (redacted: file contents and long strings become size notes) | `operation_id` | read |
| `nx_cancel_operation` | Cooperative cancel between batch steps | `operation_id` | session |
| `nx_get_result` | Page or expand a stored result (one store for bridge and sidecar snapshots) | `result_id, field?: JSON pointer, Page, detail` | read |
| `nx_delete_results` | Preview or prune stored result snapshots (age and size limits: the caller's own, or every agent's with `all_agents`) | `dry_run=true, max_age_seconds?, max_bytes?, result_ids?, all_agents=false` | file |
| `nx_run_batch` | 1-100 batchable calls under one undo mark; `"$2.data.feature"` refers to an earlier step's result | `operations[1..100]: {tool, arguments}` | model |
| `nx_preview_changes` | Apply 1-25 previewable calls, report before/after bounds, volume, health and an optional image, then roll back | `operations[1..25], capture=false` | session |
| `nx_finish_preview` | Re-apply a preview in its own part (the preview names it) if no call changed that part since, or discard it; a refused accept keeps the preview, a finished or closed-part one says what happened | `preview_id, action: accept/discard` | model |
| `nx_undo_operation` | Undo the target part's last agent operations (never the engineer's, never other parts' work); refs of objects that remain stay valid. The history ends at a part load by a file or session call (undoing across it would unload that part), a retained close, the engineer's edit, a save or close, or a mark NX evicted: the refusal names it (`details.reason`); a part an undone call loaded is unloaded again (`NX_NOTE`). When other parts' operations came after the undone ones, NX's undo would revert them: the undo compensates instead, deleting what create-only feature calls made and setting back the values `nx_set_attributes`, `nx_set_expression` and `nx_set_name` changed (`data.compensated`, `restored`, `missing`, `kept_expressions`, `renumbered`; a value changed since is `NX_DEPENDENCY_ORDER`), else `NX_CROSS_PART` names them (`details.in_the_way`) with a fix-forward hint. `operation` undoes one own operation (its part from the history; `NX_DEPENDENCY_ORDER` while later operations of the part follow it); `mode: "compensate"` (`"delete_created"` is the same) always compensates (across loads, closes and saves too, never across the engineer's edits); a refusal at such a cut says whether the operation qualifies. `data.redo` lists the undone calls as `{tool, arguments, complete}` (no `operation_id`; `complete: false` = the receipt cut long text or file content) and `redo_depth` how many `nx_redo_operation` can repeat in that part: repeat them with it or with `nx_invoke` | `steps=1, operation?, mode?` | model |
| `nx_redo_operation` | Repeat the target part's calls an undo or a checkpoint restore reverted, next first (the one part with calls to redo when no `part` is given), each as its own operation under its original `operation_id` with its receipt arguments (new refs, new receipt linked to the reverted run under `previous`; `data.results` gives each call's `data` and `changes`); new committed work in the part or an engineer edit ends its redo list, a part close drops that part's entries; a call whose receipt cut its arguments is refused (`NX_UNSUPPORTED_OPTION`: repeat it with `nx_invoke`); a failing step undoes the steps redone | `steps=1` | session |
| `nx_create_checkpoint` | In-session undo checkpoint (expires on save, part close, a later part load, an undo past it or an engineer edit) | `label?` | session |
| `nx_list_checkpoints` | Checkpoints (with `part`: that part's) and the target part's undo depth (no `part`: the work part's); an expired one stays listed with `expired_reason`: `saved_or_closed`, `undone_past`, `part_loaded` (a part was loaded after it: restoring would unload it), `display_reset` (a display switch outside NX MCP dropped NX's marks), `mark_evicted` (NX keeps about 180 undo marks for the session), `engineer_change` or `part_closed` | — | read |
| `nx_restore_checkpoint` | Roll the checkpoint's part back to it (id or label; the checkpoint names its part), undoing that part's agent operations since by the rules of `nx_undo_operation` (other parts' later work: undo by deletion or `NX_CROSS_PART`; nothing of the part since: nothing undone); it stays restorable, refs of objects that remain stay valid; `nx_redo_operation` repeats the reverted calls | `checkpoint_id` | model |
| `nx_activate_application` | Switch the NX application for the displayed part; an unlicensed application is refused (`NX_LICENSE_UNAVAILABLE`) before NX would open its blocking license dialog. NX enters an application for the displayed part only: a target part that is not displayed (or no displayed part) is refused `NX_WRONG_PART_CONTEXT` before NX, with a hint to show it with `nx_activate_part` (sheet-metal tools called with `part` enter Sheet Metal themselves) | `application: modeling/sheet_metal` | session |
| `nx_delete_objects` | Delete features, components, explosions, annotations (incl. dimensions, tables, balloons, traces), section views, sheets, views | `objects: Ref[1..100]` | model |
| `nx_set_name` | Rename any object; returns the NX-normalised name | `object, name` | model |

### 3.2 Parts and files (`parts_files`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_list_files` | Workspace directory listing with size and SHA-256 | `path="", prefix?, Page` | read |
| `nx_create_directory` | Create a workspace directory (idempotent) | `path` | file |
| `nx_upload_file` | Upload a new workspace file: UTF-8 text in one call, or one base64 chunk | `path, content?` or `data_base64, offset, total_size, sha256` | file |
| `nx_download_file` | Metadata, inline PNG, or base64 chunk of a workspace file | `path, delivery: metadata/image/chunk, offset?, length?` | read |
| `nx_create_part` | New part file, loaded; calls reach it by `part` without showing it. Shown as work and display part only while it is the agent's only part and no engineer part is displayed (1.10); never takes the engineer's active tab (`display: true` then gives it a tab behind it) | `path, units: mm/in, display?` | file |
| `nx_open_part` | Open or reuse a part; optionally load all components. Calls reach it by `part` without showing it; never takes the engineer's active tab (1.10). Reusing the engineer's or another agent's loaded part changes nothing (`NX_NOTE`). A part NX marks modified while loading it is reported (`modified_on_load`, `NX_NOTE`), never refused | `path, work=true, display?, load_components=false` | session |
| `nx_activate_part` | Show a loaded part to the engineer in its own tab. Not needed to work in it: calls target their part by refs or the `part` key, never by activation. While the engineer's part is displayed the tab stays behind it and an `NX_NOTE` says so (1.10). Never the engineer's part (`NX_ENGINEER_PART`); another agent's only with `force` (`NX_PART_BUSY`) | `part` (ref, workspace path or file name), `work, display, force=false` | session |
| `nx_list_parts` | Loaded parts with modified state, paged | `path_prefix?, modified?, active_only=false, Page` | read |
| `nx_save_part` | Save a part in place, or save as a new workspace path. Drawing views NX flags out of date are updated first (so the part never reopens with "(Out of Date)" sheets), listed in `updated_views` with an `NX_NOTE`. A save never warns about other agents (it loses no one's work and ends the part's busy state); the engineer's part only when they granted it (`NX_ENGINEER_PART`) | `part?` (ref, workspace path or file name), `path?` (path = save as, never overwrites) | file |
| `nx_close_part` | Close one loaded part, never implicitly the work part (refused while a loaded assembly uses it). Never the engineer's part (`NX_ENGINEER_PART`, no override); another agent's part, or on a shared connection a discard of unsaved changes a call made and worked in within 30 s, is refused (`NX_PART_BUSY`, 1.10) unless `force`; the agent's own parts close without it. NX keeps its undo marks (`data.undo_marks: kept`; a displayed part is first replaced on the display by another agent part, `data.displayed`); undo stops at the close. The part's analysis display objects are deleted first (NX would otherwise demand-load the closed file behind a modal dialog at the next analysis display). Closing an assembly also closes the component parts it leaves unused that the agent created or opened; `kept_components` names the ones kept, with the reason (not the agent's, used by another loaded assembly, work or display part before the call, unsaved changes, in use: a call worked in it after the assembly was loaded) | `part` (ref, workspace path or file name), `save?` (required when the part has unsaved changes), `unload_components=true`, `force=false` | session |
| `nx_export_assembly` | Copy the saved assembly with all loaded prototypes as a ZIP package or as a renamed clone folder | `path, format: zip/clone, prefix? (clone), activate=false` | file |
| `nx_export_model` | Export the work part or bodies to a neutral format; the file is read back (schema, units, solids). STEP always sets its schema explicitly (AP214 for `step`, AP242 for `step_ap242`, which needs its own license); DXF/DWG carry 3D model data (drawing sheets: `nx_export_drawing`) | `path, format?: step/step_ap242/stl/iges/parasolid/jt/dxf/dwg (default from the suffix, never step_ap242), bodies?, options?` (STL only) | file |
| `nx_import_model` | Import a CAD file into the work part, a new part or a sketch | `path, format?: step/iges/parasolid/stl/dxf_dwg/catia_v5 (default from the suffix, never catia_v5), target: work_part/new_part/sketch, new_part_path?, sketch?, flatten=true, options?` | model |
| `nx_export_profile` | Export a sketch or planar face as 1:1 LINE/ARC/CIRCLE DXF | `source, path, frame?: {origin, x_axis, y_axis}, layer="0", layers?` | file |

### 3.3 Feature tree and expressions (`modeling`)

Features are listed with `nx_list_objects(kind=feature)` (3.9).

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_get_feature` | Type, timestamp, state, parents/children, parameters with expressions, units and editability; a blend or chamfer adds the edges it acts on (`edges`, `edge_count`) | `feature` | read |
| `nx_edit_feature` | Set feature parameters by expression name or semantic alias (`distance`, `count`, `spacing`; a tube's `outer_diameter`, `inner_diameter`); a value may be a number, a formula or another expression name (binding); `definition` replaces non-expression definitions (transform, spline points, sheet-metal parameters) | `feature, parameters?: {name: number/formula}[1..25], definition?` | model |
| `nx_set_feature_suppression` | Suppress or unsuppress features (dependents follow) | `features[1..100], suppressed` | model |
| `nx_set_current_feature` | Roll the timeline to a feature, or to the end when omitted | `feature?` | model |
| `nx_set_feature_order` | Move a feature before or after another | `feature, anchor, position: before/after` | model |
| `nx_set_expression` | Create a named expression or change one's formula; lists the features it drives (`features`) and solves an active sketch whose dimensions it drives | `name, formula, units?: mm/in/deg/rad/unitless (new only)` | model |
| `nx_update_model` | Run the NX model update and return health | — | model |

### 3.4 Datums (`modeling`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_create_datum_plane` | Datum plane: offset from a plane, point+normal, or three points | `method: offset/point_normal/three_points, reference?, distance?, origin?, normal?, points?: Point3[3], name?` | model |
| `nx_create_datum_axis` | Datum axis through a point along a direction, or through two points | `method: point_direction/two_points, origin?, direction?, start?, end?, name?` | model |
| `nx_create_datum_csys` | Datum coordinate system | `origin, x_axis, y_axis, name?` | model |
| `nx_create_datum_point` | Point feature | `position: Point3, name?` | model |

### 3.5 Sketch (`sketch`)

Sketches are listed with `nx_list_objects(kind=sketch)`. All sketch coordinates are
`Point2` in the returned sketch-local frame.

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_create_sketch` | Create a sketch, left open unless `finish` (`active`, `parked`: deactivated in NX between calls, so other parts' calls run; the sketch tools reopen it by ref per call; its part's model tools wait for `nx_finish_sketch`) on a principal plane, a face or datum plane, or normal to an edge path. On a face or datum plane the frame is predictable: origin = the part origin dropped onto the plane, x = `x_direction` (in the plane) or the absolute X projected onto it (Y on a plane normal to X) | `placement: {principal: xy/xz/yz, origin?, x_axis?}` or `{on: Ref, x_direction?}` or `{path: edges[], help_point, percent, orienting_face?, reverse_normal, reverse_axis}`, `name?`, `curves?` (items of `nx_create_sketch_curves`: a profile in the same call), `finish=false` | model |
| `nx_finish_sketch` | Finish the sketch (the ref, or the target part's open sketch, parked or active) and update the model | `sketch?` | model |
| `nx_get_sketch` | Frame; curves (local and part coordinates, with their constraint count and `dof`, 0 = fully constrained) and loops; constraints with `geometry` (`{curve, point}`, point `start`/`end`/`center`/`mid`/`curve`, per curve used); `status: {state, fully_constrained, dof, free_curves}` (`dof` null when not computable) | `sketch, include?: geometry/constraints/status` | read |
| `nx_check_sketch` | Find the constraints that cause an over- or inconsistent constraint state | `sketch, max_checks=20` | read |
| `nx_create_sketch_curves` | Add 1-100 curves in one call | `sketch, curves[1..100]` of `line{start,end}`, `arc{center,radius,start_angle,end_angle}`, `circle{center,radius}`, `ellipse{center,major_radius,minor_radius,rotation,start_angle,end_angle}`, `rectangle{corner1,corner2}`, `polygon{center,sides,radius,inscribed,rotation}`, `slot{center,width,height}`, `rounded_rectangle{center,width,height,radius}`; `construction=false` | model |
| `nx_edit_sketch_curves` | 1-100 edits: reshape, trim/extend, construction toggle, delete | `sketch, edits[1..100]` of `line{curve,start,end}`, `arc{curve,center,radius,start_angle,end_angle}`, `trim{curve,boundaries,pick}`, `extend{curve,boundaries,pick}`, `construction{curves,on}`, `delete{object}` | model |
| `nx_create_sketch_corner` | Fillet or chamfer the corner between two curves (both trimmed) | `sketch, first, second, kind: fillet/chamfer, radius?, distance?, distance2?` | model |
| `nx_create_sketch_copy` | Offset or mirror curves (associative) | `sketch, curves, mode: offset/mirror, distance?, side?: auto/left/right, mirror_line?` | model |
| `nx_create_sketch_projection` | Bring model geometry into the sketch: project edges/curves or intersect faces with the plane | `sketch, objects, mode: project/intersect` | model |
| `nx_create_sketch_constraints` | 1-50 geometric constraints | `sketch, constraints[1..50]: {type: fixed/horizontal/vertical/parallel/perpendicular/equal_length/equal_radius/concentric/tangent/coincident/midpoint/point_on_curve/symmetric, curves?: Ref[], points?: SketchPoint[], centerline?}` | model |
| `nx_create_sketch_dimensions` | 1-50 driving or reference dimensions; each returns its expression | `sketch, dimensions[1..50]: {kind: length/horizontal/vertical/distance/radius/diameter/angle/parallel_distance/perpendicular_distance, first: Ref/SketchPoint, second?, value, origin: Point2, reference=false}` | model |

Note: dimensional constraints (distance, length, radius, diameter, angle) are
`nx_create_sketch_dimensions`.

### 3.6 Solid modeling (`modeling`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_create_extrude` | Extrude a sketch | `sketch, end: Limit, start?: Limit (distance 0), symmetric=false, direction?: Direction, combine?: Combine` | model |
| `nx_create_revolve` | Revolve a sketch about an axis | `sketch, axis: Axis, start_angle=0, end_angle=360, combine?` | model |
| `nx_create_sweep` | Sweep a closed section sketch along a tangent-continuous (G1) guide sketch; a body whose faces cut each other (self-intersecting) fails `NX_BUILD_FAILED` and is rolled back, as for `nx_create_tube`, `nx_create_sweep_along_guide` and `nx_create_variational_sweep` | `section, guide, combine?` | model |
| `nx_create_loft` | Through-curves loft through ordered sketches, or swept along guides | `sections: sketch[2..20], solid=true, start_points?: Point3[2..20], guides?: Ref[1..3], tangent?: faces/normal, combine?` | model |
| `nx_create_hole` | NX Hole feature: simple, counterbore, countersink, threaded; 1-100 positions on a planar face | `face, positions: Point3[1..100], form, diameter, depth: Limit, tip_angle=118, counterbore?{diameter,depth}, countersink?{diameter,angle}, thread?{standard,size,depth}, target?` | model |
| `nx_create_thread` | Thread on a cylindrical face (or one per face of `faces`), from a catalogue row or explicit geometry. An internal thread on a bore at or above the major diameter (a clearance hole) is refused `NX_WRONG_GEOMETRY` with the tap drill and the hole feature to edit (`details.tap_drill`, `seed_feature`); an external one on a shaft at or below the minor diameter too | `face \| faces[1..50], start_face? (default the face's only planar end), length?: Length/"full" (default: the whole face, following it), spec: {standard, size, method?, engagement?}` or `{pitch, major_diameter, minor_diameter, angle=60, starts=1}`, `detailed=false, hand: right/left, reverse=false` | model |
| `nx_list_thread_sizes` | Installed thread standards, sizes and size details | `standard?, size?, Page` | read |
| `nx_create_blend` | Constant or variable radius edge blend. Edges that leave out part of a tangent-continuous chain get an `NX_NOTE` (the blend ends in a step); `chain=true` adds the chain's edges | `edges[1..200], radius?` or `variable?: [{at: 0..1, radius}][2..20]` (one edge, no chain), `chain=false` | model |
| `nx_create_chamfer` | Chamfer edges; tangent chains as for `nx_create_blend` | `edges[1..200], method?: symmetric/two_offsets/offset_angle (omitted: from offset2 or angle), offset, offset2?, angle?, reverse=false, chain=false` | model |
| `nx_create_shell` | Hollow a solid; `alternate` gives listed faces their own wall thickness | `body, thickness, remove_faces?, outward=false, alternate?: [{faces, thickness}][1..20]` | model |
| `nx_create_draft` | Draft faces about a stationary face | `faces, stationary_face, direction: Direction, angle` | model |
| `nx_create_boolean` | Unite, subtract or intersect bodies | `operation: unite/subtract/intersect, target, tools: body[1..50]` | model |
| `nx_create_mirror` | Mirror bodies, or features into their own bodies, about a plane | `plane: Plane, target: bodies/features, bodies?: body[1..50], features?: feature[1..50], keep_original=true (bodies), unite=false (features)` | model |
| `nx_create_pattern` | Feature pattern: linear, circular, along a curve, polygon, spiral, fill or explicit points; counts include the seed, `general` positions list only the copies | `features[1..50], layout: linear/circular/along_curve/polygon/spiral/fill/general, parameters` (per-layout sub-schema: `nx_create_pattern#circular`), `unite=false` | model |
| `nx_create_trim_body` | Cut a body with a plane: keep one side (Trim Body) or both (Split Body) | `body, tool: Ref (datum plane or planar face), keep: back/front/both` | model |
| `nx_create_scale` | Uniform or per-axis scale feature | `bodies, factor` or `factors: [x, y, z]` (along world axes or a datum CSYS's), `origin?: Point3/"center"`, `csys?` | model |
| `nx_create_transform` | Move or copy bodies by a rigid transform (edit with `nx_edit_feature` `definition`) | `bodies, transform: Pose, copy=false` | model |
| `nx_edit_faces` | Synchronous face edit for any solid, including imported: move, offset, replace or delete faces; resize a hole, boss, cone, blend or chamfer; pattern or mirror faces; delete blend chains | `faces, action: move/offset/replace/delete/resize/resize_blend/resize_chamfer/pattern/mirror/delete_blend, distance?, direction?, replacement?, diameter?, angle?, radius?, pattern?, plane?` | model |
| `nx_get_material` | Material names and densities, including occurrence prototypes | `bodies?, scope: part/assembly` | read |
| `nx_set_material` | Assign a library or custom material to solid bodies (default all); a custom name that exists gets the new density (every body using it) | `bodies?, material: "Steel" \| {library} \| {custom: {name, density}}` | model |

Note: a hole is a hole feature; the agent first finds the placement face with
`nx_find_geometry`.

### 3.7 Freeform (`freeform`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_create_spline` | 3D Studio Spline through points or by poles (edit with `nx_edit_feature` `definition`) | `points: Point3[2..1000], method: through_points/poles, degree=3, periodic=false` | model |
| `nx_create_bridge_surface` | Bridge two sheet edges with G0/G1/G2 continuity | `first, second, continuity: g0/g1/g2, reverse_second=false` | model |
| `nx_create_mesh_surface` | Through Curve Mesh sheet | `primary: Ref[][2..100], cross: Ref[][2..100], tolerance?` | model |
| `nx_create_sew` | Sew sheets, optionally requiring a closed solid | `target, tools, tolerance?, solid=false` | model |
| `nx_create_thicken` | Thicken sheet faces into a solid | `faces, thickness?` or `offset1?, offset2?` (signed along the face normal) | model |
| `nx_create_trim_sheet` | Trim a sheet with curve boundaries | `body, boundaries, region_point: Point3, keep=true, direction?` (projection vector) | model |

### 3.8 Assembly (`assembly`)

Assembly constraints, component patterns, reference sets and explosions are
listed with `nx_list_objects` (3.9).

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_list_components` | Recursive occurrence tree with load state, paged | `name_contains?, suppressed?, include_poses=false, Page` | read |
| `nx_create_component` | Add a saved part, or a new empty part, as a component | `mode: from_file{path}/new_in_context{new_path, units?, objects_to_move?}, name?, pose?: Pose, reference_set?` | model |
| `nx_edit_component` | Rename, replace part, make unique, suppress or set reference set of direct children | `components[1..100], name?, replace_with?: path, make_unique?: new path, suppressed?, reference_set?` | model |
| `nx_set_component_pose` | Absolute pose or relative move of a direct child | `component, mode: absolute/relative, translation: Vec3, rotation?: Mat3` | model |
| `nx_create_assembly_constraint` | Persistent constraint; the solver must report solved. Concentric needs circular edges: a cylindrical or conical face given instead is replaced by its circular edge nearest the other geometry (a tie is refused `NX_AMBIGUOUS_REFERENCE`); `moved` lists the components the solver moved | `type: fix/touch/align/distance/parallel/perpendicular/angle/concentric/align_lock/fit/bond/center_1_2/center_2_2, component, geometry?, target_component?, target_geometry?, geometry2?, target_geometry2?, value?, alignment: infer/same/opposite` | model |
| `nx_edit_assembly_constraint` | Change value, alignment or suppression | `constraint, value?, alignment?, suppressed?` | model |
| `nx_create_component_pattern` | Pattern a component; associative NX pattern or independent copies | `component, layout: {linear: {direction, count, spacing, count2?, direction2?, spacing2?}}` or `{circular: {axis, count, angle, span?}}` (`angle` is the step, or with `span` first to last; 360 = full circle), `associative=true` | model |
| `nx_edit_component_pattern` | Change count, spacing or angle | `pattern, count?, spacing?, count2?, spacing2?, angle?` | model |
| `nx_create_reference_set` | Custom reference set from bodies, curves, datums, sketches, components; with `component`, in that component's part | `name, objects[1..1000], component?` | model |
| `nx_create_explosion` | Named explosion at assembled positions; `placements` move components at once. Shown in the modeling work view (and fitted) unless `display=false`; `views` says where it is shown (while a drawing sheet is open nothing is shown, with an `NX_NOTE`) | `name, placements?` (as in `nx_edit_explosion`), `display=true` | model |
| `nx_edit_explosion` | Set exploded poses or reset components; assembled poses never change; shown in the work view as for `nx_create_explosion`; `updated_views` lists the drawing views showing it that followed | `explosion, placements?: [{component, offset?: Vec3 (from the inherited pose) \| pose?: Pose}][..1000], reset_components?, display=true` | model |
| `nx_get_explosion` | Exploded and assembled poses, paged; `explosion` defaults to the part's only explosion, else the one its work view shows (several, none shown: `NX_INVALID_ARGUMENT` listing them) | `explosion?, Page` | read |
| `nx_set_explosion_display` | Show an explosion (or assembled state) in the model view, a drawing view or a saved model view | `explosion?: Ref (null = assembled), drawing_view?, model_view?` | model |
| `nx_create_explosion_trace` | Trace line between two component edges | `explosion, start_edge, end_edge, start_percent=0, end_percent=0, start_direction?, end_direction?` | model |
| `nx_edit_explosion_trace` | Change trace anchors and offsets | `trace, start_percent?, end_percent?, start_offset?, end_offset?` | model |
| `nx_export_explosion_animation` | Self-contained HTML animation of an explosion | `explosion, path, frames=12, fps=12, width, height` | file |
| `nx_update_assembly` | Propagate prototype changes: solve constraints, refresh traces, BOMs, measured annotations, views | — | model |

Note: components are removed with `nx_delete_objects`.

### 3.9 Inspection (`inspection`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_list_objects` | One inventory for work-part objects: filter, then page. Rows: reference, name, type, state and a kind-specific summary. A component owner (of the work assembly) lists the bodies, faces and edges of its occurrence bodies, or its part's features (with an `NX_NOTE`: they live in that part) | `kind: body/face/edge/datum/feature/expression/sketch/drawing_sheet/drawing_view/dimension/annotation/drawing_table/reference_set/explosion/component_pattern/assembly_constraint/section_view, owner?: Ref (body, face, feature, component), name_contains?, type?, include?: adjacency/views, Page` | read |
| `nx_get_model_summary` | Counts, bounds, health and a short tree of features, expressions, sketches or components | `section: overview/components/features/expressions/sketches, Page` | read |
| `nx_check_model` | Feature errors and warnings, suppression, unloaded prototypes, body consistency, unsolved assembly constraints. Issue `kind`s: `feature_diagnostic`, `suppressed_feature`, `unloaded_component`, `suppressed_component`, `assembly_constraint` (solver `status` and `components`; CannotSolve (e.g. after a prototype edit consumed its edge), OverConstrained, Inconsistent or Conflicting is an error, any other unsolved status a warning), `body_consistency`, `internal_void`, `self_intersection` (an error naming up to 10 faces that cut other faces of the body; the first 50 solids are examined, an `info` row counts the rest). No model update is run | `scope: part/assembly, Page` | read |
| `nx_find_geometry` | Faces, edges or bodies by type, normal, radius, role, nearest point or extreme; returns refs and a reusable selector. `kind=body` takes no type, normal, radius or role filter and returns each body's `min`/`max` box. A drawing-view owner is refused with a hint to `nx_list_view_edges`. `parallel` keeps line edges along an axis (either sense) or parallel to a principal plane (`"z"`: a block's vertical edges; `"xy"`: across z), within `tolerance`; `axis` only orders | `owner?: body/feature/component, kind: face/edge/body, geometry_type: any/plane/cylinder/cone/circle/line, normal?, radius?, role?: bore/boss, near?: Point3, order: nearest/highest/lowest, axis: x/y/z, parallel?: x/y/z/xy/yz/xz, tolerance?, Page` | read |
| `nx_find_holes` | Inward cylindrical faces grouped coaxially, with radius and axis | `owner?, diameter?, through?, Page` | read |
| `nx_create_anchor` | Persistent handle for a body, face or edge that survives edits and save/reopen | `object` | read |
| `nx_resolve_reference` | Current reference from a selector or anchor; unique match or explicit error. It acts in the part the anchor or selector names (its ref, anchor, `part` path or `owner`; an occurrence: its assembly), so two loaded parts need no `part` | `selector?` or `anchor?`, `tie_tolerance?` | read |
| `nx_measure_distance` | Minimum distance and closest points between two objects | `first, second` | read |
| `nx_measure_angle` | Angle between lines, straight edges or planar faces | `first, second, units: deg/rad` | read |
| `nx_measure_mass` | Volume, area, mass, centre of gravity, inertia | `bodies?, scope: auto/part/assembly` | read |
| `nx_measure_bounds` | Bounding box, conservative or exact | `bodies?, scope: auto/part/assembly, precision: conservative/exact` | read |
| `nx_measure_wall_thickness` | Sampled normal wall thickness | `body, faces?, samples_per_axis=5, tolerance?` | read |
| `nx_analyze_faces` | Sampled normals, curvature and optional draft angles, grouped per face (`faces: [{face, samples}]`) | `faces, samples_per_axis=5, pull_direction?, minimum_draft?` | read |
| `nx_analyze_curve` | Sampled positions, tangents, curvature, knots | `curve, samples=20` | read |
| `nx_check_continuity` | G0/G1/G2 pass/fail between two edges or curves: `surface` samples two sheet boundary edges along their length, `curve` compares them at their nearest ends (`junction`) | `first, second: edge/curve, mode: auto/surface/curve, tolerances?: {position, angle, curvature}, samples=20` | read |
| `nx_check_interference` | Interference and clearance between two objects or across groups or the whole assembly; highlight the result with `nx_set_highlight`. `counts` classifies natively checked pairs only: pairs the bounding-box test skipped are `counts.skipped` with `skipped_reason`, never `clear` | `objects?: Ref[2..500] (omit = whole assembly), clearance=0, max_pairs=500, include_clear=false, include_contact=true` | read |
| `nx_export_inspection_report` | ZIP with clearance results, captures and section images | `path, objects?, clearance, max_pairs, section_planes?: Plane[..6], capture=true` | file |

While an explosion is shown in the work view, the `nx_measure_*` tools, `nx_find_geometry`,
`nx_find_holes` and `nx_check_interference` add an `NX_NOTE`: NX answers at the assembled
(unexploded) poses, not the exploded ones on screen.

### 3.10 Drawing (`drawing`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_create_drawing_sheet` | New sheet, from a template or a standard size | `name?, size: a0/a1/a2/a3/a4 or {width, height}, scale=1, units: mm/in, projection: first_angle/third_angle, template?, template_path?` | model |
| `nx_activate_drawing_sheet` | Open a sheet, or return to modeling with `sheet=null`; an `NX_NOTE` names the views NX flags out of date (`nx_update_annotations`, `nx_export_drawing` and `nx_save_part` update them), another says so if NX flagged the part modified (measured on NX 2406: opening or leaving a sheet does not) | `sheet?` | session |
| `nx_create_drawing_view` | Place a view at a verified sheet point; `type` selects the variant. A projected view takes `orientation` (the view to show: the side follows the parent's axes and the sheet's projection angle; an orientation that does not project from the parent, or a contradicting `direction`, is refused with `details.projects`) or `direction` (the side of the parent on the sheet). Derived views keep the parent scale; broken and break_out cut the parent in place | `type: base{sheet?, orientation, body?, explosion?, position, scale?}`, `projected{parent, orientation? \| direction?: right/left/top/bottom, spacing?}`, `section{parent, cut: edge, cut_point: start/end/center, arrow_direction, step_direction?, position}` (`half_section` + `bend`, `stepped_section` + `cuts`, `revolved_section` + `rotation`), `detail{parent, center: Point3, radius, position, scale?}`, `auxiliary{parent, hinge, position}`, `broken{parent, breaks, axis, gap?}`, `break_out{parent, center, radius, depth}`, `flat_pattern{sheet, flat_pattern, position}` | model |
| `nx_edit_drawing_view` | Position, scale and display style; projected views made from it follow a scale change and keep their gap (`rescaled`) | `view, position?, scale?, style?: {hidden_lines, smooth_edges, shading, ...}` | model |
| `nx_get_drawing_view` | Type, orientation, parent, actual scale, sheet `position` (the reference point) and `center` (the border centre), border `bounds`, containment, overlapping views, style | `view` | read |
| `nx_create_drawing_dimension` | Associative dimension in a view. A linear dimension on the body's outline that stops short of the body's extent along it (covering at least half of it) gets an `NX_NOTE` naming the outer edges for the overall size | `view, kind: linear/radius/diameter/angle/ordinate/chamfer/thickness/arc_length/baseline/chain/hole_callout, first, second?, orientation?: aligned/horizontal/vertical, origin?: SheetPoint`; ordinate: `datum, datum_point, objects[1..50], direction, margin?`; baseline/chain: `datum?, objects, direction`, 3 or more points (NX 2406 does not draw a two-point set: use `linear`) | model |
| `nx_get_dimension` | Measured value, validity, format and tolerance | `dimension` | read |
| `nx_edit_dimension` | Format, tolerance (ISO 286 fits included), or rebind a linear dimension to 1-2 edges | `dimension, format?: {decimals, trailing_zeros, units, decimal_separator}, tolerance?: {type, upper, lower, decimals, ...}, edges?: edge[1..2]` | model |
| `nx_create_note` | Note with optional leader | `text: string[1..50], origin: SheetPoint, sheet?, leader_to?, view?` | model |
| `nx_create_centerline` | Center marks on circular edges, axis of a cylindrical or conical face, centerline between two edges, or a bolt circle through 3+ hole edges | `view, kind: center_mark/axis/between_edges/bolt_circle, objects[1..50]` | model |
| `nx_create_drawing_symbol` | Surface finish or weld symbol with leader | `view, object, origin?, symbol: surface_finish{roughness_ra, removal: any/required/prohibited}/weld{weld: fillet/square_butt/v_butt, size, side: arrow/other/both}` (flat fields) | model |
| `nx_create_gdt` | GD&T as PMI on faces, or drafting GD&T with a leader to view geometry | `kind: datum_feature/fcf/view_datum_feature/view_fcf/view_datum_target`; PMI: `faces, position: Point3`; view: `view, object: edge/face, origin?: SheetPoint`; `letter?` (datum or target), `frame?: {characteristic, tolerance, datums?[..3], material?: mmc/lmc/rfs, zone?, datum_material?, projected_height?, tangent_plane, free_state}`, `target?: {type: point/line/circular/rectangular/annular, index?, diameter?, inner_diameter?, width?, height?}` | model |
| `nx_edit_annotation` | Move or rename an annotation, dimension or table, or replace a note's text | `annotation, position?, name?, text?` | model |
| `nx_update_annotations` | Refresh managed measured annotations, BOMs, bend and hole tables, title blocks and detail boundaries from the model; also updates out-of-date drawing views | `objects?` (omit = all managed) | model |
| `nx_create_drawing_table` | Parts list, bend table, title block, revision or hole table | `kind: parts_list{sheet, position, scope: leaves/top_level/all}`, `bend{view, position, columns?, automatic}`, `title_block{sheet, rows, widths, position, row_height?}`, `revision{...same}`, `hole{view, origin: edge, origin_point?, position}` | model |
| `nx_edit_drawing_table` | Edit rows, widths, columns, title-block cells or update mode, or move the table; `kind` defaults to the table's | `table, kind?, rows?, widths?, row_height?, position?, columns?: [{action: edit/append/remove, index?, title?, width?, field?, key?, protected?}], bend_columns?, automatic?, cells?` | model |
| `nx_get_drawing_table` | Evaluated rows and columns | `table` | read |
| `nx_create_balloons` | Parts-list balloons in a view | `parts_list, view` | model |
| `nx_export_drawing` | Export sheets to PDF or CGM (all or the given sheets) or DXF/DWG (one sheet); out-of-date views are updated first and listed in `updated_views`; the part is not saved | `path, format?: pdf/cgm/dxf/dwg (default from the suffix), sheets?` | file |

Notes: a dimension is rebound with `nx_edit_dimension`, drawing and bend tables are
edited with `nx_edit_drawing_table`, and annotations are deleted with
`nx_delete_objects`. Notes, symbols, dimensions, view GD&T and `nx_edit_annotation` return
the annotation's sheet `bounds` and warn `NX_VIEW_OVERLAP` when it lies over a view border
or off the sheet. An omitted dimension origin lies outside the view border, stepped past
annotations already there; an omitted symbol or view GD&T origin goes up and right of the
leader point. The edges a view shows, with sheet coordinates, come from
`nx_list_view_edges`.

### 3.11 Sheet metal (`sheet_metal`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_get_sheet_metal_defaults` | Thickness, bend radius, neutral factor, reliefs, material, tool | — | read |
| `nx_set_sheet_metal_defaults` | Change part defaults | same fields, all optional | model |
| `nx_create_sheet_metal_feature` | Create one of 34 sheet-metal features; `parameters` follows the operation's sub-schema (`nx_get_tool_schemas(["nx_create_sheet_metal_feature#flange"])`) | `operation: tab/flange/contour_flange/.../flat_pattern, parameters` | model |
| `nx_get_sheet_metal` | Bodies, actual thickness and bend parameters | `body?, Page` | read |
| `nx_export_flat_pattern` | Flat pattern to DXF or Trumpf GEO | `flat_pattern, path, format: dxf/geo, dxf?: {revision, mold: inner/outer}, include?: bend_up/bend_down/bend_tangent/interior_cutout/..., tolerance?` | file |
| `nx_create_sheet_metal_callout` | Measured thickness or bend callout (refreshed by `nx_update_annotations`) | `kind: body/bend, body, faces?, position: Point3, automatic=true` | model |

Note: an existing sheet-metal feature is edited with
`nx_edit_feature(definition=...)`; the flat-pattern orientation edge list is
`nx_list_objects(kind=edge, owner=face, type=line)`.

### 3.12 Display (`display`)

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_capture_view` | PNG of the current view or sheet, saved to `path` or `captures/`; inline only with `inline=true` or without `path`; `quality=studio` renders with lighting. Omitted `width`/`height` take the graphics window's size; given ones are honoured: when the window differs, a shaded model view is rendered again at the exact size by Studio capture (`quality` then says `studio`), a wireframe view or a drawing sheet keeps the window size with an `NX_APPROXIMATED` warning. The camera, style and lighting are put back. `sheet` (ref or name) captures that drawing sheet: opened for the capture only, the previous sheet or model view comes back (no `nx_activate_drawing_sheet` first); a model-view capture of a part with sheets names them in an `NX_NOTE` | `quality: viewport/studio, path?, overwrite=false (replace an existing PNG at path), inline?, view_from?: Vec3, up?, fit?, hide_sketches=true, width?, height? (the window size when omitted), background?: white/original/transparent/color/black, color?, style: current/shaded/shaded_with_edges/wireframe/studio, lighting?, sheet?` | view |
| `nx_get_camera` | Displayed view, camera matrix, scale, style | — | read |
| `nx_set_camera` | Named orientation, a view direction, an explicit camera, zoom and/or fit | `orientation?: top/front/right/back/left/bottom/isometric/trimetric, view_from?: Vec3, up?, rotation?: Mat3, origin?, scale?, zoom?, fit=false` | view |
| `nx_set_visibility` | Show, hide or isolate objects, or all datums | `objects?` or `datums=true`, `mode: show/hide/isolate` | view |
| `nx_set_appearance` | Colour and transparency | `objects, color?: 1..216 or name, transparency?: 0..100` | view |
| `nx_get_appearance` | Colour, blank state, transparency (or counts only) | `objects, count_only=false` | read |
| `nx_set_highlight` | Replace agent highlights with these objects; empty list clears | `objects: Ref[0..1000]` | view |
| `nx_restore_display` | Restore a display snapshot (`restore_id`) or a saved presentation file | `restore_id?` or `presentation?: path` | view |
| `nx_save_presentation` | Save camera, section and appearance to a workspace JSON file | `path` | file |
| `nx_create_section_view` | Dynamic section plane in the model view | `plane: {origin, normal}, offset?, name?, cap=true` | view |
| `nx_edit_section_view` | Move, enable or disable a section | `section, plane?, offset?, enabled?, cap?` | view |

Note: section views are edited with `nx_edit_section_view` and deleted with
`nx_delete_objects`.

### 3.13 Count

The catalogue (`nx_mcp.core.catalogue`) has 261 tools; the 4 gateway tools of 2.1 are
served by the sidecar outside it.

| Domain | Tools |
|---|---|
| assembly | 36 |
| display | 17 |
| drawing | 28 |
| freeform | 33 |
| inspection | 24 |
| modeling | 59 |
| motion | 1 |
| optimization | 7 |
| parts_files | 21 |
| session | 16 |
| sheet_metal | 8 |
| sketch | 11 |
| **Total** | **261** |

Effects of the 261: 170 model, 56 read, 17 file, 9 session, 9 view.

Added after the design and listed only in the [tool reference](../tools.md), by domain:

- **parts_files:** `nx_get_attributes`, `nx_set_attributes`; part families
  `nx_create_part_family` (members written from the saved template) and
  `nx_get_part_family`; Knowledge Fusion `nx_get_kf_rules`, `nx_set_kf_rule` (a child
  instance of a KF class, or a typed formula rule) and `nx_update_kf_rules`.
- **modeling:** `nx_create_emboss_body`, `nx_create_helix`, `nx_create_primitive` (block,
  cylinder, cone or sphere solid; a block combines with one target body), `nx_create_rib`,
  `nx_create_tube` (a self-intersecting tube is refused and rolled back),
  `nx_create_face_blend` (rolling ball between two face sets),
  `nx_create_wrap_geometry` (convex envelope), `nx_create_text` (text outline curves),
  `nx_export_expressions`, `nx_import_expressions`, `nx_list_materials`; sweeps
  `nx_create_sweep_along_guide` (mitred corners of a polyline guide) and
  `nx_create_variational_sweep` (a section sketched on its path); `nx_create_emboss` (a
  sketch section as a pad or pocket on solid faces), `nx_create_offset_emboss` (a bead on a
  sheet), `nx_create_draft_body` (two-sided draft about a parting plane),
  `nx_create_blend_corner` (setback corner blend), `nx_create_measurement` (an associative
  measure feature whose expression drives others); body repair `nx_create_patch_body`,
  `nx_create_delete_body`, `nx_create_unsew`, `nx_create_untrim`; synchronous
  `nx_edit_face_relation` (make a face coplanar, coaxial, parallel, perpendicular,
  tangent, symmetric or offset to another), `nx_edit_face_region` (offset region, pull
  face), `nx_edit_edges` (move or offset edges), `nx_create_face_copy` (copy or cut a face
  set), `nx_create_face_paste`, `nx_create_face_label` (chamfer or group labels).
- **freeform:** surfaces `nx_create_offset_surface`, `nx_create_n_sided_surface`,
  `nx_create_ruled`, `nx_create_sheet_extension`, `nx_create_law_extension`,
  `nx_create_variable_offset`, `nx_create_swept_volume`, `nx_create_bounded_plane`,
  `nx_create_fill_hole`, `nx_create_midsurface`, `nx_create_refit_face`; curves
  `nx_create_section_curves` (plane, parallel stations, radial),
  `nx_create_intersection_curve`, `nx_create_projected_curve`,
  `nx_create_combined_projection`, `nx_create_offset_curve`, `nx_create_fit_curve`
  (least squares, with the measured deviation), `nx_create_bridge_curve`,
  `nx_create_joined_curve`, `nx_create_mirror_curve`, `nx_create_trim_curve` (trim or
  divide), `nx_create_isoparametric_curves`, `nx_create_isocline_curves`,
  `nx_create_curve_on_surface`, `nx_create_law_curve`, `nx_create_general_conic`; points
  `nx_import_points` (a workspace x y z file).
- **assembly:** `nx_create_component_mirror`, `nx_create_extract`, `nx_create_wave_link`,
  `nx_create_interpart_link` (work-part expressions linked to another part's),
  `nx_create_assembly_cut`; arrangements `nx_create_arrangement`, `nx_edit_arrangement`,
  `nx_set_arrangement_component` (pose and suppression in one arrangement),
  `nx_list_arrangements`; weight and centre of gravity `nx_set_component_mass`,
  `nx_create_component_group`, `nx_list_component_groups`; stored clearance sets
  `nx_create_clearance_set`, `nx_run_clearance_set`, `nx_list_clearance_sets`,
  `nx_delete_clearance_set`; `nx_create_simplified_assembly` (one simplified solid);
  deformable parts `nx_create_deformable_definition` and `nx_set_component_deformation`.
- **inspection:** `nx_measure_section`, `nx_measure_section_inertia` (area, centroid,
  second moments and section moduli of a plane cut), `nx_check_face_continuity` (G1/G2
  across shared edges), `nx_create_surface_analysis` (an NX display analysis to capture,
  made in the displayed part only; `slope` and `distance` maps also return values),
  `nx_measure_assembly_mass`, `nx_export_mass_report`, `nx_analyze_body_changes` (two
  versions of a solid compared face by face).
- **drawing:** `nx_list_view_edges` (the model edges a drawing view draws, with sheet
  coordinates), `nx_create_sheet_sketch` (2D curves on a sheet), `nx_create_hatch` (fills
  a closed sheet region), `nx_set_view_sectioning` (which bodies a section view cuts),
  `nx_create_sheet_symbol` (ID, library, target point, intersection or edge condition
  symbols), `nx_set_sheet_borders` (border, zones, margins), `nx_create_part_family_table`,
  `nx_set_view_settings` (edge and thread display of views), `nx_check_drawing_changes`
  (compare with an earlier revision on disk).
- **sheet_metal:** `nx_get_bend_list` and `nx_export_bend_list` (CSV).
- **display:** visual (render) materials `nx_list_visual_materials`,
  `nx_get_visual_material`, `nx_set_visual_material`; `nx_set_true_shading`; decals
  `nx_create_decal` and `nx_delete_decal`.
- **motion** (domain added after the design): `nx_run_motion_sweep`, an assembly
  range-of-motion and clearance sweep without the Motion solver.
- **optimization** (domain added after the design, topology optimization):
  `nx_create_topology_study`, `nx_create_topology_condition`,
  `nx_create_topology_environment` (g-load, scenery bodies, connections),
  `nx_run_topology_study`, `nx_analyze_topology_study` (strength without lightening),
  `nx_get_topology_study`, `nx_set_topology_contour` (plot a solved study's result on its
  body, or capture it with its legend).

Biggest merges: sketch curves and constraints (32 → 11), inventories into
`nx_list_objects` (15 → 1), drawing views (5 → 1), drawing dimensions (4 → 1),
component patterns (3 → 1), drawing tables (3 → 1), interference/clearance/
highlight (3 → 1), camera (3 → 1).

---

## 4. Reserved families for the next wave (built)

Each family extends an existing tool with a new enum value or variant where the
job is the same; new tools only where the job is new. A new tool or variant has
no evidence (and is refused and hidden, decision 11) until its acceptance
scenario passes on the supported build. Every family below is built; the table keeps
the plan, and the [tool reference](../tools.md) has the arguments as built where they
differ (noted in the rows).

| Family | Tool(s) | Arguments to add | Effect | Fits as |
|---|---|---|---|---|
| Attributes (done) | `nx_get_attributes`, `nx_set_attributes` | `objects` (part, body, component, face, feature), `names?`; set: `values: [{name, type: string/number/integer/date/boolean, value, category?}][1..100]`, `delete?: names[]` | read / model | new tools |
| Material library (done; no `library` filter) | `nx_list_materials`; `nx_set_material` | list: `library?, query?, Page`; set: `material: {library: name}` next to `{custom}`; `nx_get_material` adds E, Poisson ratio, thermal data | read / model | new list + variant |
| Export by format (done) | `nx_export_model` | `format: step/step_ap242/stl/iges/parasolid/jt`, `options` sub-schema per format (`#stl`: chord tolerance, angular tolerance, binary) | file | enum values |
| Import by format (done; CATIA V5 added) | `nx_import_model` | `format: step/iges/parasolid/stl/dxf_dwg/catia_v5` (STEP AP242 files import as `step`), `target: work_part/new_part/sketch` (DXF) | model | enum values |
| Drawing DXF/DWG (done) | `nx_export_drawing` | `format: pdf/dxf/dwg` (no `version`) | file | enum values |
| Expressions file (done) | `nx_export_expressions`, `nx_import_expressions` | `path`; import: `mode: merge/replace`, returns changed expressions and update health | file / model | new tools |
| Feature mirror (done; `target: features`) | `nx_create_mirror` | `features[1..50]` as an alternative to `bodies` | model | variant |
| Pattern layouts (done; per-layout `parameters`) | `nx_create_pattern` | `layout: along_curve{curve, count, spacing}`, `polygon{axis, sides, count_per_side}`, `spiral{axis, count, pitch, angle}`, `fill{boundary, spacing, layout_type}`, `general{positions: Point3[]}`; `Direction` from edge or datum already supported | model | variants |
| Hole callouts and tables (done) | `nx_create_drawing_dimension`, `nx_create_drawing_table` | `kind: hole_callout{hole_feature or edge}`; table `kind: hole{view, origin_point, position}` | model | variants |
| Drawing GD&T (done) | `nx_create_gdt` | `kind: view_datum_feature/view_fcf/view_datum_target` with `view, object, origin` (distinct variant names keep PMI and drafting evidence apart) | model | variant |
| Chamfer, thickness, arc-length dims, sets (done; `kind: baseline/chain`) | `nx_create_drawing_dimension` | `kind: chamfer/thickness/arc_length`, `set: baseline/chain{objects[]}` | model | variants |
| Fits (done) | `nx_edit_dimension` | `tolerance: {type: fit, hole?: "H7", shaft?: "g6", display: symbol/limits/both}` | model | variant |
| Title-block filling (done) | `nx_edit_drawing_table` | `cells: [{row, column, text or attribute}]` for `title_block` (also by label) | model | variant |
| More drawing views (done) | `nx_create_drawing_view` | `type: auxiliary{parent, hinge: edge}`, `broken{view, breaks[]}`, `half_section`, `stepped_section{segments[]}`, `revolved_section`, `breakout{view, boundary, depth}` | model | variants |
| Synchronous edits (done) | `nx_edit_faces` | `action: resize{diameter or radius}`, `resize_blend{radius}`, `resize_chamfer{offset}`, `pattern{layout}`, `mirror{plane}`, `delete_blend` | model | enum values |
| Rib, tube, helix, emboss (done) | `nx_create_rib`, `nx_create_tube`, `nx_create_helix`, `nx_create_emboss_body` | rib: `sketch, body, thickness, side`; tube: `path: Ref[], outer_diameter, inner_diameter=0, combine?`; helix: `axis: Axis, radius, pitch, turns, hand` (as built: `radius`); emboss as built: a body emboss `target, tool_body, clearance?, thickness?` instead of the sketch emboss | model | new tools |
| Assembly constraint types (done) | `nx_create_assembly_constraint` | `type: align_lock/fit/bond/center_1_2/center_2_2` (no `distance_to_edge`) | model | enum values |
| New component in context (done) | `nx_create_component` | `mode: from_file/new_in_context{new_path, units?, objects_to_move?}`; `nx_edit_component(make_unique: path)` | model | variants |
| WAVE links and extract (done) | `nx_create_wave_link`, `nx_create_extract` | link: `kind: body/face/curve/datum/sketch, sources: Ref[1..50] (occurrences, part objects or components), component?, target?: component whose part gets the link (from the displayed assembly; else the work part), associative=true, at_timestamp=false`; extract: `objects (bodies or faces), associative=true` | model | new tools |

---

## 5. Decisions (engineer, 2026-10-09)

Tool names and arguments may change freely; there are no compatibility aliases.

1. **One inventory tool:** `nx_list_objects(kind=...)` replaces the 15 list tools.
2. **No name lookup in mutations:** mutating tools accept only `Ref`; finding
   by name is a `list`/`find` job.
3. **`nx_hole` dropped:** `nx_create_hole` places on a planar face; a free cut is
   a sketch plus `nx_create_extrude` with a subtract `Combine`.
4. **Sheet metal:** one tool with per-operation sub-schemas (`variants`).
5. **Anchors:** features, bodies and sketches carry a persistent anchor
   automatically; faces and edges only on request (`anchor: true` on the
   returning tool or `nx_create_anchor`).
6. **Batch scope:** only tools marked `batchable` (sketch geometry,
   constraints and dimensions, annotations, component placement); heavy
   features stay one call each.
7. **Default profile:** `agent`; `full` stays available.
8. **Generic delete:** `nx_delete_objects` accepts mixed kinds and reports the
   dependent objects NX deletes with them.
9. **Appearance:** `view` effect, restorable through display snapshots; the
   description says it persists when the part is saved.
10. **Export does not save:** `nx_export_model` exports the in-memory state where
    the translator allows it, otherwise a temporary copy; it never saves the
    engineer's part.
11. **Evidence gaps:** a tool without `tested` evidence on the connected NX build
    is unavailable there (refused by the kernel, hidden from discovery); there is
    no `api_only` state visible to agents.
12. **Units in arguments:** a plain number is in part units (sheet units for
    sheet quantities); a string with a unit (`"10 mm"`, `"0.5 in"`, `"30 deg"`)
    is converted explicitly. Results always state their units.
