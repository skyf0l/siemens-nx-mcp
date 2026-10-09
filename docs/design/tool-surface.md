# NX MCP tool surface: target design

Status: design of record, implemented. The generated [tool reference](../tools.md) is the
authoritative list of tools and arguments; section 3 keeps the original mapping from the
227 pre-migration tools and the rationale. Scope: the public tool surface (names, arguments,
results, errors, profiles) of the catalogue-based re-architecture. Names and
arguments change freely: no aliases, no backwards compatibility.

**Summary.** 227 current tools become **149 task tools** plus 3 new tools
(`nx_invoke`, `nx_invoke_read`, `nx_delete_results`), 152 in total. Every current
tool maps to exactly one new tool (section 3, checked by script). The 151
distinct error codes raised in `src/nx_mcp` become 22 categories and 66 codes.
Code layout, the kernel and migration are specified in [architecture.md](architecture.md). Listing every tool directly drops from about
55-60K tokens to about 27K. The recommended agent profile costs about 4K tokens.

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
| `domain` | `session`, `parts_files`, `sketch`, `modeling`, `assembly`, `drawing`, `sheet_metal`, `freeform`, `inspection`, `display` |
| `effect` | `read`, `view`, `model`, `file` or `session` (1.3). One fixed value per tool, never dependent on arguments |
| `label` | Static name, imperative, at most 48 characters ("Extrude sketch", "Measure distance"); discovery rows, undo-mark name, panel fallback |
| `summary` | Panel line rendered from the arguments (template, per-variant templates or function), at most 60 characters rendered (`Extrude "profile" 10 mm`) |
| `description` | Model-facing text (1.7), the handler's docstring |
| `keywords` | 3-8 synonyms and NX command names used by search ("pad, boss, protrude, cut") |
| `input` / `output` | The handler signature and its returned `TypedDict`, built from the shared types (1.4); stdlib only, the sidecar derives Pydantic models and JSON Schema |
| `requires` | Preconditions checked by the kernel before NX: `work_part`, `no_active_sketch`, `application:sheet_metal`, `drawing_sheet`, `interactive`, `assembly` |
| `batchable` / `previewable` | Whether `nx_run_batch` / `nx_preview_changes` may run it |
| `variants` | Named sub-schemas for discriminated unions (`nx_create_drawing_view#section`) |
| `evidence` | Not written by hand: the acceptance runner records, per NX build, the variants that passed machine-checked scenarios (`evidence/<domain>.json`). A build key present means tested; there is no other visible state (decision 11) |
| `handler` | The decorated function itself (`domains/modeling/datums.py: nx_create_datum_plane`) |
| `cost`, `invalidates`, `refresh_annotations`, `runs`, `example` | Long-operation class, reference impact, managed-annotation refresh, sidecar-only tools, one minimal example (architecture.md 1.2) |

The kernel runs every call, including each batch and preview child, through
fixed gates in this order: schema validation → workspace path validation →
evidence/version gate → `requires` preconditions → engineer control hold →
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
| `analyze` | Sampled field data (curvature, draft, derivatives) | read |
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
| `preview` | Apply temporarily, report, roll back | session |
| `run` | Execute a batch | model |
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
| `Ref` | `"b:41"` | Opaque NX object reference returned by an earlier call. Never a display name |
| `Point2` | `[x, y]` | Sketch-local point (part units). The only 2D point shape: no `{x,y}`, no `cx/cy` |
| `SheetPoint` | `[x, y]` | Drawing-sheet point in sheet units |
| `Point3` | `[x, y, z]` | Work-part absolute point in part units |
| `Vec3` | `[x, y, z]` | Nonzero direction; the kernel normalises it and reports the normalised value |
| `Mat3` | `[[r00,r01,r02],[r10,r11,r12],[r20,r21,r22]]` | Right-handed orthonormal rotation, row-major, `p_out = R·p + t` |
| `Pose` | `{translation: Point3, rotation?: Mat3}` | Absolute placement; rotation defaults to identity |
| `Length` | number or `"10 mm"` | Plain number: part (work-part) length units; a string with `mm`/`in` is converted (decision 12) |
| `SheetLength` | number or `"10 mm"` | Plain number: sheet units of the target sheet |
| `Angle` | number or `"0.5 rad"` | Plain number: degrees; a string with `deg`/`rad` is converted |
| `Units` | `"mm"` or `"in"` | Everywhere (parts, sheets, exports). `"inch"` disappears. Expressions add `"deg"`, `"rad"`, `"unitless"` |
| `Plane` | `Ref` (datum plane or planar face) or `{principal: "xy"/"yz"/"xz"}` or `{origin: Point3, normal: Vec3}` | One plane shape for sketches, mirrors, datums, trims |
| `Axis` | `Ref` (datum axis, linear edge, cylindrical face) or `{principal: "x"/"y"/"z"/"-x"/"-y"/"-z"}` or `{origin: Point3, direction: Vec3}` | One axis shape for revolve, circular patterns, rotations |
| `Direction` | `Vec3` or `Ref` (linear edge, datum axis, planar face normal) or `{principal: "x"/.../"-z"}` | Directions for extrude, draft, patterns |
| `SketchPoint` | `{curve: Ref, point: "start"/"end"/"center"/"mid"}` | A characteristic point of a sketch curve |
| `Limit` | `{type: "distance", value: Length}` or `{type: "through_all"}` or `{type: "until_face", face: Ref}` | Extrude and hole limits |
| `Combine` | `{operation: "unite"/"subtract"/"intersect", targets: Ref[1..20]}` | Boolean of a new feature with existing bodies; omitted = new body |
| `Page` | `offset=0`, `limit=20` (max 200) | Every inventory argument pair |

Enum values are lower snake case (`"first_angle"`, `"mmc"`, `"profile_of_a_surface"`).

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
| `changes` | `model` tools only. `references`: `preserved` (all earlier refs still valid), `part_invalidated` (re-list the part), `listed` (only those in `deleted` are invalid) |
| `warnings` | `{code, message}`; never dropped by compaction. Warning codes (`NX_UPDATE_WARNING`, `NX_APPROXIMATED`, `NX_PARTIAL_PAGE`, `NX_MODAL_DIALOG` = NX showed a modal dialog during the call) are a separate short list |
| `page` | Inventories: `{offset, limit, returned, total, next_offset}` |
| `result_id`, `omitted` | Compact projection: arrays over 20 items are cut, `nx_get_result` expands them; one snapshot store for bridge and sidecar results |
| `restore_id` | `view` tools that change display state |
| `marks_part_modified` | When a non-model call nevertheless marks a part modified |

### 1.7 Description style and length budget

| Item | Budget | Rule |
|---|---|---|
| Description first sentence | at most 120 characters | What the tool does in engineer terms, starting with a verb |
| Whole description | at most 300 characters | Then only non-obvious constraints: preconditions, which reference to reacquire, the one gotcha. No units or field docs (fields carry them) and no advertising ("native", "associative" only when it changes behaviour) |
| Field description | at most 80 characters | Units, frame, range |
| Tool definition (name + description + input schema) | median 700, cap 1,200 characters; cap 2,500 for discriminated unions | Checked by a catalogue test |
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
| 2 | `unsupported` | `NX_UNSUPPORTED_OPTION`, `NX_UNSUPPORTED_GEOMETRY`, `NX_UNSUPPORTED_FILE_TYPE` | never | Choose another tool or approach | `NX_UNSUPPORTED_ARGUMENT`, `NX_UNSUPPORTED_EDIT`, `NX_UNSUPPORTED_SCOPE`, `NX_UNSUPPORTED_UNITS`, `NX_UNSUPPORTED_FRAME`, `NX_UNSUPPORTED_GEOMETRY`, `NX_UNSUPPORTED_IMAGE`, `NX_UNSUPPORTED_FILE_TYPE`, `NX_NOT_IMPLEMENTED` |
| 3 | `not_found` | `NX_NOT_FOUND`, `NX_FILE_NOT_FOUND`, `NX_RESULT_EXPIRED`, `NX_UNKNOWN_TOOL` | after_fix | Re-list; for expired results re-read (never repeat a mutation) | `NX_NOT_FOUND`, `NX_OBJECT_NOT_FOUND`, `NX_FILE_NOT_FOUND`, `NX_DIRECTORY_NOT_FOUND`, `NX_RESULT_EXPIRED`, `NX_TOOL_NOT_FOUND`, `NX_NO_TABLE_HEADER` |
| 4 | `stale_reference` | `NX_STALE_REFERENCE`, `NX_STALE_CHECKPOINT`, `NX_STALE_PREVIEW`, `NX_STALE_SOURCE` | after_reacquire | Reacquire references (`nx_list_objects`, `nx_find_geometry`, `nx_resolve_reference`) and retry | `NX_OBJECT_STALE`, `NX_STALE_REFERENCE`, `NX_SAVED_REFERENCE_STALE`, `NX_STALE_TRACE_ANCHOR`, `NX_STALE_ANNOTATION_SOURCE`, `NX_DISPLAY_SNAPSHOT_STALE`, `NX_CHECKPOINT_STALE`, `NX_PREVIEW_STALE`, `NX_SOURCE_CHANGED`, `NX_ARTIFACT_CHANGED`, `NX_EXPLOSION_ASSEMBLY_CHANGED` |
| 5 | `wrong_kind` | `NX_WRONG_KIND`, `NX_WRONG_GEOMETRY` | after_fix | Pass a reference of the kind or geometry the field names | `NX_OBJECT_TYPE_MISMATCH`, `NX_NOT_SOLID`, `NX_NOT_SHEET`, `NX_NON_PLANAR`, `NX_NOT_SHEET_METAL`, `NX_NOT_DIRECTORY`, `NX_INVALID_GEOMETRY` (input side) |
| 6 | `wrong_owner` | `NX_WRONG_OWNER`, `NX_WRONG_PART_CONTEXT` | after_fix | Use geometry of the right body/part, or activate the owning part | `NX_OBJECT_OWNER_MISMATCH`, `NX_DISPLAY_PART_MISMATCH`, `NX_PART_CONTEXT`, `NX_EXPLOSION_VIEW_MISMATCH` |
| 7 | `ambiguous` | `NX_AMBIGUOUS_REFERENCE`, `NX_TARGET_REQUIRED` | after_fix | Pass the explicit reference; `details.candidates` lists choices | `NX_AMBIGUOUS_REFERENCE`, `NX_AMBIGUOUS_GEOMETRY`, `NX_AMBIGUOUS_TARGET`, `NX_AMBIGUOUS_THREAD`, `NX_OBJECT_OWNER_AMBIGUOUS`, `NX_NO_TARGET_BODY` |
| 8 | `precondition` | `NX_SKETCH_ACTIVE`, `NX_SKETCH_NOT_ACTIVE`, `NX_NO_WORK_PART`, `NX_WRONG_APPLICATION`, `NX_NOT_LOADED`, `NX_UNSAVED_PART`, `NX_PART_IN_USE` | after_fix | Run the tool named in `hint` (finish sketch, activate part, save, load components), then retry | `NX_SKETCH_ACTIVE`, `NX_SKETCH_NOT_ACTIVE`, `NX_NO_ACTIVE_SKETCH`, `NX_NO_PART_OPEN`, `NX_NO_WORK_PART`, `NX_NO_DISPLAY_PART`, `NX_DRAWING_CONTEXT`, `NX_DRAWING_ACTIVE`, `NX_NO_DRAWING`, `NX_NO_COMPONENTS`, `NX_UNLOADED_COMPONENT`, `NX_ASSEMBLY_NOT_LOADED`, `NX_UNSAVED_PART`, `NX_PART_IN_USE`, `NX_APPLICATION_UNAVAILABLE` |
| 9 | `already_exists` | `NX_ALREADY_EXISTS`, `NX_NAME_CONFLICT` | after_fix | Choose another name or path, or use the existing object in `details` | `NX_FILE_EXISTS`, `NX_ALREADY_EXISTS`, `NX_NAME_EXISTS`, `NX_NAME_CONFLICT`, `NX_IMPORT_NAME_CONFLICT`, `NX_MATERIAL_EXISTS`, `NX_SECTION_EXISTS`, `NX_PARTS_LIST_EXISTS` |
| 10 | `blocked` | `NX_DEPENDENCY_ORDER`, `NX_IN_USE`, `NX_READ_ONLY`, `NX_CROSS_PART` | after_fix | Resolve the blocker listed in `details` (dependent views, parent order, locked expression); ask the engineer when it is their data | `NX_REORDER_DEPENDENCY`, `NX_REORDER_REJECTED`, `NX_EXPLOSION_IN_USE`, `NX_EXPRESSION_READ_ONLY`, `NX_CROSS_PART_ROLLBACK`, `NX_RESTORE_ORDER`, `NX_NOT_CANCELLABLE` |
| 11 | `geometry_failed` | `NX_BUILD_FAILED`, `NX_UPDATE_FAILED`, `NX_INCOMPLETE_RESULT` | after_fix | Model rolled back; change dimensions or inputs (smaller radius, other face). Do not repeat unchanged | `NX_OPERATION_FAILED`, `NX_UPDATE_FAILED`, `NX_FACE_EDIT_FAILED`, `NX_INCOMPLETE_SEW`, `NX_SINGULAR_SURFACE`, `NX_SHEET_METAL_INVALID`, `NX_SKETCH_INVALID`, `NX_DRAWING_INVALID`, `NX_ANNOTATION_INVALID`, `NX_INTERFERENCE_UNRESOLVED`, `NX_IMPORT_NO_OUTPUT`, `NX_EXPORT_NO_SOLIDS`, `NX_MOTION_RESULT_MISSING`, `NX_MOTION_NOT_ASSOCIATIVE` |
| 12 | `solver_failed` | `NX_CONSTRAINT_UNSATISFIED`, `NX_SKETCH_OVERCONSTRAINED` | after_fix | Diagnose with `nx_check_sketch` or `nx_list_objects(kind=assembly_constraint)`; remove the conflicting constraint | `NX_CONSTRAINT_UNSATISFIED`, `NX_RELATION_UNSATISFIED`, `NX_SKETCH_CONFLICT`, `NX_CONSTRAINT_REMOVED` |
| 13 | `verification_failed` | `NX_VERIFICATION_FAILED` (`details.check` names the failed read-back) | ask_engineer | NX built something that differs from the request; it was rolled back. Report, do not loop | `NX_VERIFICATION_FAILED`, `NX_PATTERN_VERIFICATION_FAILED`, `NX_SAVE_VERIFICATION_FAILED`, `NX_MATERIAL_VERIFICATION_FAILED`, `NX_PLACEMENT_MISMATCH`, `NX_PLACEMENT_CHANGED`, `NX_EXPLOSION_POSITION_MISMATCH`, `NX_FRAME_MISMATCH`, `NX_CAPTURE_RESOLUTION_MISMATCH`, `NX_CLONE_REFERENCE_MISMATCH` |
| 14 | `limit_exceeded` | `NX_TOO_MANY_OBJECTS`, `NX_REQUEST_TOO_LARGE`, `NX_RESPONSE_TOO_LARGE` | after_fix | Split the request or page smaller | `NX_OBJECT_LIMIT`, `NX_SAMPLE_LIMIT`, `NX_PAIR_LIMIT`, `NX_REQUEST_TOO_LARGE`, `NX_RESPONSE_TOO_LARGE`, `NX_IMAGE_TOO_LARGE` |
| 15 | `path_rejected` | `NX_PATH_OUTSIDE_WORKSPACE`, `NX_PATH_RESERVED` | after_fix | Use a workspace-relative path (`nx_get_status` shows the root) | `NX_PATH_OUTSIDE_WORKSPACE`, `NX_PATH_RESERVED` |
| 16 | `file_failed` | `NX_IMPORT_FAILED`, `NX_EXPORT_FAILED`, `NX_SAVE_FAILED`, `NX_CAPTURE_FAILED`, `NX_TRANSFER_FAILED` | after_fix | Read `details` (translator log, checksum); fix input; ask the engineer for save failures | `NX_IMPORT_FAILED`, `NX_EXPORT_FAILED`, `NX_SAVE_FAILED`, `NX_SAVE_PREFLIGHT_FAILED`, `NX_CLONE_FAILED`, `NX_CAPTURE_FAILED`, `NX_SCREENSHOT_FAILED`, `NX_COMPONENT_LOAD_FAILED`, `NX_DIRECTORY_ERROR`, `NX_CHECKSUM_MISMATCH`, `NX_UPLOAD_GAP`, `NX_UPLOAD_CONFLICT`, `NX_RECEIPT_UNREADABLE`, `NX_RESULT_STORAGE_FAILED` |
| 17 | `unavailable` | `NX_LICENSE_UNAVAILABLE`, `NX_VERSION_UNSUPPORTED`, `NX_VIEWPORT_UNAVAILABLE`, `NX_CATALOG_UNAVAILABLE` | never | Tell the engineer which license or build is missing; use an alternative if one exists | `NX_LICENSE_UNAVAILABLE`, `NX_UNAVAILABLE_ON_VERSION`, `NX_API_UNAVAILABLE`, `NX_VIEWPORT_UNAVAILABLE`, `NX_CATALOG_UNAVAILABLE`, `NX_UNDO_UNAVAILABLE` |
| 18 | `engineer_control` | `NX_CONTROL_DECLINED`, `NX_CONTROL_PAUSED`, `NX_UI_BUSY`, `NX_PART_NOT_GRANTED`, `NX_MODAL_DIALOG_OPEN` | ask_engineer | The engineer has NX. Ask in chat before requesting control again; `NX_UI_BUSY` = wait and retry; `NX_PART_NOT_GRANTED` = the part is the engineer's: ask them to grant it on the panel or work on a copy; `NX_MODAL_DIALOG_OPEN` = in Agent drives a modal NX dialog blocks NX before a model or view step (`details.dialogs`; reads still answer; Auto and Ask me first wait as for `NX_UI_BUSY`): ask the engineer to answer it, the agent never clicks it | `NX_UI_DECLINED`, `NX_UI_PAUSED`, `NX_UI_BUSY` (the ownership gate is new) |
| 19 | `bridge` | `NX_BRIDGE_UNAVAILABLE`, `NX_PROTOCOL_MISMATCH`, `NX_AUTH_FAILED` | after_wait | NX or the bridge is not running or was restarted; for mutations check `nx_get_operation` first; ask the engineer if it persists | `NX_BRIDGE_UNAVAILABLE`, `NX_PROTOCOL_ERROR`, `NX_PROTOCOL_VERSION_MISMATCH`, `NX_AUTH_FAILED`, `NX_MAIN_THREAD_UNAVAILABLE`, `NX_BRIDGE_OUTCOME_UNKNOWN` |
| 20 | `interrupted` | `NX_CANCELLED`, `NX_OUTCOME_UNKNOWN`, `NX_IDEMPOTENCY_CONFLICT` | after_status_check | Query `nx_get_operation(operation_id)`; never retry a mutation blindly; reuse an `operation_id` only with identical arguments | `NX_CANCELLED`, `NX_IDEMPOTENCY_CONFLICT`, `NX_BRIDGE_OUTCOME_UNKNOWN` |
| 21 | `rollback_failed` | `NX_ROLLBACK_FAILED`, `NX_RESTORE_FAILED` | ask_engineer | The model or view may be partly changed. Stop mutating, capture a screenshot, tell the engineer | `NX_ROLLBACK_FAILED`, `NX_SAVE_VIEW_RESTORE_FAILED`, `NX_RENDER_RESTORE_FAILED` |
| 22 | `nx_error` | `NX_ERROR` | ask_engineer | Unclassified native exception; `nx_code` and message preserved. Retry once only after changing inputs | `NX_API_ERROR`, `NX_INTERNAL_ERROR` |

`NX_MCP_*` strings in the code are environment variables and marker names, not
error codes. The new code list has 65 entries (the "Codes" column); a
catalogue test fails when a handler raises a code that is not in the list.

### 1.9 `mutation_outcome`

Present in every result, success or error.

| Value | Meaning | References | Agent may |
|---|---|---|---|
| `not_applicable` | Tool effect is `read` | preserved | continue |
| `committed` | Every change applied and verified | `changes.references` says which survive | continue |
| `previewed` | Changes applied temporarily and reverted (`nx_preview_changes`) | refs in the preview result are stale | accept or discard the preview |
| `not_started` | Failed before NX, the session or the workspace changed | preserved | fix and retry |
| `rolled_back` | Started, failed, undo mark restored | `changes.references` = `preserved` or `part_invalidated` | fix, reacquire if needed, retry |
| `partial` | Some changes remain (rollback failed, or a non-undoable step such as a translator file or a session change ran) | `part_invalidated` | inspect (`nx_check_model`, screenshot); ask the engineer before more mutations |
| `unknown` | Transport lost or timeout; NX may still finish | unknown | query `nx_get_operation(operation_id)` only; never retry until it reports `committed` or `failed` |

`operation_id` is optional; the kernel generates one when absent and returns it.
Supply your own when you might need to retry after a transport failure.

---

## 2. Surface profiles

### 2.1 How agents see tools

| Profile | Listed directly | Reached through gateway | Use |
|---|---|---|---|
| `agent` (default, recommended) | 4 gateway tools + 14 core tools | all 149 task tools | Any MCP client; smallest context, read-only gateway can be auto-approved |
| `full` | all 152 tools with `readOnlyHint`/`destructiveHint` from `effect` | none needed | Clients with native deferred tool search (they index names and load schemas on demand) |

Gateway tools (always listed):

| Tool | Purpose | Key arguments | Effect |
|---|---|---|---|
| `nx_find_tools` | Ranked search: exact name > keyword/synonym > summary > description; all query words must match after stemming and synonym expansion ("fillet" finds `nx_create_blend`, "mate" finds `nx_create_assembly_constraint`). Rows: name, summary, effect, domain, evidence for the connected build | `query`, `domain?`, `effect?`, `limit=10` (max 20), `offset` | read |
| `nx_get_tool_schemas` | Exact input schema, example, prerequisites and evidence for 1-10 tools or variants in one call; output schema on request | `names[1..10]` (e.g. `nx_create_drawing_view#section`), `include_output=false` | read |
| `nx_invoke` | Call any task tool; kernel gates apply; `intent` (at most 120 characters) is shown in the NX panel | `tool`, `arguments`, `intent?`, `detail=compact/full` | per tool (declared destructive) |
| `nx_invoke_read` | Same, but refuses tools whose effect is not `read` before validation; hosts can auto-approve it | `tool`, `arguments`, `detail` | read |

Core tools listed directly in the `agent` profile (most-used, mostly read):
`nx_get_status`, `nx_get_operation`, `nx_get_result`, `nx_list_parts`,
`nx_open_part`, `nx_list_components`, `nx_list_objects`, `nx_get_model_summary`,
`nx_find_geometry`, `nx_measure_distance`, `nx_check_model`, `nx_capture_view`,
`nx_undo_operation`, `nx_download_file`.

### 2.2 Token cost

Estimates at 3.8 characters per token for JSON schemas; measured values come from
the catalogue test, which fails above budget.

| Item | Today | Target |
|---|---|---|
| Full listing (name + description + input schema) | 218,951 characters, about 55-60K tokens for 227 tools (median 851, max 6,077 characters per tool) | At most 880 characters per listed tool on average, call keys (`operation_id`, `intent`: 87 characters on each changing tool) included: 157 tools averaged 798.5 (125,371 characters, about 33K tokens) on 2026-10-10. A mean rather than a total, so adding tools does not move the cap; the per-tool caps (1,200, 2,500 for unions, median 700) still bound each definition. The earlier fixed totals (110,000, then 125,000) are retired; the full profile is opt-in for clients that load schemas on demand, the default `agent` profile stays at 15K |
| `agent` profile listing | 13 tools, 11,588 characters, about 3K tokens | 18 tools, about 15K characters, about 4K tokens |
| One discovery call (10 rows) | full descriptions, 1-3K tokens | name + summary + effect rows, about 300 tokens |
| Schema fetch | one tool per call, 250-1,500 tokens each | 1-10 tools per call, about 200 tokens per tool |
| Typical modeling task (find 4 tools, fetch 4 schemas, 10 calls) | about 8-10K tokens of tool metadata | about 5K tokens |

---

## 3. Tool catalogue

Every row's "Replaces" lists current tools; each of the 227 appears exactly once.
"Note" names actions of a replaced tool that move elsewhere. "—" means a new tool.

### 3.1 Session and kernel (`session`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_get_status` | Bridge, NX build, work/display part, application, UI control state, workspace root and path rules | `include?: bridge/parts/control/workspace` | read | `nx_status`, `nx_workspace_info` |
| `nx_set_control` | Request NX input for the agent or hand it back to the engineer | `mode: agent/manual` | session | `nx_ui_control` (status mode → `nx_get_status`) |
| `nx_find_tools` | Search the catalogue (2.1) | `query, domain?, effect?, Page` | read | — |
| `nx_get_tool_schemas` | Schemas, examples, evidence, sub-schemas such as sheet-metal operations | `names[1..10], include_output` | read | `nx_capabilities`, `nx_sheet_metal_schema` |
| `nx_invoke` | Gateway call | `tool, arguments, intent?, detail` | per tool | — |
| `nx_invoke_read` | Gateway call for read tools only | `tool, arguments, detail` | read | — |
| `nx_get_operation` | Durable state of an operation: running, committed, failed, unknown | `operation_id` | read | `nx_operation_status` |
| `nx_cancel_operation` | Cooperative cancel between batch steps | `operation_id` | session | `nx_cancel_operation` |
| `nx_get_result` | Page or expand a stored result (one store for bridge and sidecar snapshots) | `result_id, field?: JSON pointer, Page, detail` | read | `nx_read_result` |
| `nx_delete_results` | Preview or prune stored result snapshots | `dry_run=true, max_age_seconds?, max_bytes?, result_ids?` | file | — |
| `nx_run_batch` | 1-100 batchable calls under one undo mark; `"$2.data.feature"` refers to an earlier step's result | `operations[1..100]: {tool, arguments}` | model | `nx_batch` |
| `nx_preview_changes` | Apply 1-25 previewable calls, report before/after bounds, volume, health and an optional image, then roll back | `operations[1..25], capture=false` | session | `nx_preview_change` |
| `nx_finish_preview` | Re-apply a preview if the part is unchanged, or discard it | `preview_id, action: accept/discard` | model | `nx_finish_preview` |
| `nx_undo_operation` | Undo the last agent operation | — | model | `nx_undo` |
| `nx_create_checkpoint` | In-session undo checkpoint (expires on save) | `label?` | session | `nx_checkpoint` |
| `nx_list_checkpoints` | Available checkpoints and retained history | — | read | `nx_checkpoint_state` |
| `nx_restore_checkpoint` | Roll the model back to a checkpoint | `checkpoint_id` | model | `nx_rollback` |
| `nx_activate_application` | Switch the NX application for the displayed part | `application: modeling/sheet_metal` | session | `nx_sheet_metal_context` |
| `nx_delete_objects` | Delete features, components, explosions, annotations (incl. dimensions, tables, balloons, traces), section views, sheets, views | `objects: Ref[1..100]` | model | `nx_delete_feature`, `nx_delete_explosion` |
| `nx_set_name` | Rename any object; returns the NX-normalised name | `object, name` | model | `nx_rename_object` |

### 3.2 Parts and files (`parts_files`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_list_files` | Workspace directory listing with size and SHA-256 | `path="", prefix?, Page` | read | `nx_workspace_list` |
| `nx_create_directory` | Create a workspace directory (idempotent) | `path` | file | `nx_create_directory` |
| `nx_upload_file` | Upload one chunk of a new workspace file | `path, data_base64, offset, total_size, sha256` | file | `nx_upload_file` |
| `nx_download_file` | Metadata, inline PNG, or base64 chunk of a workspace file | `path, delivery: metadata/image/chunk, offset?, length?` | read | `nx_download_file` |
| `nx_create_part` | New part file, loaded and made work part | `path, units: mm/in` | file | `nx_create_part` |
| `nx_open_part` | Open or reuse a part; optionally load all components | `path, work=true, display=true, load_components=false` | session | `nx_open_part` |
| `nx_activate_part` | Make a loaded part work and/or display part | `part, work, display` | session | `nx_activate_part` |
| `nx_list_parts` | Loaded parts with modified state, paged | `path_prefix?, modified?, active_only=false, Page` | read | `nx_list_open_parts` |
| `nx_save_part` | Save a part in place, or save as a new workspace path | `part?, path?` (path = save as, never overwrites) | file | `nx_save_part`, `nx_save_as` |
| `nx_close_part` | Close a part (refused while a loaded assembly uses it) | `part?, save=true` | session | `nx_close_part` |
| `nx_export_assembly` | Copy the saved assembly with all loaded prototypes as a ZIP package or as a renamed clone folder | `path, format: zip/clone, prefix? (clone), activate=false` | file | `nx_package_assembly`, `nx_copy_project` |
| `nx_export_model` | Export the work part or bodies to a neutral format | `path, format: step (others reserved, 4), bodies?` | file | `nx_export_step` |
| `nx_import_model` | Import a neutral file into the work part or a new part | `path, format: step, target: work_part/new_part, new_part_path?, flatten=true` | model | `nx_import_geometry` |
| `nx_export_profile` | Export a sketch or planar face as 1:1 LINE/ARC/CIRCLE DXF | `source, path, frame?: {origin, x_axis, y_axis}, layer="0", layers?` | file | `nx_export_planar_dxf` |

### 3.3 Feature tree and expressions (`modeling`)

Features are listed with `nx_list_objects(kind=feature)` (3.9).

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_get_feature` | Type, timestamp, state, parents/children, parameters with expressions, units and editability | `feature` | read | `nx_get_feature_info`, `nx_feature_parameters` |
| `nx_edit_feature` | Set feature parameters by expression name or semantic alias (`distance`, `count`, `spacing`); a value may be a number, a formula or another expression name (binding); `definition` replaces non-expression definitions (transform, spline points, sheet-metal parameters) | `feature, parameters?: {name: number/formula}[1..25], definition?` | model | `nx_edit_feature`, `nx_set_feature_parameters`, `nx_bind_parameter` |
| `nx_set_feature_suppression` | Suppress or unsuppress features (dependents follow) | `features[1..100], suppressed` | model | `nx_suppress_features` |
| `nx_set_current_feature` | Roll the timeline to a feature, or to the end when omitted | `feature?` | model | `nx_make_current_feature` |
| `nx_set_feature_order` | Move a feature before or after another | `feature, anchor, position: before/after` | model | `nx_reorder_feature` |
| `nx_set_expression` | Create a named expression or change one's formula | `name, formula, units?: mm/in/deg/rad/unitless (new only)` | model | `nx_set_expression` |
| `nx_update_model` | Run the NX model update and return health | — | model | `nx_rebuild_model` |

### 3.4 Datums (`modeling`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_create_datum_plane` | Datum plane: offset from a plane, point+normal, or three points | `method: offset/point_normal/three_points, reference?, distance?, origin?, normal?, points?: Point3[3], name?` | model | `nx_datum_plane` |
| `nx_create_datum_axis` | Datum axis through a point along a direction, or through two points | `method: point_direction/two_points, origin?, direction?, start?, end?, name?` | model | `nx_datum_axis` |
| `nx_create_datum_csys` | Datum coordinate system | `origin, x_axis, y_axis, name?` | model | `nx_datum_csys` |
| `nx_create_datum_point` | Point feature | `position: Point3, name?` | model | `nx_datum_point` |

### 3.5 Sketch (`sketch`)

Sketches are listed with `nx_list_objects(kind=sketch)`. All sketch coordinates are
`Point2` in the returned sketch-local frame.

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_create_sketch` | Create and activate a sketch on a principal plane, a face or datum plane, or normal to an edge path | `placement: {principal: xy/xz/yz, origin?, x_axis?}` or `{on: Ref, x_direction?}` or `{path: edges[], help_point, percent, orienting_face?, reverse_normal, reverse_axis}`, `name?` | model | `nx_create_sketch`, `nx_create_sketch_on`, `nx_create_path_sketch` |
| `nx_finish_sketch` | Deactivate the sketch | `sketch` | model | `nx_finish_sketch` |
| `nx_get_sketch` | Frame, curves (local and part coordinates), constraints, solver status and remaining DOF | `sketch, include?: geometry/constraints/status` | read | `nx_sketch_info`, `nx_sketch_diagnostics` |
| `nx_check_sketch` | Find the constraints that cause an over- or inconsistent constraint state | `sketch, max_checks=20` | read | `nx_sketch_conflicts` |
| `nx_create_sketch_curves` | Add 1-100 curves in one call | `sketch, curves[1..100]` of `line{start,end}`, `arc{center,radius,start_angle,end_angle}`, `circle{center,radius}`, `ellipse{center,major_radius,minor_radius,rotation,start_angle,end_angle}`, `rectangle{corner1,corner2}`, `polygon{center,sides,radius,inscribed,rotation}`, `slot{center,width,height}`, `rounded_rectangle{center,width,height,radius}`; `construction=false` | model | `nx_sketch_line`, `nx_sketch_rectangle`, `nx_sketch_arc`, `nx_sketch_ellipse`, `nx_sketch_polygon`, `nx_sketch_primitive` |
| `nx_edit_sketch_curves` | 1-100 edits: reshape, trim/extend, construction toggle, delete | `sketch, edits[1..100]` of `line{curve,start,end}`, `arc{curve,center,radius,start_angle,end_angle}`, `trim{curve,boundaries,pick}`, `extend{curve,boundaries,pick}`, `construction{curves,on}`, `delete{object}` | model | `nx_edit_sketch`, `nx_sketch_construction`, `nx_sketch_trim_extend` |
| `nx_create_sketch_corner` | Fillet or chamfer the corner between two curves (both trimmed) | `sketch, first, second, kind: fillet/chamfer, radius?, distance?, distance2?` | model | `nx_sketch_fillet`, `nx_sketch_chamfer` |
| `nx_create_sketch_copy` | Offset or mirror curves (associative) | `sketch, curves, mode: offset/mirror, distance?, side?: auto/left/right, mirror_line?` | model | `nx_sketch_offset`, `nx_sketch_mirror` |
| `nx_create_sketch_projection` | Bring model geometry into the sketch: project edges/curves or intersect faces with the plane | `sketch, objects, mode: project/intersect` | model | `nx_sketch_project`, `nx_sketch_intersection` |
| `nx_create_sketch_constraints` | 1-50 geometric constraints | `sketch, constraints[1..50]: {type: fixed/horizontal/vertical/parallel/perpendicular/equal_length/equal_radius/concentric/tangent/coincident/midpoint/point_on_curve/symmetric, curves?: Ref[], points?: SketchPoint[], centerline?}` | model | `nx_sketch_constraint`, `nx_sketch_relation`, `nx_sketch_tangent`, `nx_sketch_symmetry`, `nx_sketch_midpoint`, `nx_sketch_point_on_curve` |
| `nx_create_sketch_dimensions` | 1-50 driving or reference dimensions; each returns its expression | `sketch, dimensions[1..50]: {kind: length/horizontal/vertical/distance/radius/diameter/angle/parallel_distance/perpendicular_distance, first: Ref/SketchPoint, second?, value, origin: Point2, reference=false}` | model | `nx_sketch_dimension`, `nx_sketch_angle`, `nx_sketch_curve_dimension`, `nx_sketch_point_dimension` |

Note: dimensional types of `nx_sketch_constraint` (distance, length, radius,
diameter, angle) move to `nx_create_sketch_dimensions`.

### 3.6 Solid modeling (`modeling`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_create_extrude` | Extrude a sketch | `sketch, end: Limit, start?: Limit (distance 0), symmetric=false, direction?: Direction, combine?: Combine` | model | `nx_extrude` |
| `nx_create_revolve` | Revolve a sketch about an axis | `sketch, axis: Axis, start_angle=0, end_angle=360, combine?` | model | `nx_revolve` |
| `nx_create_sweep` | Sweep a section sketch along a guide sketch | `section, guide, combine?` | model | `nx_sweep` |
| `nx_create_loft` | Through-curves loft through ordered sketches | `sections: sketch[2..20], solid=true` | model | `nx_loft` |
| `nx_create_hole` | NX Hole feature: simple, counterbore, countersink, threaded; 1-100 positions on a planar face | `face, positions: Point3[1..100], form, diameter, depth: Limit, tip_angle=118, counterbore?{diameter,depth}, countersink?{diameter,angle}, thread?{standard,size,depth}, target?` | model | `nx_hole`, `nx_hole_feature` |
| `nx_create_thread` | Thread on a cylindrical face, from a catalogue row or explicit geometry | `face, start_face, length, spec: {standard, size, method?, engagement?}` or `{pitch, major_diameter, minor_diameter, angle=60, starts=1}`, `detailed=false, hand: right/left, reverse=false` | model | `nx_thread`, `nx_standard_thread` |
| `nx_list_thread_sizes` | Installed thread standards, sizes and size details | `standard?, size?, Page` | read | `nx_thread_catalog` |
| `nx_create_blend` | Constant or variable radius edge blend | `edges[1..200], radius?` or `variable?: [{at: 0..1, radius}][2..20]` (one edge) | model | `nx_blend`, `nx_variable_blend` |
| `nx_create_chamfer` | Chamfer edges | `edges[1..200], method: symmetric/two_offsets/offset_angle, offset, offset2?, angle?, reverse=false` | model | `nx_chamfer`, `nx_chamfer_advanced` |
| `nx_create_shell` | Hollow a solid | `body, thickness, remove_faces?, outward=false` | model | `nx_shell` |
| `nx_create_draft` | Draft faces about a stationary face | `faces, stationary_face, direction: Direction, angle` | model | `nx_draft` |
| `nx_create_boolean` | Unite, subtract or intersect bodies | `operation: unite/subtract/intersect, target, tools: body[1..50]` | model | `nx_boolean` |
| `nx_create_mirror` | Mirror bodies about a plane (features reserved, 4) | `bodies[1..50], plane: Plane, keep_original=true` | model | `nx_mirror_body`, `nx_mirror_body_about` |
| `nx_create_pattern` | Feature pattern; layout carries the parameters | `features[1..50], layout: {linear: {direction: Direction, count, spacing, count2?, direction2?, spacing2?}}` or `{circular: {axis: Axis, count, angle}}` | model | `nx_pattern`, `nx_circular_pattern` |
| `nx_create_trim_body` | Cut a body with a plane: keep one side (Trim Body) or both (Split Body) | `body, tool: Ref (datum plane or planar face), keep: back/front/both` | model | `nx_trim_body`, `nx_split_body` |
| `nx_create_scale` | Uniform scale feature | `bodies, factor, origin?: Point3` | model | `nx_scale_body` |
| `nx_create_transform` | Move or copy bodies by a rigid transform (edit with `nx_edit_feature` `definition`) | `bodies, transform: Pose, copy=false` | model | `nx_transform_bodies` |
| `nx_edit_faces` | Synchronous face edit for any solid, including imported | `faces, action: move/offset/replace/delete, distance?, direction?, replacement?` | model | `nx_edit_faces` |
| `nx_get_material` | Material names and densities, including occurrence prototypes | `bodies?, scope: part/assembly` | read | `nx_material_info` |
| `nx_set_material` | Assign a material to bodies (library materials reserved, 4) | `bodies, material: {custom: {name, density}}` | model | `nx_set_material` |

Note: `nx_hole` (cylinder subtraction from a free point) is replaced by the hole
feature; the agent first finds the placement face with `nx_find_geometry`.

### 3.7 Freeform (`freeform`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_create_spline` | 3D Studio Spline through points or by poles (edit with `nx_edit_feature` `definition`) | `points: Point3[2..1000], method: through_points/poles, degree=3, periodic=false` | model | `nx_spline` |
| `nx_create_bridge_surface` | Bridge two sheet edges with G0/G1/G2 continuity | `first, second, continuity: g0/g1/g2, reverse_second=false` | model | `nx_bridge_surface` |
| `nx_create_mesh_surface` | Through Curve Mesh sheet | `primary: Ref[][2..100], cross: Ref[][2..100], tolerance?` | model | `nx_surface_mesh` |
| `nx_create_sew` | Sew sheets, optionally requiring a closed solid | `target, tools, tolerance?, solid=false` | model | `nx_sew` |
| `nx_create_thicken` | Thicken sheet faces into a solid | `faces, offset1, offset2=0` | model | `nx_thicken` |
| `nx_create_trim_sheet` | Trim a sheet with curve boundaries | `body, boundaries, region_point: Point3, keep=true` | model | `nx_trim_sheet` |

### 3.8 Assembly (`assembly`)

Assembly constraints, component patterns, reference sets and explosions are
listed with `nx_list_objects` (3.9).

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_list_components` | Recursive occurrence tree with load state, paged | `name_contains?, suppressed?, include_poses=false, Page` | read | `nx_list_components` |
| `nx_create_component` | Add a part as a component | `path, name?, pose?: Pose` | model | `nx_add_component` |
| `nx_edit_component` | Rename, replace part, suppress or set reference set of direct children | `components[1..100], name?, replace_with?: path, suppressed?, reference_set?` | model | `nx_component_action`, `nx_set_component_reference_set` |
| `nx_set_component_pose` | Absolute pose or relative move of a direct child | `component, mode: absolute/relative, translation: Vec3, rotation?: Mat3` | model | `nx_set_component_transform`, `nx_reposition_component` |
| `nx_create_assembly_constraint` | Persistent constraint; the solver must report solved | `type: fix/touch/align/distance/parallel/perpendicular/angle/concentric, component, geometry?, target_component?, target_geometry?, value?, alignment: infer/same/opposite` | model | `nx_assembly_constraint`, `nx_mate_component` |
| `nx_edit_assembly_constraint` | Change value, alignment or suppression | `constraint, value?, alignment?, suppressed?` | model | `nx_edit_assembly_constraint` |
| `nx_create_component_pattern` | Pattern a component; associative NX pattern or independent copies | `component, layout: {linear: {direction, count, spacing, count2?, direction2?, spacing2?}}` or `{circular: {axis, count, angle}}`, `associative=true` | model | `nx_component_array`, `nx_native_component_pattern`, `nx_pattern_components` |
| `nx_edit_component_pattern` | Change count, spacing or angle | `pattern, count?, spacing?, count2?, spacing2?, angle?` | model | `nx_edit_component_pattern` |
| `nx_create_reference_set` | Custom reference set from bodies, curves, datums, components | `name, objects[1..1000]` | model | `nx_create_reference_set` |
| `nx_create_explosion` | Named explosion at assembled positions | `name` | model | `nx_create_explosion` |
| `nx_edit_explosion` | Set exploded poses or reset components | `explosion, placements?: [{component, pose: Pose}][..1000], reset_components?` | model | `nx_edit_explosion` |
| `nx_get_explosion` | Exploded and assembled poses, paged | `explosion, Page` | read | `nx_explosion_info` |
| `nx_set_explosion_display` | Show an explosion (or assembled state) in the model view, a drawing view or a saved model view | `explosion?: Ref (null = assembled), drawing_view?, model_view?` | model | `nx_show_explosion` |
| `nx_create_explosion_trace` | Trace line between two component edges | `explosion, start_edge, end_edge, start_percent=0, end_percent=0, start_direction?, end_direction?` | model | `nx_explosion_trace` |
| `nx_edit_explosion_trace` | Change trace anchors and offsets | `trace, start_percent?, end_percent?, start_offset?, end_offset?` | model | `nx_edit_explosion_trace` |
| `nx_export_explosion_animation` | Self-contained HTML animation of an explosion | `explosion, path, frames=12, fps=12, width, height` | file | `nx_export_explosion_animation` |
| `nx_update_assembly` | Propagate prototype changes: solve constraints, refresh traces, BOMs, measured annotations, views | — | model | `nx_update_assembly_documentation` |

Note: `nx_component_action(action=remove)` moves to `nx_delete_objects`.

### 3.9 Inspection (`inspection`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_list_objects` | One inventory for work-part objects: filter, then page. Rows: reference, name, type, state and a kind-specific summary | `kind: body/face/edge/datum/feature/sketch/expression/annotation/dimension/reference_set/explosion/component_pattern/assembly_constraint/section_view/drawing_sheet, owner?: Ref (body, face, sheet, feature), name_contains?, type?, include?: adjacency/views, Page` | read | `nx_list_bodies`, `nx_list_topology`, `nx_flat_pattern_orientation_edges`, `nx_list_datums`, `nx_list_features`, `nx_list_sketches`, `nx_list_expressions`, `nx_list_annotations`, `nx_list_dimensions`, `nx_list_reference_sets`, `nx_list_explosions`, `nx_list_component_patterns`, `nx_list_assembly_constraints`, `nx_list_sections`, `nx_list_drawings` |
| `nx_get_model_summary` | Counts, bounds, health and a short tree of features, expressions, sketches or components | `section: overview/components/features/expressions/sketches, Page` | read | `nx_model_summary` |
| `nx_check_model` | Feature errors and warnings, suppression, unloaded prototypes, body consistency | `scope: part/assembly, Page` | read | `nx_model_health` |
| `nx_find_geometry` | Faces or edges by type, normal, radius, nearest point or extreme; returns refs and a reusable selector | `owner?, kind: face/edge, geometry_type: any/plane/cylinder/circle/line, normal?, radius?, near?: Point3, order: nearest/highest/lowest, axis: x/y/z, tolerance?, Page` | read | `nx_find_geometry` |
| `nx_find_holes` | Inward cylindrical faces grouped coaxially, with radius and axis | `owner?, Page` | read | `nx_recognize_holes` |
| `nx_create_anchor` | Persistent handle for a body, face or edge that survives edits and save/reopen | `object` | read | `nx_geometry_anchor` |
| `nx_resolve_reference` | Current reference from a selector or anchor; unique match or explicit error | `selector?` or `anchor?`, `tie_tolerance?` | read | `nx_resolve_geometry`, `nx_resolve_geometry_anchor` |
| `nx_measure_distance` | Minimum distance and closest points between two objects | `first, second` | read | `nx_measure_distance` |
| `nx_measure_angle` | Angle between lines, straight edges or planar faces | `first, second` | read | `nx_measure_angle` |
| `nx_measure_mass` | Volume, area, mass, centre of gravity, inertia | `bodies?, scope: auto/part/assembly` | read | `nx_measure_volume`, `nx_mass_properties` |
| `nx_measure_bounds` | Bounding box, conservative or exact | `bodies?, scope: auto/part/assembly, precision: conservative/exact` | read | `nx_get_bounding_box` |
| `nx_measure_wall_thickness` | Sampled normal wall thickness | `body, faces?, samples_per_axis=5, tolerance?` | read | `nx_wall_thickness` |
| `nx_analyze_faces` | Sampled normals, curvature and optional draft angles | `faces, samples_per_axis=5, pull_direction?, minimum_draft?` | read | `nx_face_analysis` |
| `nx_analyze_curve` | Sampled positions, tangents, curvature, knots | `curve, samples=20` | read | `nx_curve_analysis` |
| `nx_check_continuity` | G0/G1/G2 pass/fail between two boundary edges | `first, second, tolerances?: {position, angle, curvature}, samples=20` | read | `nx_surface_continuity` |
| `nx_check_interference` | Interference and clearance between two objects or across groups or the whole assembly; highlight the result with `nx_set_highlight` | `objects?: Ref[2..500] (omit = whole assembly), clearance=0, max_pairs=500, include_clear=false, include_contact=true` | read | `nx_check_interference`, `nx_check_clearance`, `nx_highlight_collisions` |
| `nx_export_inspection_report` | ZIP with clearance results, captures and section images | `path, objects?, clearance, max_pairs, section_planes?: Plane[..6], capture=true` | file | `nx_inspection_report` |

### 3.10 Drawing (`drawing`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_create_drawing_sheet` | New sheet, from a template or a standard size | `name?, size: a0/a1/a2/a3/a4 or {width, height}, scale=1, units: mm/in, projection: first_angle/third_angle, template?` | model | `nx_create_drawing`, `nx_create_drawing_sheet` |
| `nx_activate_drawing_sheet` | Open a sheet, or return to modeling with `sheet=null` | `sheet?` | session | `nx_activate_drawing` |
| `nx_create_drawing_view` | Place a view; `type` selects the variant | `type: base{sheet, orientation, scope: body/assembly, body?, explosion?, position, scale?}`, `projected{parent, direction: right/left/top/bottom, spacing?}`, `section{parent, cut: SketchPoint-like {edge, point: start/end/center}, step_direction, arrow_direction, position, scale?}`, `detail{parent, center: Point3, radius, position, scale?}`, `flat_pattern{sheet, flat_pattern, position}` | model | `nx_add_base_view`, `nx_add_projection_view`, `nx_add_section_drawing_view`, `nx_add_detail_drawing_view`, `nx_add_flat_pattern_view` |
| `nx_edit_drawing_view` | Position, scale and display style | `view, position?, scale?, style?: {hidden_lines, smooth_edges, shading, ...}` | model | `nx_edit_drawing_view` |
| `nx_get_drawing_view` | Actual scale, sheet position, border, containment | `view` | read | `nx_drawing_view_info` |
| `nx_create_drawing_dimension` | Associative dimension in a view | `view, kind: linear/radius/diameter/angle/ordinate, first, second?, orientation?: aligned/horizontal/vertical, origin?: SheetPoint`; ordinate: `datum, datum_point, objects[1..50], direction, margin?` | model | `nx_add_dimension`, `nx_add_radial_dimension`, `nx_add_angular_dimension`, `nx_add_ordinate_dimensions` |
| `nx_get_dimension` | Measured value, validity, format and tolerance | `dimension` | read | `nx_dimension_format` |
| `nx_edit_dimension` | Format, tolerance, or rebind to other edges | `dimension, format?: {decimals, trailing_zeros, units, decimal_separator}, tolerance?: {type, upper, lower, decimals}, first?, second?` | model | `nx_edit_dimension_format` |
| `nx_create_note` | Note with optional leader | `text: string[1..50], origin: SheetPoint, sheet?, leader_to?, view?` | model | `nx_add_note` |
| `nx_create_centerline` | Center marks on circular edges, axis of a cylindrical face, or centerline between two edges | `view, kind: center_mark/axis/between_edges, objects[1..50]` | model | `nx_add_center_mark`, `nx_add_centerline` |
| `nx_create_drawing_symbol` | Surface finish or weld symbol with leader | `view, object, origin, symbol: {surface_finish: {roughness_ra, removal: any/required/prohibited}}` or `{weld: {type: fillet/square_butt/v_butt, size, side: arrow/other/both}}` | model | `nx_add_surface_finish`, `nx_add_weld_symbol` |
| `nx_create_gdt` | GD&T as PMI on faces, or drafting GD&T with a leader to view geometry | PMI: `faces, position: Point3`; view: `view, object: edge/face, origin: SheetPoint`; `kind: {datum_feature | view_datum_feature: {letter}}`, `{view_datum_target: {letter, target: {type: point/line/circular/rectangular/annular, index?, diameter?, inner_diameter?, width?, height?}}}` or `{fcf | view_fcf: {characteristic, tolerance, datums?[..3], material?: mmc/lmc/rfs, zone?: diameter/spherical_diameter/square, datum_material?, projected_height?, tangent_plane, free_state}}` | model | `nx_pmi_datum`, `nx_pmi_fcf` |
| `nx_edit_annotation` | Move or rename an annotation, or redefine GD&T | `annotation, position?, name?, definition?` | model | `nx_edit_annotation` |
| `nx_update_annotations` | Refresh managed measured annotations, BOMs and bend tables from the model | `objects?` (omit = all managed) | model | `nx_refresh_annotations`, `nx_update_parts_list` |
| `nx_create_drawing_table` | Parts list, bend table, title block or revision table | `kind: parts_list{sheet, position, scope: leaves/top_level/all}`, `bend{view, position, columns?, automatic}`, `title_block{sheet, rows, widths, position, row_height?}`, `revision{...same}` | model | `nx_drawing_table`, `nx_bend_table`, `nx_create_parts_list` |
| `nx_edit_drawing_table` | Edit rows, columns or update mode | `table, rows?, columns?: [{action: edit/append/remove, index?, title?, width?, field?, key?, protected?}], automatic?` | model | `nx_parts_list_column` |
| `nx_get_drawing_table` | Evaluated rows and columns | `table` | read | `nx_parts_list_info` |
| `nx_create_balloons` | Parts-list balloons in a view | `parts_list, view` | model | `nx_parts_list_balloons` |
| `nx_export_drawing` | Export sheets | `path, format: pdf (dxf/dwg reserved), sheets?` | file | `nx_export_drawing_pdf` |

Notes: the rebind mode of `nx_add_dimension` (`dimension=`) moves to
`nx_edit_dimension`; table edits of `nx_drawing_table` and `nx_bend_table` move
to `nx_edit_drawing_table`; `nx_edit_annotation(delete=true)` moves to
`nx_delete_objects`.

### 3.11 Sheet metal (`sheet_metal`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_get_sheet_metal_defaults` | Thickness, bend radius, neutral factor, reliefs, material, tool | — | read | `nx_sheet_metal_defaults` |
| `nx_set_sheet_metal_defaults` | Change part defaults | same fields, all optional | model | `nx_set_sheet_metal_defaults` |
| `nx_create_sheet_metal_feature` | Create one of 34 sheet-metal features; `parameters` follows the operation's sub-schema (`nx_get_tool_schemas(["nx_create_sheet_metal_feature#flange"])`) | `operation: tab/flange/contour_flange/.../flat_pattern, parameters` | model | `nx_sheet_metal_feature` |
| `nx_get_sheet_metal` | Bodies, actual thickness and bend parameters | `body?, Page` | read | `nx_sheet_metal_info` |
| `nx_export_flat_pattern` | Flat pattern to DXF or Trumpf GEO | `flat_pattern, path, format: dxf/geo, dxf?: {revision, mold: inner/outer}, include?: bend_up/bend_down/bend_tangent/interior_cutout/..., tolerance?` | file | `nx_export_flat_pattern` |
| `nx_create_sheet_metal_callout` | Measured thickness or bend callout (refreshed by `nx_update_annotations`) | `kind: body/bend, body, faces?, position: Point3, automatic=true` | model | `nx_sheet_metal_annotation` |

Note: editing an existing sheet-metal feature (`feature=` today) moves to
`nx_edit_feature(definition=...)`; the flat-pattern orientation edge list is
`nx_list_objects(kind=edge, owner=face, type=line)`.

### 3.12 Display (`display`)

| New tool | Purpose | Key arguments | Effect | Replaces |
|---|---|---|---|---|
| `nx_capture_view` | PNG of the current view or sheet, inline; `quality=studio` renders with lighting | `quality: viewport/studio, path?, width=1024, height=768, background: white/original/transparent/color, color?, style: current/shaded/shaded_with_edges/wireframe/studio, fit=false, lighting?` | view | `nx_screenshot`, `nx_render_view` |
| `nx_get_camera` | Displayed view, camera matrix, scale, style | — | read | `nx_view_info` |
| `nx_set_camera` | Named orientation, explicit camera, and/or fit | `orientation?: top/front/right/back/left/bottom/isometric/trimetric, rotation?: Mat3, origin?, scale?, fit=false` | view | `nx_set_camera`, `nx_set_view`, `nx_fit_view` |
| `nx_set_visibility` | Show, hide or isolate objects, or all datums | `objects?` or `datums=true`, `mode: show/hide/isolate` | view | `nx_set_visibility`, `nx_set_datum_visibility` |
| `nx_set_appearance` | Colour and transparency | `objects, color?: 1..216 or name, transparency?: 0..100` | view | `nx_set_display` |
| `nx_get_appearance` | Colour, blank state, transparency (or counts only) | `objects, count_only=false` | read | `nx_display_info` |
| `nx_set_highlight` | Replace agent highlights with these objects; empty list clears | `objects: Ref[0..1000]` | view | `nx_highlight_objects`, `nx_clear_highlights` |
| `nx_restore_display` | Restore a display snapshot (`restore_id`) or a saved presentation file | `restore_id?` or `presentation?: path` | view | `nx_restore_display`, `nx_restore_presentation` |
| `nx_save_presentation` | Save camera, section and appearance to a workspace JSON file | `path` | file | `nx_save_presentation` |
| `nx_create_section_view` | Dynamic section plane in the model view | `plane: {origin, normal}, name?, cap=true` | view | `nx_section_view` |
| `nx_edit_section_view` | Move, enable or disable a section | `section, plane?, enabled?, cap?` | view | `nx_section_control` |

Note: `nx_section_view(section=...)` edits move to `nx_edit_section_view`;
`nx_section_control(action=delete)` moves to `nx_delete_objects`.

### 3.13 Count

| Domain | Current tools mapped | New tools |
|---|---|---|
| session (incl. 3 new) | 19 | 20 |
| parts_files | 16 | 14 |
| feature tree + datums | 14 | 11 |
| sketch | 32 | 11 |
| solid modeling (incl. materials) | 27 | 20 |
| freeform | 6 | 6 |
| assembly | 22 | 17 |
| inspection (incl. `nx_list_objects`) | 35 | 17 |
| drawing | 33 | 19 |
| sheet metal | 6 | 6 |
| display | 17 | 11 |
| **Total** | **227** | **152** (149 + 3 new) |

Effects of the 152: 81 model, 40 read, 13 file, 9 session, 8 view, 1 per tool
(`nx_invoke`).

Biggest merges: sketch curves and constraints (32 → 11), inventories into
`nx_list_objects` (15 → 1), drawing views (5 → 1), drawing dimensions (4 → 1),
component patterns (3 → 1), drawing tables (3 → 1), interference/clearance/
highlight (3 → 1), camera (3 → 1).

---

## 4. Reserved families for the next wave

Each family extends an existing tool with a new enum value or variant where the
job is the same; new tools only where the job is new. A new tool or variant has
no evidence (and is refused and hidden, decision 11) until its acceptance
scenario passes on the supported build.

| Family | Tool(s) | Arguments to add | Effect | Fits as |
|---|---|---|---|---|
| Attributes | `nx_get_attributes`, `nx_set_attributes` | `objects` (part, body, component, face, feature), `names?`; set: `values: [{name, type: string/number/integer/date/boolean, value, category?}][1..100]`, `delete?: names[]` | read / model | new tools |
| Material library | `nx_list_materials`; `nx_set_material` | list: `library?, query?, Page`; set: `material: {library: name}` next to `{custom}`; `nx_get_material` adds E, Poisson ratio, thermal data | read / model | new list + variant |
| Export by format | `nx_export_model` | `format: step/step_ap242/stl/iges/parasolid/jt`, `options` sub-schema per format (`#stl`: chord tolerance, angular tolerance, binary) | file | enum values |
| Import by format | `nx_import_model` | `format: step/step_ap242/iges/parasolid/stl/dxf_dwg` (+ licensed CAD formats), `target: work_part/new_part/sketch` (DXF) | model | enum values |
| Drawing DXF/DWG | `nx_export_drawing` | `format: pdf/dxf/dwg`, `version?` | file | enum values |
| Expressions file | `nx_export_expressions`, `nx_import_expressions` | `path`; import: `mode: merge/replace`, returns changed expressions and update health | file / model | new tools |
| Feature mirror | `nx_create_mirror` | `features[1..50]` as an alternative to `bodies` | model | variant |
| Pattern layouts | `nx_create_pattern` | `layout: along_curve{curve, count, spacing}`, `polygon{axis, sides, count_per_side}`, `spiral{axis, count, pitch, angle}`, `fill{boundary, spacing, layout_type}`, `general{positions: Point3[]}`; `Direction` from edge or datum already supported | model | variants |
| Hole callouts and tables | `nx_create_drawing_dimension`, `nx_create_drawing_table` | `kind: hole_callout{hole_feature or edge}`; table `kind: hole{view, origin_point, position}` | model | variants |
| Drawing GD&T (done) | `nx_create_gdt` | `kind: view_datum_feature/view_fcf/view_datum_target` with `view, object, origin` (distinct variant names keep PMI and drafting evidence apart) | model | variant |
| Chamfer, thickness, arc-length dims, sets | `nx_create_drawing_dimension` | `kind: chamfer/thickness/arc_length`, `set: baseline/chain{objects[]}` | model | variants |
| Fits | `nx_edit_dimension` | `tolerance: {type: fit, hole?: "H7", shaft?: "g6", display: symbol/limits/both}` | model | variant |
| Title-block filling | `nx_edit_drawing_table` | `cells: [{row, column, text or attribute}]` for `title_block` | model | variant |
| More drawing views | `nx_create_drawing_view` | `type: auxiliary{parent, hinge: edge}`, `broken{view, breaks[]}`, `half_section`, `stepped_section{segments[]}`, `revolved_section`, `breakout{view, boundary, depth}` | model | variants |
| Synchronous edits | `nx_edit_faces` | `action: resize{diameter or radius}`, `resize_blend{radius}`, `resize_chamfer{offset}`, `pattern{layout}`, `mirror{plane}`, `delete_blend` | model | enum values |
| Rib, tube, helix, emboss | `nx_create_rib`, `nx_create_tube`, `nx_create_helix`, `nx_create_emboss` | rib: `sketch, body, thickness, side`; tube: `path: Ref[], outer_diameter, inner_diameter=0, combine?`; helix: `axis: Axis, diameter, pitch, turns, hand`; emboss: `sketch, faces, height, draft?` | model | new tools |
| Assembly constraint types | `nx_create_assembly_constraint` | `type: align_lock/fit/bond/center/distance_to_edge` | model | enum values |
| New component in context | `nx_create_component` | `mode: from_file/new_in_context{path, units, objects_to_move?}`; `nx_edit_component(make_unique: path)` | model | variants |
| WAVE links and extract | `nx_create_wave_link`, `nx_create_extract` | link: `source: Ref (occurrence body/face/curve/datum/sketch), associative=true, at_timestamp=false`; extract: `objects (bodies or faces), associative=true` | model | new tools |

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
