# NX MCP code architecture: catalogue, kernel, kit, domains

Status: current architecture (as built), companion to [tool-surface.md](tool-surface.md) (names,
arguments, results, errors, profiles). This document says where code lives, how one call is
processed and what agents must follow (section 5).

**Problem (before the migration).** One tool is spread over 9-12 places that must agree. About 17 name lists
(`READ_ONLY` twice, `NON_MODEL`, `PATHS`, `SIDE`, `DESCRIPTIONS`, `VIEW_ONLY`, `DOMAINS`,
guidance sets, `PAYLOADS`, panel templates, manifest and fragments) carry the same names.
`HardenedExecutor` combines 27 mixins with cross-mixin calls and MRO overrides. Helpers exist
in up to 6 copies, 82 builder `Destroy()` blocks are hand-written, there are 149+ error
codes, and live evidence is free text.

**Target.** Each tool is **one decorated function** in one domain package. Its signature is
the schema, its docstring is the description, and its decorator carries everything else,
including the engineer's panel line. A kernel applies every safety gate in one fixed order,
NX know-how lives once in `kit/`, and evidence is machine-checked acceptance output.

**Hard constraints (unchanged).**

- The sidecar imports without NX. NX-side code is stdlib only (no pydantic).
- On NX 2406 the host is the startup app `nx_app/startup/nx_mcp_startup.py`: first line
  `# nx: threaded`, ticked by a UI-thread timer, never holding NX's main loop. The
  journal `nx_app/journals/start_2406.py` (also `# nx: threaded`) plays the same
  bootstrap in an NX started without it and never starts a second bridge.
- Undo/rollback, workspace checks on both process boundaries, version and evidence gating,
  and license mapping stay at least as strong as today.
- The panel has two modes, Auto (default, also in development) and Ask me first; only the
  engineer changes the mode, no tool does, and all behaviour stays tested.

---

## 1. Package layout (`src/nx_mcp`)

```text
core/          NX-free: types, validate, catalogue, summary, errors, kernel, tx, references,
               receipts, evidence, target, switching, marks, busy, parking
kit/           shared NX helpers: geom, args, units, builders, select, sketch, drawing, explode,
               expr, readback, result, analysis_objects, bodies, capture, curve, display,
               facts, features, files, kinematics, licenses, massprops, measure, model, parts,
               pose, positioning, snapshots, surface, switching
domains/       session, parts_files, sketch, modeling, freeform, assembly, motion, inspection,
               drawing, sheet_metal, display, optimization: one package each, one @tool
               function per tool
host/          NX side: dispatch (bridge call -> kernel), services (NXServices), quiet, dialogs
sidecar/       MCP server: profiles, schema generation, proxy, gateways, artifacts, HTTP
acceptance/    scenario runner and evidence writer
evidence/      <domain>.json, written only by the acceptance runner
interactive.py NX UI-thread host: timer tick, panel, control modes, holds (activity.py: log)
nx_bridge.py   batch bridge lifecycle; bridge.py, result_transport.py: loopback protocol
workspace.py   filesystem boundary; http_surface.py optional HTTP transport
```

Import rules are enforced by an AST test:

- `core` imports nothing else from the package, and `kit` imports only `core`.
- `domains/<d>` imports only `core`, `kit` and its own package.
- Only the NX-side host modules (`host`, `interactive.py`, `nx_bridge.py`) may `import NXOpen`.
- Every other module must import in a process where importing `NXOpen` raises.

### 1.1 `core/types.py`: argument types, stdlib only

Types are `typing.Annotated` aliases and `TypedDict`s that carry small frozen dataclass
markers. `core/validate.py` checks them on the NX side. `sidecar/schema.py` turns the same
metadata into Pydantic `TypeAdapter`s and JSON Schema. A parity test asserts that both
validators accept and reject the same generated samples.

| Marker | Meaning | JSON Schema |
|---|---|---|
| `Doc(text)` | field description, ≤ 120 chars | `description` |
| `Range(ge, le, gt, lt)` | finite numeric bounds | `minimum`, `maximum`, ... |
| `Items(min, max)` | list length; required on every list | `minItems`, `maxItems` |
| `Text(max, pattern)` | string bounds | `maxLength`, `pattern` |
| `RefKind(*kinds)` | accepted reference kinds | description (gate 1 checks the pattern and length; section 4) |
| `Quantity(dim)` | `length`, `sheet_length`, `angle` or `count`; allows unit strings and NX expression names or formulas | `[number, string]` (gate 1 checks the 80-character bound) |
| `PathArg(mode, suffixes)` | workspace path; mode `read`, `new`, `dir`, `save` or `replace` (new, or an existing file the tool replaces only when asked) | `string` |
| `RefTo(*kinds, doc=None)` | shorthand for `Text(128, ref pattern)`, `RefKind(*kinds)`, `Doc` | as its parts |
| `PathTo(mode, *suffixes)` | shorthand for `Text(260)`, `PathArg`, `Doc("Workspace path")` | as its parts |
| `Ratio(per, factor)` | on a number: `"50%"` is `50 * per / 100` (`Percent`, `Fraction`); a `factor` (`Factor`) also takes `"2x"` | as the number |

`core.validate.unwrap` expands the two shorthands in place, so every reader sees the plain
markers. A reference or path is written `Annotated[str, RefTo("edge", "face")]`: type
checkers see `str`, so the aliases narrow (`x is not None`) and compare like strings.

The aliases follow surface 1.4: `Ref`, `BodyRef`, `FaceRef`, `EdgeRef`, `CurveRef`,
`FeatureRef`, `SketchRef`, `PartRef`, `ComponentRef`, `ExplosionRef`, `DimensionRef`,
`DrawingSheetRef`, `DrawingViewRef`, `DrawingTableRef`, `Point2`, `SheetPoint`, `Point3`,
`Vec3`, `Mat3`, `Pose`, `Length`, `SheetLength`, `Angle`, `Percent`, `Fraction`, `Factor`,
`Units = Literal["mm", "in"]`,
`Plane`, `Axis`, `Direction`, `SketchPoint`, `Limit`, `Combine`, `Page`, `Name`, `PathIn`.

- Unions are written with `|`: `Plane = Annotated[str, RefTo("datum_plane", "face")] |
  PrincipalPlane | PlaneByNormal`.
- A `TypedDict` argument calls `forbid_extra(...)`, which sets `__pydantic_config__ =
  {"extra": "forbid"}` as a plain dict, so no pydantic import is needed.
- Fixed vectors are `tuple[float, float, float]`, which gives `minItems == maxItems`.
- `Pose` is `{translation, rotation?}` (row-major matrix) or the axis-angle form
  `{axis: 'z' | [x, y, z], angle, origin?, translation?}`; gate 9 turns the axis-angle
  form into the matrix form (`types.CANONICAL`), so handlers see one form.

**Unit rule.**

- A plain number is in part units for `Length`, sheet units for `SheetLength`, and degrees
  for `Angle`.
- A string `"<number> <unit>"` is converted explicitly. Lengths use the pattern
  `^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\s*(mm|in)\s*$`; angles use the same with
  `deg|rad`. A wrong dimension (`"30 deg"` for a `Length`) therefore fails schema
  validation.
- A string that is not a number or unit string may be an NX expression name or formula
  (`"thickness"`, `"thickness/2"`, also for a `count`). Gate 9 evaluates it in the work part
  (`services.evaluate_formula`: a bare name is read; another formula is evaluated in a
  temporary expression, so only for `model` tools) and passes a `Formula` float that keeps
  the text, so a builder expression stays driven by it (`kit.expr.rhs`).
- Points, vectors and matrices take plain numbers only.
- The kernel converts at gate 9 (section 3), so handlers see only floats in part units and
  degrees. Results always state `units`.

**Natural spellings.** Gate 1 (`validate.py`, mirrored by the sidecar adapters) accepts what
agents commonly send and passes the canonical value on: an enum option in any case and with
spaces, `-` or `_` (`"Through All"` → `"through_all"`), a declared `Aliases` spelling or the
plural of a singular option, `"true"`/`"false"` for a boolean, a numeric string for a number,
and one string where a list of strings is expected. Also: a bare option for a one-field
form (`"-z"`, `"+Y"`, `"z axis"` for `{"principal": ...}` of an `Axis`/`Direction`, `"xy"`,
`"XY plane"`, `"top"` of a `Plane`; a ref-shaped string such as `"e:3"` stays a ref), a
JSON-array string for an array or tuple (`"[1, 2, 3]"`), `"50%"` for a percent, fraction or
factor number and `"2x"` for a factor (a `Ratio` marker, else the field's name or `Doc`:
percent, factor/scale/zoom, fraction or a 0..1 range). A union prefers an exact fit, then a
natural spelling, then a formula. Errors say what to send: the valid options with a
did-you-mean (for the variant field only the variants the NX build offers), the accepted
forms of a union, and for an unknown field the closest accepted field (`FIELD_SYNONYMS` such
as `type` → `kind`, then singular/plural and close spellings) with `details.accepted`. Every
invalid, unknown or missing field of a call is reported at once: `field`/`message` name the
first, `details.errors` lists all as `{field, message}`.

### 1.2 `core/catalogue.py`: `@tool`, `ToolSpec`, registry

`@tool(...)` builds a frozen `ToolSpec` and registers it at import. `catalogue.load()`
imports every package in `domains/`. A *derived* field is never written by hand.

| Field | Source | Content |
|---|---|---|
| `name` | function name | `nx_<verb>_<object>`, with the verb from the closed list (surface 1.2) |
| `domain` | derived from the package | `domains/modeling/...` → `modeling` |
| `effect` | decorator | `read`, `view`, `model`, `file` or `session`, fixed per tool |
| `label` | decorator | static, ≤ 48 chars. Used in discovery rows, as the undo-mark name `NX MCP: <label>`, and as the panel fallback |
| `summary` | decorator | panel line built from the arguments, ≤ 60 chars rendered (forms below) |
| `description` | docstring | first sentence ≤ 120 chars, whole text ≤ 500 (`catalogue.DESCRIPTION_MAX`) |
| `keywords` | decorator | 3-8 synonyms and NX command names for `nx_find_tools` |
| `params`, `returns` | derived from the signature | input schema; `data` schema from the returned `TypedDict` |
| `requires` | decorator | `work_part`, `no_active_sketch`, `drawing_sheet`, `assembly`, `interactive`, `application:sheet_metal` |
| `batchable`, `previewable` | decorator | admission to `nx_run_batch` and to `nx_preview_changes` |
| `paths` | derived from `PathArg` | JSON pointers with mode, nested paths included |
| `variants` | decorator | discriminator field (`"method"`) or a map to sub-schemas (`#flange`) |
| `refresh_annotations` | decorator; default true for `model` tools in geometry domains | run the managed-annotation refresh after a commit |
| `invalidates` | decorator; default `"topology"` | `"topology"`: deleted objects and the faces and edges NX no longer has (consumed) go stale. `"deleted"`: only deleted objects (datums, annotations, display) |
| `cost` | decorator | `light` (default), `heavy` or `long` (3.3) |
| `runs` | decorator; default `"nx"` | `"sidecar"` for workspace and result tools that never reach NX |
| `example` | decorator | one minimal argument set, returned by `nx_get_tool_schemas` |
| `required_by` | decorator | `{variant: (fields...)}` a variant needs (keys are values of the variant field, fields optional parameters; checked at import): gate 1 refuses the call without them. `nx_get_tool_schemas` adds a compact `required_by` map next to `input_schema` (`sidecar/schema.required_by`): one key per group of offered variants needing the same fields, `|`-joined (`{"hole": ["view", "origin"], "title_block\|revision": ["sheet", "rows", "widths"]}`), variants needing nothing left out; a `tool#variant` schema lists the fields in `required` |
| `variant_from` | decorator | `field` or `(field, resolver)`: the variant field may be omitted; gate 3 takes it from the object the ref `field` names (`resolver(tx, obj)`, else the ref's kind), e.g. `nx_edit_drawing_table` without `kind` |
| `target` | decorator; default from `runs`/`requires`/`effect`/`domain` (`catalogue.default_target`) | gate 3t policy: `refs` (the references' part; default), `assembly` (components, constraints, patterns, explosions and trace lines decide; occurrences pin the assembly), `sheet` (sheets, views, dimensions, annotations, tables decide), `display`, `any` (an NX-global read), `creates`, `none` (no part: sidecar tools and tools that do not require `work_part`), `arg:<param>` (the tool's own part argument: `nx_activate_part`, `nx_close_part`, `nx_save_part`) or a resolver callable `(services, args) -> part or None` (stored as `resolver` + `target_resolver`: `nx_finish_preview`, `nx_restore_checkpoint`, `nx_redo_operation`, `nx_undo_operation`; a resolver returns `target.ANY` when its object is gone, so the handler says what happened instead of `NX_AMBIGUOUS_PART`). Explicit rows only where the default is wrong (inspection measurements, attributes, exports, ...) |
| `switch` | decorator; default from `target`/`effect`/`requires`/`domain` (`catalogue.default_switch`) | how gate 4s makes the target current: `none` (reads, `none`/`creates`/`arg:`/`any` targets: bound, never switched), `work` (the target becomes the work part; `nx_get_sketch`/`nx_check_sketch` activate a sketch), `display` (`view` effects, the `display` and `drawing` domains, `interactive`, `application:sheet_metal`, explosions, WAVE links, model exports, `nx_update_assembly`) |
| `display_bound` | decorator; default `switch == "display"` | `True` (implies `switch="display"`), or the variant values and `/<param>[/<key>]` argument pointers that need the target displayed when given (`nx_create_reference_set` `/component`: `working_in`; `nx_create_sketch` `/placement/path`); `spec.switch_for(args)` |
| `handler` | the decorated function | called as `handler(tx, **converted_args)` |
| `evidence` | loaded from `evidence/<domain>.json` | tested variants per NX build |

**Summary forms.** A summary is a format string (`"Blend {edges} R{radius}"`), a dict keyed by
variant value, or a callable `(args, say) -> str`. `core/summary.py` renders placeholders by
type, never raises, and falls back to `label` on failure:

- a `Ref` becomes its NX name in quotes (`"profile"`);
- a list becomes a count (`3 edges`);
- a `Length` gets its unit (`10 mm`);
- a path is shown workspace-relative.

**Cross-domain hooks.** Generic tools stay in one domain; each domain registers handlers for
its own kinds:

- `@lister(kind)` for `nx_list_objects`;
- `@deleter(kind)` for `nx_delete_objects`;
- `@definition(feature_type)` for `nx_edit_feature(definition=...)`;
- `@refresher(kind)` for `nx_update_annotations` and for the kernel's refresh (gate 13):
  `fn(tx, objects or None) -> [rows refreshed]`. Registered: `drawing_view` (updates
  out-of-date views, re-maps detail boundaries), `drawing_table`, `dimension` (hole
  callouts), `annotation` (sheet-metal callouts) and `assembly_constraint`, a post-call
  check that refreshes nothing: it reads the solver status of the work part's constraints
  and of those of loaded assemblies that position it, and warns (`NX_UPDATE_WARNING`, once
  per status change) about those that no longer solve;
- `@sheet_locator(kind)` for gate 9: `fn(services, obj) -> sheet or None`, the drawing
  sheet an annotation, dimension or table lies on.

**Import-time validation.** A violation raises `CatalogueError`: the sidecar refuses to
start, and `dev_reload.py` prints the error. The checks:

- name, verb and uniqueness, and the budgets in surface 1.7;
- known `effect` and `requires` values;
- `tx` first, keyword-only parameters built from core types, `Items` on every list, and
  defaults that validate;
- summary placeholders that name real parameters, and `variants` naming a `Literal` field;
- `batchable` implies `model` with `cost` ≤ `light`; `previewable` implies `model`;
- no `new`, `save` or `replace` path on a `read` tool;
- a known `target` (an `arg:` target names a parameter, a sidecar tool has none), a known
  `switch`, `display_bound=True` only with `switch="display"`, tuples naming variant
  values or `/<param>` pointers, and no switch for `none`/`creates`/`arg:` targets;
- known hook kinds.

### 1.3 Other `core/` modules

| Module | Responsibility |
|---|---|
| `errors.py` | Table of 22 categories and 72 codes (surface 1.8), each with `retry` and a default `hint`. `fail(code, message, *, field=None, hint=None, details=None, nx_code=None, retry=None)` raises `NXError` (a subclass of `NXToolError`). `native(codes=None, default="NX_ERROR")` context manager (below) |
| `kernel.py` | `run(spec, args, ctx)` and `run_child(spec, args, parent_tx)`, the gate order of section 3. Nothing else applies gates |
| `target.py` | Gate 3t (pure): the call keys (`split_call_keys`, `check_call_keys`, the ghost guard `check_expiry`), `part_from_text`, `agent_parts`, the two-parts note (`census`, `load_note`), `roots`, `resolve` and `refuse_other_part`; for `target` resolvers of tools that carry refs inside other arguments, `part_named_by` (the part a ref or anchor string pins, as gate 3t pins a ref argument) and `part_at_path` (3.4) |
| `switching.py` | Gate 4s: `enter(...)` returns the call's `Bound` (`check`, `defers`, `leave`), `repair`, `deferred`; drives `kit.switching.Switch` through `Services.switch` (3.4) |
| `marks.py` | NX undo marks around a call: `watch` before a non-read call's mark, `settle` after it; load, close and engineer barriers (`barrier`, `close_barrier`, `engineer_change`), the reasons of dropped marks, `remove_entries`. Never raises (3.4) |
| `busy.py` | Part ownership between callers: `record` after a committed changing call (the parts it worked in, the caller of the parts it loaded), `adopt` (agent parts after a host reload), `refuse_engineer` (`NX_ENGINEER_PART`), `refuse_close`/`refuse_activate` (`NX_PART_BUSY`), `loaded_at`, `component_reason`; table in `Services.part_activity()` (3.4) |
| `parking.py` | Parked sketches: `park`/`unpark`/`parked`/`open_in`, table in `Services.parked_sketches()` (3.4) |
| `tx.py` | `Tx`, the handler's only route to NX and to the services (3.2) |
| `references.py` | short refs, kinds, anchors, invalidation |
| `receipts.py` | Receipts (successor of `recovery.OperationStore`): fingerprint (tool, arguments and the target part's file key `part_file`; receipts written before targets match without it), idempotent replay, states, outcome, progress, `.cancel` flag. A receipt keeps the request's `arguments`, redacted (`redact`: file contents and long strings become size notes, lists keep 100 items, at most 8,000 characters), which `nx_get_operation` echoes, and a development failure's `traceback`. A running receipt also has an empty `running/<operation_id>` marker (written before it, removed when it finishes), so start-up `recover()` reads only the in-flight operations and turns those of an earlier session into `unknown`; it then starts a background pruner (older than 7 days, beyond the 20,000 newest; never a running one; `RETENTION_DAYS`, `MAX_RECEIPTS`). A committed call an undo or a checkpoint restore reverted (`reverted_by`) runs again under its id instead of replaying; the new receipt keeps the reverted run under `previous` (`previous_run`: start and finish, what reverted it, the refs it created, up to 5 earlier reverted runs). The undo history, checkpoints and redo list live in the services (`undo_history`, `undo_checkpoints`, `redo_steps`, 4) |
| `evidence.py` | Loads `evidence/*.json`. `available(spec, build, variant)`, `known_build(build)`, discovery filter, writer API for the runner |

**`native()` mapping.**

- `ErrorCode` 948801 or 949895 → `unavailable/NX_LICENSE_UNAVAILABLE`.
- A code listed in `codes` → its stable code.
- Anything else → `nx_error/NX_ERROR`.
- `nx_code` and the native message are always kept.
- A Python exception → `NX_ERROR` with its type name. Its traceback never enters
  `error.details`: with `NX_MCP_DEV=1` the kernel writes it to the operation receipt
  (`traceback`) and `.nx-mcp/logs/tracebacks.log`.

**References.** A ref is `<prefix>:<n>` (`p:1` part, `b:41` body, `f:12` feature, `s:3`
sketch, `fc:88` face, `e:90` edge, `d:7` datum, `o:5` component).

- The prefix is a mnemonic; the registry stores the exact kind.
- `n` comes from one workspace-wide counter that is never reused. Its floor is persisted in
  `.nx-mcp/refs.json` at host start, so a ref from an earlier session or reload never
  aliases another object. A ref this session never issued is refused by
  `references._unknown`: an unknown prefix is `invalid_argument`; a number at or above the
  floor that no tool returned is `not_found/NX_NOT_FOUND` (`details.reason: never_issued`,
  plus `issued_as` when the number is a ref of another kind); a number below the floor is
  `stale_reference` (`reason: not_in_this_session`: refs are not persisted, so an earlier
  session's ref and an invented one look the same).
- Features, bodies and sketches carry an automatic `anchor` (`"@smoke/a/blk.prt:EXTRUDE(4)"`)
  built from the persistent journal identifier, plus a body index for bodies. Faces and
  edges get an anchor only on request (decision 5).
- A `stale_reference` error carries the anchor's unique current match in `details.current`.
  The kernel never re-resolves silently, except the re-binding after an undo, a rollback or
  an engineer's edit (below).
- `references.py` wraps the host's `ObjectRegistry`, so the `obj_...` and short spellings of a
  ref both resolve.
- Refs follow NX's persistent object identity (the tag), not geometry: a face or edge a
  feature reshaped but did not consume keeps its ref.

**Invalidation.**

- After every commit, only refs of objects NX no longer has go stale: deleted objects and
  consumed faces and edges. `changes` lists them (`deleted`, `stale_refs`) with
  `references: listed`; with nothing stale it is `preserved`. Only services that cannot say
  which faces and edges died (`retired is None`) retire every face and edge ref of a
  `topology` call and report `stale_kinds: ["face", "edge"]`. A deleted object that never
  had a ref is a row without `id` and with `gone: true`; unnamed internal NX objects (sketch
  helper curves, a pattern's own constraints) are only counted in `changes.internal`.
- After an undo or a rollback, `kernel.rebind_after_undo` keeps the refs of every object NX
  restored (NX 2406 undo restores the same tags), re-binds an anchored ref whose object came
  back under a new tag, and retires the rest; part refs of parts still open always stay. A
  rollback that left refs stale reports `part_invalidated` with the error's
  `details.stale_refs`; `nx_undo_operation` and `nx_restore_checkpoint` call
  `tx.after_undo()` and report `preserved` or `listed`.
- An edit the agent did not make (the engineer's) re-validates refs the same way
  (`NXServices.revalidate`) instead of dropping them, appends an `engineer` barrier to the
  undo history (phase P3) and expires the checkpoints (`expired_reason: engineer_change`).
- Part close and host reload invalidate the refs in their scope; a closed part's
  checkpoints stay listed as expired (`part_closed`).
- **Undo marks, undo per part, closes and parked sketches.** NX keeps one undo list for
  the session; NX MCP loads and switches the display without dropping its marks, reads
  the history per part and keeps no sketch active between calls (3.4).

### 1.4 `kit/`: shared NX know-how, never copied

| Module | API (`tx` first wherever NX is touched) |
|---|---|
| `geom` | pure math: `add`, `sub`, `dot`, `cross`, `norm`, `unit(v, field)`, `tol(*pts)`, `parallel`, `same_direction`, `on_plane`, `plane_through(pts, field)`, `orthonormal_frame`, `matvec`, `matmul`, `transpose`, `xyz(nx_point)`, `rows(nx_matrix)` |
| `args` | cross-field rules the schema cannot express: `need(**fields)`, `exactly_one(**fields)`, `distinct(field, values)`. Each raises `invalid_argument` with a JSON-pointer `field` |
| `units` | `part_units(tx)` → `"mm"`/`"in"`, `sheet_units(tx, sheet)`, `factor(source, target)`, `say(value, units)` → `"10 mm"`, `normalize` (`"inch"`/`"inches"` → `"in"`; `"deg"`, `"rad"` kept). The spelling `"inch"` never leaves it |
| `builders` | `with builders.build(tx, factory, *a) as b:` then `b.commit()` (`CommitFeature` or `Commit`). `Destroy()` always runs, and rejections pass through `native(default="NX_BUILD_FAILED")`. Also `point(tx, p)`, `direction(tx, v, origin)`, `collector(tx, refs, "Edge"/"Face"/"Body")` |
| `select` | `planar(tx, ref)` → `(obj, origin, normal)` for a planar face or datum plane of the work part. `plane`, `axis`, `direction(tx, value)` resolve the union types. `bodies`, `faces`, `edges(tx, refs)`, `owned_by_work_part(tx, obj)` |
| `sketch` | `with sketch.editing(tx, ref) as sk:` activates the sketch and restores the previous state, with `sk.frame`, `sk.to_part(p2)` and `sk.to_local(p3)`. `sketch.solve(tx, sk)` returns status and DOF, or raises `solver_failed`. `sketch.owns(tx, sk, curve)` is the one ownership check. `release_drafting_sketch(session, nx)` deactivates an active drafting (sheet) sketch for gate 4 |
| `drawing` | `with drawing.on_sheet(tx, sheet) as sh:` activates the sheet and restores the previous sheet and display afterwards (it does not switch or restore the NX application; open). `sh.units`, `sh.point(SheetPoint)`, `drawing.view(tx, ref)`, `drawing.refresh(tx, views)`. `visible_edges(tx, view)` and `edge_in_view` give the model edges a view draws in sheet coordinates; `annotation_box` (an annotation's sheet bounds) and `crowding` (views it overlaps, off-sheet). `out_of_date(views)` (views NX flags "(Out of Date)") and `update_out_of_date(tx, views)` (updates and returns them; `NX_UPDATE_FAILED` when NX still flags any) |
| `explode` | `shown(tx)`: the explosion shown in the work view of the displayed work assembly, else None; `assembled_note(tx)`: the note that NX measures assembled (unexploded) poses while one is shown |
| `expr` | `create(tx, name, formula, units)`, `set_formula`, `value`, `alias(feature, "distance")` (semantic parameter names), `bind` |
| `readback` | `expect(ok, check, **actual)`, `expect_close(actual, wanted, check, tol=None)` (scalar or vector, scaled tolerance), `expect_same_direction`, `expect_on_plane`. Each raises `verification_failed` with `details.check` |
| `result` | `rows(tx, objs, kind)`, `page(rows, page)`, `measured(value, "length")`, `rename(tx, obj, name)` (returns the name as NX normalised it) |
| `analysis_objects` | NX analysis display objects (reflection, slope, distance, radius, combs, continuity): `require_displayed` (made only in the displayed part: NX 2406 keeps one committed in a background part registered after that part closes, and the next analysis display then demand-loads the closed file behind a modal dialog), `remember` (per part), `of_part`, `delete_all(tx, part)` (before a part closes) |
| `bodies` | Which bodies a call covers: `of(tx, obj)` (body, feature, component occurrence bodies), `scope(tx, "auto")`, `occurrences(component)`, `solids(found, field)`, `measurable(found, scoped)` (faceless bodies and, in a scope, topology optimization results are skipped). Body health after a build: `faulty`/`expect_sound` (UF consistency faults), `face_clashes`/`clashing_faces`/`clashing`/`expect_no_clash` (faces cutting other faces of the body, which UF consistency passes: a tube wider than its helix pitch), each `NX_BUILD_FAILED` with rollback; `scale_note` (bodies over 10 m) |
| `capture` | `studio_png(tx, path, w, h, style=...)` (Studio Image Capture at the exact pixel size) and `viewport_png(tx, path, w, h)` (rasterises the graphics window at its own size; returns the actual size) |
| `curve` | UF `Eval` on curves and edges: `evaluator(tx, curve)` (an `Evaluator`: `at`, `closest`, `fraction`), `ends`, `point_at`, `closest` (foot and unit tangent), `along` (pieces sorted from the source's start), `min_distance`, `point_distance` |
| `display` | Display state of the displayed part: `shown_part`, `model_view`, `camera`/`set_camera`, styles, appearance, datums, dynamic sections, and the `Snapshots` behind `restore_id` |
| `facts` | `edge(tx, edge)` (line/arc data), `face_point(tx, face)` (a point inside a trimmed face), `on_object` |
| `features` | The feature timeline as the Part Navigator shows it (`timeline`, `internal`) and each feature's state (`states`) |
| `files` | Workspace files: `metadata(path, root)` (`{path, size, sha256}`), `write_new`, `temp_path`, `png_size` |
| `kinematics` | Assembly motion shared by assembly and motion: `pose(component)`, `place`, `drive(tx, constraint, value)` (solve a driven constraint), `gap` (minimum distance and witness points), `carry`, `angle_about` |
| `licenses` | `served(tx, features)`, `require(tx, features, message, strict=, by_argument=)`: `LicenseManager.CheckPresence` (a query: no checkout, no dialog) before work whose missing license would open NX's blocking "License Not Available" dialog; refuses with `NX_LICENSE_UNAVAILABLE` |
| `massprops` | Weight and centre-of-gravity bookkeeping: `entry`, `combine`, reference `frame`s (LCG/TCG/VCG), NX weight-management assertions (`asserted`, `assert_weight`, `clear_weight`) and component `groups` |
| `measure` | `mass`, `volume`, `bounds`, `area`, `length`, `inertia` in work-part units; `wcs` is the displayed part's WCS (UF reports in it; NX refuses `WCS` of a part that is not displayed). On a quiet work part beside a displayed part of other units NX 2406 slips readings by the inch factor: `area` corrects it, and each `length` reading is cross-checked against the curve's sampled chord length |
| `model` | `update(tx, mark=None)`: the NX model update inside the call's undo mark (errors → `NX_UPDATE_FAILED`) |
| `switching` | `Switch(tx, part, mode, application=None).enter()` / `.restore()`: gate 4s's NX primitives. `work`: `Parts.SetWork` for the display part or a component prototype under it, else `uf.Assem.SetWorkPartQuietly` (any loaded part, no UI change, about 0 ms); `display`: `parts.activate` (undo marks kept, no Sheet Metal dialog) and the application the tool needs (`sheet_metal`). `restore` puts back the display part, the work part (through the previous work component when there was one) and the application (not Modeling for a shown part with sheet-metal features: NX's blocking warning); on failure it forces the home state (`SetWorkPartQuietly(display)` + `SetWork(display)`) and returns the warning text instead of raising. `clean`, `retry()`, `primitive`, `timings` |
| `parts` | Loaded parts: `loaded(tx, path)`, `check_new_name` (NX names a part by its file basename), `show(nx, parts, part)` (the one display switch, keeping NX's undo marks), `activate` (through `show`; a sheet-metal part from no display part through a scratch part, without NX's dialog), `components`, `load_components`, `dependents`. `with parts.saving(tx, part) as updated:` keeps a part's drawing preview data through a save (opens a sheet, restores sheet, display and work parts) and first updates the views NX flags out of date (`drawing.update_out_of_date`), yielding them for `updated_views` (`nx_save_part`; `nx_export_drawing` passes `update_views=False` and updates its own sheets) |
| `pose` | Rigid poses `(t, R)`: `compose`, `inverse`, `relative`, `move_args` (NX component moves rotate about the assembly origin), `axis_rotation`, `interpolate` |
| `positioning` | Assembly constraint state shared by assembly and inspection: `all_constraints(part)`, `solved`, `status_name`, `kind_name`, `components`, `uses(constraint, prototype)`, and `unsolved(tx, part)` → `[{constraint, type, status, severity, components}]` (CannotSolve, OverConstrained, Inconsistent, Conflicting are errors, other statuses warnings) for `nx_check_model` and the post-call constraint check (3.1 gate 13) |
| `snapshots` | Retention of stored result snapshots (never receipts); a deleted snapshot leaves a tombstone, so `nx_get_result` tells expired from unknown |
| `surface` | Surface differential geometry and sampled edge continuity (G0/G1/G2) |

### 1.5 `domains/<domain>/`

There is one package per domain. A module holds a few `@tool` functions and private helpers
used only in that domain; anything a second domain needs moves to `kit/`. Domain code never
imports NXOpen: it uses `tx.nx`, `tx.uf`, `tx.session` and `tx.part`. That lets the sidecar
import the same modules to read the specs.

Example `modeling/` layout: `datums.py`, `features.py` (extrude, revolve, sweep, loft),
`holes.py`, `details.py` (blend, chamfer, shell, draft), `bodies.py`, `tree.py` (feature tree
and expressions), `material.py`, `hooks.py`.

### 1.6 Runtime packages

| Package | Modules |
|---|---|
| `host/` (NX) | `dispatch.py`: every bridge call goes to the kernel (`execute`), panel summaries come from `spec`, the part grant (`grant_state`/`set_grant`). `services.py`: `NXServices` (work part, snapshots, undo marks, receipts, object registry, ownership, update-error cleanup) |
| NX hosts | `interactive.py`: UI-thread host (timer tick, Win32 panel with the two modes (Auto, Ask me first) and the "Allow agent to change <part>" grant, holds); `activity.py`: panel history and `.nx-mcp/activity.log`; `nx_bridge.py`: batch lifecycle. Bootstraps: `nx_app/startup/nx_mcp_startup.py` (2406), `nx_app/journals/start_2606.py`, `nx_app/journals/start_batch.py`. The 2406 bootstrap imports the NXOpen namespaces the handlers reach through `tx.nx` once at start (`interactive.NXOPEN_NAMESPACES`, `PartFamily` and `DesignSimulation` included): a namespace is an attribute of `NXOpen` only after its import |
| Bridge | `bridge.py`: descriptor, authenticated loopback server and client (protocol, token and the 1 MiB limit); `result_transport.py`: bounded receipts with pageable full results |
| `sidecar/` | `server.py`: `create_server()`/`install()`, profile from `NX_MCP_SURFACE`. `agent` (default) lists 4 gateways + 14 core tools; `full` lists the gateways and every visible catalogue tool, with `readOnlyHint`/`destructiveHint` from `effect`. `schema.py`: core types -> Pydantic and JSON Schema. `proxy.py`: validates, resolves paths and calls the bridge. `local.py`: `runs="sidecar"` tools. `gateways.py`: `nx_find_tools` (ranked, filtered by evidence for the descriptor's build), `nx_get_tool_schemas` (several variants of one tool in one call come as one entry: the shared schema once plus each variant's own fields, surface 2.1), `nx_invoke`, `nx_invoke_read`. `artifacts.py`: result snapshots and the `nx-artifact://` resource. `http_surface.py` (top level): optional HTTP transport |
| `acceptance/` | `runner.py` replays `acceptance/<domain>/*.json` through a real stdio MCP client (the production path). Fixtures in `acceptance/fixtures/` are saved in NX 2406 and copied to `smoke/<agent>/`. Every `expect` is machine-checked. A passing run writes `src/nx_mcp/evidence/<domain>.json` for the connected build |

---

## 2. Worked example: `nx_create_datum_plane`

Before the migration this tool was spread over `modeling_server.nx_datum_plane`,
`DatumFeaturesMixin._datum_plane`, a `request_summary` template, the manifest and copied vector
helpers. Now it is one function, one test and one scenario. The `flip` argument was dropped: a
negative distance goes the other way.

```python
# src/nx_mcp/domains/modeling/datums.py
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Length, Name, Point3, RefTo, Vec3
from nx_mcp.kit import args, builders, geom, readback, result, select


class DatumPlaneData(TypedDict):
    feature: result.Row
    datum_plane: result.Row
    origin: Point3
    normal: Vec3


@tool(
    effect="model",
    label="Create datum plane",
    summary={"offset": "Datum plane {distance} from {reference}",
             "point_normal": "Datum plane through {origin}",
             "three_points": "Datum plane through 3 points"},
    keywords=("datum plane", "reference plane", "offset plane", "construction plane", "work plane"),
    requires=("work_part", "no_active_sketch"),
    variants="method", cost="light", previewable=True, invalidates="deleted",
    example={"method": "offset", "reference": "fc:7", "distance": "15 mm"},
)
def nx_create_datum_plane(
    tx: Tx, *,
    method: Literal["offset", "point_normal", "three_points"],
    reference: Annotated[str, RefTo("datum_plane", "face")] | None = None,
    distance: Length | None = None,
    origin: Point3 | None = None,
    normal: Vec3 | None = None,
    points: Annotated[list[Point3], Items(3, 3)] | None = None,
    name: Name | None = None,
) -> DatumPlaneData:
    """Create a datum plane offset from a planar face or datum plane, through a point with a
    normal, or through three points. Offset planes follow the reference; a negative distance
    goes against its normal."""
    # 1. Pure checks first: failing here is "not_started" (tx untouched).
    if method == "offset":
        args.need(reference=reference, distance=distance)
    elif method == "point_normal":
        args.need(origin=origin, normal=normal)
        want = geom.unit(normal, field="/normal")
    else:
        args.need(points=points)
        want = geom.plane_through(points, field="/points")       # rejects collinear points
    # 2. NX work; distance is already a float in part units.
    if method == "offset":
        target, ref_origin, want = select.planar(tx, reference)   # planar + work-part owned
        geometry, nx_method = [target], "Distance"
    elif method == "point_normal":
        geometry = [builders.point(tx, origin), builders.direction(tx, want, origin)]
        nx_method = "PointDir"
    else:
        geometry, nx_method = [builders.point(tx, p) for p in points], "ThreePoints"
    with builders.build(tx, tx.part.Features.CreateDatumPlaneBuilder,
                        tx.nx.Features.Feature.Null) as b:
        plane = b.GetPlane()
        plane.SetUpdateOption(tx.nx.SmartObject.UpdateOption.WithinModeling)
        plane.SetMethod(getattr(tx.nx.PlaneTypes.MethodType, nx_method))
        plane.SetGeometry(geometry)
        if method == "offset":
            plane.Expression.RightHandSide = repr(distance)
        plane.Evaluate()
        feature = b.commit()
    # 3. Read-back: a mismatch raises verification_failed; the kernel rolls back.
    datum = feature.DatumPlane
    got_origin, got_normal = geom.xyz(datum.Origin), geom.unit(geom.xyz(datum.Normal))
    readback.expect_same_direction(got_normal, want, check="normal")
    if method == "offset":
        gap = geom.dot(geom.sub(got_origin, ref_origin), want)
        readback.expect_close(gap, distance, check="offset")
    for i, p in enumerate([origin] if method == "point_normal" else points or []):
        readback.expect_on_plane(p, got_origin, got_normal, check=f"on_plane[{i}]")
    if name:
        result.rename(tx, feature, name)
    return {"feature": tx.ref(feature), "datum_plane": tx.ref(datum, kind="datum_plane"),
            "origin": got_origin, "normal": got_normal}
```

The handler holds no undo, receipt, path, unit, ownership, panel or envelope code: the kernel
supplies all of it. The panel shows `Datum plane 15 mm from "Face of EXTRUDE(2)"`.

**Evidence** in `src/nx_mcp/evidence/modeling.json`, written by the runner. A build key being
present means the tool is tested on that build (decision 11).

```json
"nx_create_datum_plane": {
  "v2406": {"variants": ["offset", "point_normal", "three_points"],
            "scenario": "acceptance/modeling/datum_plane.json", "scenario_sha256": "9c1e…",
            "source_sha256": "41ab…", "checks": 11, "recorded": "2026-10-14T09:12:00Z"}
}
```

**Unit test** in `tests/unit/modeling/test_datums.py`, on the shared fake kit. It covers unit
conversion, `Destroy()`, rollback on a read-back mismatch, and the outcome.

```python
from tests.kit import fake_nx, run

def test_offset_converts_units_and_rolls_back_on_readback_mismatch():
    nx = fake_nx(units="in")
    top = nx.block(2, 1, 1).face(normal=(0, 0, 1))                       # top face at z = 1 in
    nx.on_commit("DatumPlaneBuilder", lambda b: nx.datum_plane(origin=(0, 0, 2), normal=(0, 0, 1)))
    ok = run(nx, "nx_create_datum_plane", method="offset", reference=top.ref, distance="25.4 mm")
    assert ok.mutation_outcome == "committed" and ok.units["length"] == "in"
    assert nx.last("DatumPlaneBuilder").plane.Expression.RightHandSide == "1.0"
    assert nx.live_builders == 0 and ok.changes["references"] == "preserved"

    nx.on_commit("DatumPlaneBuilder", lambda b: nx.datum_plane(origin=(0, 0, 3), normal=(0, 0, 1)))
    bad = run(nx, "nx_create_datum_plane", method="offset", reference=top.ref, distance=1)
    assert bad.error["category"] == "verification_failed" and bad.error["details"]["check"] == "offset"
    assert bad.mutation_outcome == "rolled_back" and nx.undo_marks == [] and nx.live_builders == 0
```

**Acceptance scenario** in `acceptance/modeling/datum_plane.json`. Only steps with an `expect`
count as checks. `covers` names the variants that are credited when every check passes.

```json
{
  "covers": {"nx_create_datum_plane": ["offset", "point_normal", "three_points"]},
  "fixture": "fixtures/block_40x20x10_mm.prt",
  "steps": [
    {"call": "nx_open_part", "args": {"path": "$fixture"}},
    {"call": "nx_find_geometry", "save": "top", "args": {"kind": "face", "geometry_type": "plane",
      "normal": [0, 0, 1], "order": "highest", "axis": "z", "limit": 1}, "expect": {"data.items": {"count": 1}}},
    {"call": "nx_create_datum_plane", "save": "dp1",
     "args": {"method": "offset", "reference": "$top.data.items.0.id", "distance": "15 mm"},
     "expect": {"mutation_outcome": "committed", "data.origin.2": {"close": 25, "tol": 1e-6},
                "data.normal": {"close": [0, 0, 1], "tol": 1e-9}, "changes.created": {"count_kind": {"feature": 1}}}},
    {"call": "nx_create_datum_plane", "args": {"method": "offset", "reference": "$dp1.data.datum_plane.id",
      "distance": -40}, "expect": {"data.origin.2": {"close": -15, "tol": 1e-6}}},
    {"call": "nx_create_datum_plane", "args": {"method": "point_normal", "origin": [5, 5, 5], "normal": [1, 1, 0]},
     "expect": {"data.normal": {"close": [0.7071068, 0.7071068, 0], "tol": 1e-6}}},
    {"call": "nx_create_datum_plane", "args": {"method": "three_points", "points": [[0, 0, 5], [0, 10, 5], [10, 0, 5]]},
     "expect": {"data.normal": {"close": [0, 0, -1], "tol": 1e-9}, "data.origin.2": {"close": 5, "tol": 1e-6}}},
    {"call": "nx_create_datum_plane", "args": {"method": "three_points", "points": [[0, 0, 0], [1, 1, 1], [2, 2, 2]]},
     "expect": {"error.category": "invalid_argument", "error.field": "/points", "mutation_outcome": "not_started"}},
    {"call": "nx_check_model", "args": {}, "expect": {"data.errors": {"count": 0}}}
  ]
}
```

The `expect` operators are `equals`, `close` (with `tol`), `count`, `count_kind`, `contains` and
`matches`. An `error.*` path asserts a failure. A `"setup": true` step prepares state and is
not counted as a check. `$label.path` (a saved step's result) and `$part`/`$fixture`/`$run`
are substituted in `args` and in `expect` values (`{"equals": "$link.data.part"}`); in
`expect` only `$name...` strings are, so other text starting with `$` (`$~C`) stays as
written, and `$$` is a literal `$`.

---

## 3. The kernel: one gate order for every call

### 3.1 Gate order

**S** marks a gate that runs in the sidecar and **N** one that runs on the NX side. **C** marks a
gate repeated for every child of `nx_run_batch` and `nx_preview_changes`. A failure before gate 7
always reports `not_started`.

| # | Gate | Where | Rule |
|---|---|---|---|
| 1 | Schema validation | S (Pydantic), N (`validate.py`) · C | Rejects an unknown tool, extra fields (with a did-you-mean), out-of-bounds values and unit strings of the wrong dimension; natural spellings pass as their canonical value (1.1); every invalid field is reported at once (`details.errors`), and a variant's `required_by` fields are required. The sidecar checks batch children against the child spec, which must be `batchable` or `previewable`. `"$2.data.feature"` placeholders are allowed only in Ref fields |
| 2 | Workspace paths | S (`Workspace.resolve`), N (`ensure_inside` + mode) · C | Checked from `spec.paths`. A path without a suffix gets the marker's first one (`parts/plate` → `parts/plate.prt`). `.nx-mcp` is reserved; `new` must not exist, `read` must exist, `save` may write only the target part's own file (gate 3t's part, not the work part), and `replace` may be an existing file (never a directory) that the tool replaces atomically only when asked (`Files.stage(path, replace=True)`). An existing `new` or `save` path fails `NX_ALREADY_EXISTS` with `details.suggested_path` (the first free `<stem>-<n>`). In a ref\|path union a ref-shaped value (`p:3`) is a ref and is not resolved as a path (`proxy._takes_refs`) |
| 3 | Variant inference, evidence/version | S (build from the descriptor), N · C | N first fills an omitted variant of a `variant_from` tool from the referenced object and checks that variant's `required_by` fields. The tool and the value of its variant field must be tested on the connected build. On an unknown build only `read` and `view` tools run. A refusal is `unavailable/NX_VERSION_UNSUPPORTED` and names the variants available here. Listings show only what runs: untested variants leave the schema enum, `tool#variant` schemas and `nx_find_tools`, and a tool with no variant left is hidden; before NX connects listings assume the default build (v2406) and say so, and a change sends `tools/list_changed`. `NX_MCP_DEV=1` still offers untested variants, marked. A tested tool/variant that failed with `NX_LICENSE_UNAVAILABLE` is remembered for the NX session: refused before NX ("remembered from <time>") and hidden via `nx_get_status` `bridge.license_unavailable` until NX restarts |
| 3t | Target part | N (pure, `core/target.py`) | The call keys `part` and `expires_at` are taken out of `arguments` before gate 1 (unless they are the tool's own parameters) and validated; the ghost guard refuses a changing call whose `expires_at` passed (`NX_CALL_EXPIRED`, not started, no receipt). Then rule G names the part the call acts on: the tool's own part argument, a resolver, the refs' part (`part` must agree: `NX_WRONG_PART_CONTEXT`), the `part` key, the parent's part, else the work part while at most one of the agent's parts is loaded and `NX_AMBIGUOUS_PART` with two or more; `envelope.part` reports it (3.4) |
| 4s | Target switch | N | `core/switching.py` + `kit/switching.py`: the kernel makes the target current by `spec.switch_for(args)` (`none`: a read is bound to it; `work`: the target becomes the work part, quietly when NX allows no `SetWork`; `display`: it is displayed and the application entered), checks after the handler that the work part is still the target, and restores the engineer's display, work part and application at the end of the call, committed or not. No switch and no NX call when the target is the work part. A refused switch is `NX_WRONG_PART_CONTEXT`, not started; a refused restore is an `NX_UPDATE_WARNING` (3.4) |
| 4 | `requires` | N · C | Read-only state checks (`NX_NO_WORK_PART`, `NX_SKETCH_ACTIVE`, `NX_WRONG_APPLICATION`, ...) whose hint names the tool to run. One exception changes state: `no_active_sketch` releases an active drafting (sheet) sketch, which NX re-activates on opening a sheet, instead of refusing (`services.release_drafting_sketch` → `kit.sketch.release_drafting_sketch`: deactivated without a view change); a model sketch, or the target part's parked one, still refuses. They run before the hold, so the engineer is never asked to approve a request that cannot succeed, and again after it |
| 5 | Engineer control hold | N (host) | Auto: each call takes NX's main window for itself only (no `UI.LockAccess`) and gives it back; reads never wait, other calls wait silently while the engineer is in an NX command or dialog (at most 120 s, then `NX_UI_BUSY`), and a non-read call is refused with `NX_MODAL_DIALOG_OPEN` (not started) while a modal NX dialog is open. No request card in Auto, long steps included. Ask me first: every call waits on the panel card (`spec.summary` and the agent's `intent`). The card shows one call, the oldest waiting, and an answer applies to that call only (red team A29, phase P5): **Run once** runs it, **Run & switch to Auto** runs it and switches the mode (the calls still queued then run under Auto's rules), **Not now** returns `NX_CONTROL_DECLINED` for it; each call has its own 60 s clock from the moment the card shows it (ending 5 s before the bridge's queue wait), then `NX_CONTROL_PAUSED`. With more calls waiting the card says "Answer within N s for this step (M more waiting)". A call that needs no answer (the status read, an approved call) never waits behind the card. Only `nx_get_status` skips the hold (it reports the mode), and batch children inherit the parent's call. While two or more parts are loaded the lines name the target part's file first (3.4). Only the engineer changes the mode; no tool does |
| 6 | Part ownership | N · C | In production, `model` and `file` effects and `nx_close_part` may change only parts the agent created, opened when they were not already loaded, or was granted by the engineer on the panel. The work part is checked first. After the handler, any non-owned part whose `IsModified` flipped forces a rollback. Error: `engineer_control/NX_PART_NOT_GRANTED`. Off with `NX_MCP_DEV=1` |
| 7 | Receipt | N | Every non-`read` effect gets a fingerprint of tool plus canonical arguments plus the target part's file key (none without a target; a receipt written before targets matches tool and arguments alone). The replay runs right after gate 3t (a call whose target is no longer resolvable replays its receipt's own part); the same id with the same arguments in another part is `NX_IDEMPOTENCY_CONFLICT` naming that part. The same id with the same arguments replays the committed result (`replayed: true`); different arguments give `NX_IDEMPOTENCY_CONFLICT`. A committed call that `nx_undo_operation` or `nx_restore_checkpoint` reverted (`reverted_by`) runs again instead: its new receipt keeps the reverted run under `previous`, and a warning says so. Children write progress into the parent's receipt |
| 8 | Undo mark | N | `model` only: a visible mark `NX MCP: <label>`. Children share the parent's mark; a preview's mark is always undone |
| 9 | Unit conversion | N · C | `Quantity` fields become floats in the target part's or sheet's units, then canonical forms replace their alternatives (an axis-angle `Pose` becomes `{translation, rotation}`). Gate 1 already rejected wrong dimensions, so this gate cannot fail on arguments |
| 10 | Handler | N · C | `handler(tx, **args)` |
| 11 | Read-back | N · C | The handler verifies with `kit.readback`. For `model` tools the kernel also turns a snapshot diff into `changes`, drops inherited NX 2406 feature errors, and treats a new feature in error as `geometry_failed/NX_UPDATE_FAILED` with rollback. The same applies to every other part the handler declared with `tx.touch_part` (`kernel._readback_parts`) |
| 12 | Reference invalidation | N · C | After a commit: refs of objects NX no longer has. After a rollback: refs of objects the undo did not restore (1.3) |
| 13 | Annotation refresh | N | If `refresh_annotations` is set (default for `model` tools of modeling, sketch, freeform, sheet_metal and assembly), every `@refresher` hook runs inside the same undo mark, and what they refresh is added to `changes.modified`: out-of-date drawing views, detail boundaries, tables, hole callouts, sheet-metal callouts. The `assembly_constraint` hook only warns about constraints the call left unsolved (1.2); a failing status check never fails the call. On a background target the display-bound hooks wait for the part's next displayed model call (3.4) |
| 14 | Result projection | N (envelope), S (compaction) | The surface 1.6 envelope with `units`, `frame`, `changes` and `warnings`. With `detail=compact`, arrays over 20 items go to the result store |
| 15 | Error mapping | N, S | `NXError` becomes `{category, code, message, hint, field, retry, nx_code, details}`; `retry` comes from the category table |

### 3.2 `Tx` and the outcome rule

Members of `Tx`:

- `tx.nx`, `tx.uf`: the NXOpen modules. `tx.session`, `tx.part` (work part): mark the
  transaction *touched* when accessed. `tx.units`: `{"length": "mm", "angle": "deg"}`.
- `tx.resolve(ref, *kinds, work_part=False)`: the live object, or `stale_reference`,
  `wrong_kind` or `wrong_owner`; `work_part=True` refuses objects of other parts and
  occurrences, except a component of the work assembly. `tx.ref(obj, kind=None)`: a result
  row with its anchor.
- `tx.touch_part(part)`: declares another part the call changes (a component's prototype
  edited in context, e.g. assembly `component`/`target` arguments), before the change. The
  kernel snapshots it, lists the objects created or deleted there in `changes` and fails the
  call on its new features that do not update (gate 11); the work part and repeats are
  ignored.
- `tx.warn(code, msg)` (codes from `errors.WARNING_CODES`, else `NX_UPDATE_WARNING`) and
  `tx.checkpoint(done=None, total=None)` (3.3).
- `tx.after_undo()`: for handlers that undo NX marks (undo, restore); keeps the refs of
  restored objects and sets `tx.references` to `preserved` or `listed` (`tx.stale_refs`).
- `with tx.scratch():` temporary geometry for `check`/`analyze` tools, under a hidden mark
  that is always undone; object counts are verified afterwards.
- `tx.files.stage(path)` / `tx.files.publish()`: atomic workspace writes.
  `tx.non_undoable(reason)`. `tx.workspace`, `tx.dev`.
- `tx.given(name)`: whether the caller passed argument `name`, as opposed to the handler
  receiving the schema default (False when the call's arguments are unknown). E.g.
  `nx_capture_view` honours `width`/`height` exactly only when given.
- `tx.call(tool, args, operation_id=...)`: runs a catalogue tool as a top-level call of
  its own through `kernel.run` (own receipt, undo mark and undo-history entry) and returns
  its envelope, `ok` true or false. Only a `session` tool outside any undo mark may use it
  (`nx_redo_operation`).

Handlers never set undo marks, write receipts or touch the panel.

**`not_started` versus `unknown`.**

- A sidecar failure before sending → `not_started`. Transport loss after sending → `unknown`.
- An NX failure before the handler → `not_started`.
- A handler failure while *untouched* → `not_started`, and any undo mark is deleted. Untouched
  means both Tx tracking and the existing `side_effects.SideEffectObserver` saw no side effect;
  the observer stays around every handler for now.
- A handler failure after it touched NX:

| Effect | Outcome |
|---|---|
| `model` | Undo succeeds → `rolled_back`. Undo fails → `rollback_failed/NX_ROLLBACK_FAILED`, outcome `partial`. A declared `non_undoable` step → `partial` |
| `file` | Unpublished staged files are removed → `rolled_back`. After `publish()` → `partial` |
| `view` | Display snapshot restored → `rolled_back`, otherwise `unknown` |
| `session` | `unknown` |

`side_effects.py` remains as the host's side-effect observer for calls without an undo mark.

### 3.3 Long operations

| Cost | Typical tools | Kernel behaviour |
|---|---|---|
| `light` | registry and state reads, sketch curves, datums, annotations | allowed in a batch (model tools); the panel says only "running" |
| `heavy` | builders, model update | one call each; the panel says "NX may pause" |
| `long` | translators, PDF, assembly-wide checks | one call each; the bridge waits for its budget (below) |

Handlers that loop call `tx.checkpoint()` between native calls, and the kernel calls it
between batch children. Each checkpoint:

1. writes progress to the receipt (`nx_get_status` reads it from the worker-thread snapshot
   without queueing);
2. honours `nx_cancel_operation` by raising `NX_CANCELLED`, which rolls back a `model` tool;
3. enforces the cost class's cooperative budget (light 30 s, heavy 300 s, long 1800 s): over
   budget raises `NX_CANCELLED` with `details.reason = "budget"`.

A single native call cannot be interrupted, and the host never pumps NX's main loop.

**Bridge timing (phase P5, red team A27/A28).** A queued call waits at most `QUEUE_TIMEOUT`
(135 s) to start, then `not_started` ("safe to retry"); once started, its caller waits
`bridge.run_timeout(method)`: `DISPATCHER_TIMEOUT` (120 s) for light calls, the cost budget
+ 30 s for heavy (330 s) and long (1830 s) calls, so a long call or a long read returns its
result instead of `unknown` at 120 s. The sidecar's `DescriptorBridgeClient` waits
`client_timeout(method)` = queue + run + 15 s (270 s for light calls, as before), so NX's
own answer always arrives first. Past the run timeout the caller gets `unknown` ("may
still be running"; query the receipt). The ghost guard (call key `expires_at`, 3.4)
bounds a changing call by send time + `QUEUE_TIMEOUT` (`bridge.start_deadline`): the MCP
client itself (Claude Code) has no short request timeout to copy, so a call that sat in
NX's listen backlog during a long call never runs late. A bridge worker keeps its
client's socket on the queued call (`_PendingBridgeCall.peer`); a client that closed it
(an MCP cancellation closes the sidecar's connection) gets no run either
(`MainThreadDispatcher._execute`, the host's `_reject_gone`: `NX_CALL_EXPIRED`). Expired
queue entries leave the queue (`waiting()`).
`nx_get_status` `control.queue` = `{waiting, oldest_seconds}` counts the agent calls
queued for NX.

### 3.4 Several parts and parallel agents

Several parts can be loaded in one NX session, and several agents can work in them at the
same time through one bridge. Targeting knows no caller: the unit is the part, never the
agent. Only closing, activating and saving a part ask who sends the call (the `caller`
key, "Part ownership" below). While at most one of the agent's parts is loaded, every
call behaves as it did before this feature (solo behaviour unchanged). This section is the one place that
explains the feature; other sections point here.

**Targeting: rule G (gate 3t, `core/target.py`).** `resolve` decides in this order:

1. The tool's own part argument (`target="arg:<param>"`: `nx_activate_part`,
   `nx_save_part`, `nx_close_part` keep their `part` parameter), then a resolver (the
   call's own object: `nx_finish_preview`, `nx_undo_operation`, `nx_restore_checkpoint`,
   `nx_redo_operation`; when that object is gone the resolver answers `ANY`, so the
   handler says what happened instead of `NX_AMBIGUOUS_PART`).
2. **Refs pin the part.** A ref pins its object's part; an occurrence pins its
   prototype's owner, except under `target="assembly"` and for components, which pin the
   assembly that holds them. `part` is never required on a ref-pinned call. If given, it
   must agree with the refs, reads included, else `NX_WRONG_PART_CONTEXT` (naming the
   assembly that holds an occurrence ref agrees); the same holds for a resolver's object.
   Refs in several parts: a deciding kind wins (`assembly` policy: components,
   constraints, component patterns, explosions, trace lines; `sheet` policy: sheets,
   views, dimensions, annotations, tables); else a read takes the first ref's part, and a
   change takes the work part when it is one of them or fewer than two agent parts are
   loaded, else `NX_AMBIGUOUS_PART` (`details.parts`). A call that passes any ref is never
   ref-less: an unknown or stale ref goes to the work part and the tool reports the ref.
3. The `part` call key, then the parent call's part (a batch or preview child, the calls
   `nx_redo_operation` repeats).
4. **A ref-less call without `part`** counts the agent's loaded parts (`agent_parts`):
   the rows `nx_list_parts` marks `owned` (created, opened when not already loaded, or
   granted by the engineer on the panel), components included, never a part the engineer
   opened and did not grant. With 0 or 1 the call acts on the work part, exactly as before,
   except that with 1 it acts on that part when the work part is the engineer's (the
   engineer tab guard below keeps the engineer's part displayed and work while the agent
   works in its own; gate 4s switches to it). With 2 or more it is `NX_AMBIGUOUS_PART`:
   "N agent parts are loaded (and M of the engineer's) ..." with `details.loaded_parts`
   (at most 10, the work part last), `details.count`, and a hint that lists the files
   with the neutral example `{"part": "<file name>"}` (for `nx_create_component`: the
   assembly's file name).

There is no hidden mode and no time window: the rule reads only what is loaded now.
`any` tools (NX-global reads) are never ambiguous; `none` and `creates` tools, and a call
that works in a part it creates (`new_part`), have no target. `envelope.part` =
`{id, file, from}` (`from`: `refs`, `part`, `arg`, `resolver`, `parent`, `only`, `work`,
`any`) whenever `part` was given or two or more parts (any owner) are loaded. A changing
call that brings the agent's loaded parts from fewer than two to two or more carries the
`NX_NOTE` "2 parts are loaded: calls without refs now take part" (the number is the new
count; `census`/`load_note`). `nx_get_status` `parts.agent_loaded` gives the count. A
receipt's fingerprint includes the target part's file, so the same `operation_id` with
the same arguments in another part is `NX_IDEMPOTENCY_CONFLICT` naming that part.

**Call keys.** `part` and `expires_at` travel inside `arguments`, like `operation_id`.
The kernel takes them out before gate 1 (`split_call_keys`) unless the tool has a
parameter of that name, and validates them (`NX_INVALID_ARGUMENT`, field `/part` or
`/expires_at`). Catalogue schemas do not list them. The seven ref-less core tools listed
directly in the `agent` profile list `part` (`server.PART_KEY`: "Target part's file name;
needed when 2+ parts are loaded"): in benchmark bench3p agents trusted the strict
schemas over the instruction sentence, and with `part` left out none of four builders
passed it to a core tool. The listing cap rose to 15,300 characters for it (15,244
measured; the offset/limit descriptions and `idempotentHint: false` were dropped to keep
the raise small; section 6 has today's cap). The server does not validate input against
the listed schemas (`validate_input=False`) and the proxy forwards both keys untouched (`proxy.call`). The
agent instructions (`server.AGENT_INSTRUCTIONS`) say: "When two or more parts are loaded,
add the target part's file name to the arguments of every call without refs, the listed
core tools included".

| Key | Forms | Errors |
|---|---|---|
| `part` | a loaded part's file name with or without `.prt` (case-insensitive; `bracket.v2` is `bracket.v2.prt`), its workspace path, or `p:N` | not a string, empty or over 260 characters: `NX_INVALID_ARGUMENT`; a path outside the workspace: `NX_PATH_OUTSIDE_WORKSPACE`; no loaded part has the file: `NX_NOT_FOUND` listing the loaded files; two loaded parts with that file name: `NX_AMBIGUOUS_PART` (pass the path or `p:N`); a `p:N` of a closed part: `stale_reference` |
| `expires_at` | epoch seconds, or ISO 8601 (no zone: UTC) | the ghost guard: a changing call whose `expires_at` passed never runs, `NX_CALL_EXPIRED` (not started, no receipt); reads still answer |

The sidecar gives every changing call `expires_at` = send time + `QUEUE_TIMEOUT` (135 s)
unless the agent passed its own (3.3). Both the host (before showing or running a queued
call) and the kernel (gate 3t) apply the guard; a queued call whose client closed its
socket is refused the same way (`bridge.client_gone`). A batch or preview child takes
`part`; its `expires_at` is the parent's.

**Switching (gate 4s, `core/switching.py`, `kit/switching.py`).** The kernel makes the
target current itself, by `spec.switch_for(args, kinds)`:

| Mode | Tools | What NX does |
|---|---|---|
| `none` | reads; `none`, `creates`, `any` and `arg:` targets | No NX call. A target that is not the work part is bound (`Services.bound`): `tx.part`, `Services.work_part()`, units, `requires` and sheets follow it; `tx.scratch` makes it the work part quietly for its block |
| `work` | model tools (the default) | The target becomes the work part: `Parts.SetWork` for the display part or a component prototype under it, else `uf.Assem.SetWorkPartQuietly` (any loaded part, no UI change); the display never moves |
| `display` | `view` effects, the `display` and `drawing` domains, `interactive`, `application:sheet_metal`, explicit `switch="display"` tools (explosions, trace lines, WAVE links, model exports, `nx_update_assembly`, ...), the tools that activate an existing sketch (below), and `display_bound` variants, arguments and ref kinds (`nx_create_sketch` `/placement/path`, `nx_create_reference_set` `/component`, `nx_delete_objects` of `kind:explosion`, `kind:section_view` and drafting kinds, `nx_preview_changes` `capture=true`) | The target is displayed (`kit.parts.activate`: undo marks kept, no Sheet Metal dialog) and made the work part, and the application the tool needs (Sheet Metal) is entered |

- **No switch and no NX call** when the target is the work part, when it is the display
  part and the call did not name it with `part` (or the mode is `display`: a work
  component under the displayed assembly stays), or when NX does not list it (the tool
  reports it). `NX_MCP_SWITCH=display` makes model tools switch the display instead (the
  visible fallback).
- **A display-bound call that names a work part NX does not display** (`switching.named`:
  its `part`, refs or its own object; a quiet work part beside the engineer's displayed
  part, a work component's prototype) still shows it: the work-part shortcut above
  would let it act on the engineer's displayed part (drawing, capture, camera). It shows the part's tab for the call and gives the engineer's back (tabs
  below). An unnamed call acts on the work part as NX has it.
- A switch NX refuses is `NX_WRONG_PART_CONTEXT`, not started. After the handler the
  work part must still be the target (`Bound.check`), else `NX_WRONG_PART_CONTEXT` and
  rollback before commit (unless the call unloaded its part, an undo across its load).
- **Restore** runs at the end of the call, committed or not (after a rollback): the
  display part, the work part (through the previous work component when there was one)
  and the application. The application is carried: Sheet Metal is entered on the
  target, and the previous application comes back unless that would show NX's blocking
  Sheet Metal warning (a shown part with sheet-metal features entering Modeling); then
  NX stays and the call says so. A restore NX refuses falls back to the home state
  (`SetWorkPartQuietly(display)` + `SetWork(display)`, then the previous work part) and
  becomes an `NX_UPDATE_WARNING` on the envelope; a committed change stays committed. A
  state NX still did not take back is repaired at the next call's entry (`repair`,
  `NX_NOTE`).
- **Deferred refreshers.** Gate 13's display-bound refreshers (`drawing_view`,
  `drawing_table`, `dimension`, `annotation`: NX updates them only on the displayed
  part) do not run on a background target: the part is remembered
  (`switching.deferred`), an `NX_NOTE` says so when it has drawing sheets, dimensions or
  notes, and the next model call that runs while that part is displayed (drawing calls
  included) runs them; a save updates out-of-date views as well.
- Services without `switch` (unit-test fakes) refuse a `part` that is not the work part
  (`refuse_other_part`, the phase P0 rule). `Services.switch_log()` keeps the last 500
  switches with their timings. Measured live (NX 2406): work switch 0.05 ms + restore
  1.6-2.7 ms; display switch 136-170 ms + restore 130-245 ms (each way one
  `SetActiveDisplayRetainUndoMarks`); Sheet Metal carried 294 + 373 ms.
- `nx_create_wave_link` resolves to the assembly that holds its `target`, `component` or
  occurrence sources (else the displayed one): its sources are other parts' objects.

**Batches and previews (phase P6, `kernel.run_child`).**

- Each child of `nx_run_batch` and `nx_preview_changes` resolves its own target: its
  refs, a `part` key in its arguments (checked before any step runs: it must name a
  loaded part and agree with the child's refs), else the parent's part. It is switched
  for its step and the parent's state comes back after it. When the parent itself
  switched or was bound, a child switches whenever its target is not the current work
  part, or not displayed for a display-bound child.
- The batch's (or preview's) own part: `part` when given; else the one part every step
  names (its refs, its `part`, or an earlier step's result: `batch.steps_part`); else the
  usual rule (the work part, or `NX_AMBIGUOUS_PART` with two or more agent parts).
- A model child in another part makes the parent snapshot that part first
  (`tx.touch_part`), so the rollback, read-back (gate 11), `changes` and the busy stamp
  cover it. A child result in another part, or one that named `part`, carries `part`.
- One mark rolls back every part: NX's undo is session-wide (verified live on NX 2406,
  `acceptance/session/multipart_batch.json`: a failing third child rolled back a rename
  in one part and a delete in another).
- A failure after any child ran always rolls back (`rolled_back`). Before P6 a child
  refused inside `tx.checks()` reported `not_started` and left the earlier children
  applied.

**Undo per part (phase P3, `domains/session/history.py`, `compensate.py`).**

- The history stays one session-wide list (`Services.undo_history`) read per part:
  undo, redo, checkpoints and their depths act on the call's target part (the `part`
  key, the operation's or checkpoint's part, else rule G; `nx_list_checkpoints` is an
  `any` read: without `part` it lists every checkpoint and the work part's depth).
- NX's undo is linear, so the undo of part A uses `UndoToMark` only while nothing else
  lies between A's undone operations and the top of NX's list. Interleaving:
  - other parts' operations in between do not end A's history: the undo compensates A's
    operations instead (compensating undo v2, `data.compensated`), or refuses with
    `NX_CROSS_PART` naming the operations and parts in the way (`details.in_the_way`),
    why compensation does not qualify, and how to fix forward (`nx_delete_objects`, or
    setting the value back with the tool that set it). An operation qualifies when its
    committed receipt created features with their bodies and expressions and nothing
    else (no sketch, nothing deleted; drawing views, sheets and annotations NX refreshed
    count as derived, not as changed: benchmark bench3p refused a sphere for them), every
    created object still found by its live ref, and no feature outside the set among
    their `GetChildren`; or when its tool has a `@restorer` hook (`nx_set_attributes`,
    `nx_set_expression`, `nx_set_name`) and the call kept its before-values in its
    history entry (`tx.note_restore`, top-level calls only), and those values still read
    what the operation set (else `NX_DEPENDENCY_ORDER`). Values are set back first
    (newest first: a created attribute or expression is deleted, an empty feature name
    is removed through UF `Obj.DeleteName` as NX ignores `SetName("")`), each verified
    (`data.restored`); the deletion then runs and is verified on snapshots, and the
    kernel rolls everything back otherwise. Created expressions NX keeps are reported;
    anchors are re-checked (`References.recheck_anchors`). The compensation enters the
    history as a `compensation` entry: never undone itself, it blocks NX's undo of every
    older entry, of every part;
  - loads by a `file` or `session` call and kept-marks closes are barriers that end
    every part's history (`part_loaded`, `part_closed`): undoing across them would unload
    or reload the part;
  - **the engineer barrier**: an engineer change is a global barrier no undo or restore
    crosses (`engineer_change`; the interactive host appends it when its mark
    fingerprint changed between agent calls, `marks.engineer_change`). The history
    before it stays listed, checkpoints expire, the redo list empties and every part's
    revision moves.
- `nx_undo_operation(operation=...)` undoes one own operation (its part from the
  history, so no `part` is needed; `NX_DEPENDENCY_ORDER` while later operations of the
  part follow it, unless compensated); `mode: "compensate"` (`"delete_created"`, its v1
  name, is the same) always compensates, never across the engineer barrier. The default
  treats every block of NX's undo by other work alike (benchmark bench3t: another
  agent's close refused it while other agents' later work compensated it): when a load,
  a close or an evicted mark cut NX's history after the operations (`history.CROSSABLE`:
  `part_loaded`, `part_closed`, `mark_evicted`; not the part's own save, which stays
  `NX_NOT_FOUND` with the offer), it compensates when they qualify (`_compensable`; the
  `NX_UPDATE_WARNING` says "NX's undo no longer reaches it (...)"), else it refuses with
  `NX_NOT_FOUND` and the hint (why compensation does not qualify, how to fix forward).
  It never crosses the engineer barrier. A restore undoes the
  checkpoint's part's operations since by the same rules. The redo list is per part: new
  work in a part ends that part's items only (`kernel._record`).
- Previews compare `Services.part_revision`: the kernel bumps the target and touched
  parts of every non-read, non-view call that may have changed something, so another
  part's work, reads and view changes do not age a preview. A preview is accepted in its
  own part; a refused accept keeps the preview.

**Loads and undo marks (phase P1, `core/marks.py`).** NX keeps one undo list for the
session (NX 2406, measured): a plain `SetDisplay` drops every mark when a live mark is
older than the first display of the shown or hidden part, and so do a load that displays
the part (`FileNew` with `MakeDisplayedPart`, `OpenBaseDisplay`), a save and a plain
`Close`. NX MCP therefore loads without display (`FileNew` with `MakeDisplayedPart`
false, `OpenBase`, `AddComponent`) and switches the display only through
`kit.parts.show` (`SetActiveDisplayRetainUndoMarks(AllowAdditional, SameAsDisplay)`;
the sheet-metal scratch routine too, whose scratch part closes with
`CloseRetainUndoMarks`); a grep test forbids every other switch. Undoing to a mark set
before a load unloads that part again, so a load by a `file` or `session` call is a
barrier (`{mark: None, load: [paths]}`). With two or more agent parts loaded, or while
the engineer's part is displayed, `nx_create_part` only loads the new part (work and
display parts stay; tabs below). A model call that loads a part (a component from a
file, an import into a new part) needs no barrier: undoing it unloads that part, which the session tools forget
and name in an `NX_NOTE`. A mark that went away says why (`expired_reason`):
`saved_or_closed`, `part_loaded`, `display_reset` (a display switch outside NX MCP
dropped the marks), `mark_evicted` (NX dropped its oldest marks, with an `NX_NOTE`),
`part_closed`, `engineer_change`, `undone_past`.

**NX window tabs (`kit.parts.show`, `present`; tabs probe on NX 2406).** Every part NX
MCP displays gets its own NX window tab (`DisplayPartOption.AllowAdditional`); for a part
that has a tab the same call only activates it. No call ever replaces the part of
another tab, so the engineer's model is never swapped out of its window.

| fact (NX 2406.9160, measured) | value |
|---|---|
| undo marks | kept through new tabs, tab re-activation, quiet work, tab closes and a display from no part |
| cost | new tab 0.2-0.9 s; re-activating a tab 63-92 ms (cheaper than the old replace, 136-240 ms); from no display part 0.3-0.5 s |
| tab list | `Parts.GetDisplayedParts()`: the active tab (`Parts.Display`) first, then the most recently active (`kit.parts.tabs`) |
| drawings | a sheet stays open in its tab; `CurrentDrawingSheet` reads None while another tab is active and comes back with the tab |
| application | one per session, sticky across tab switches (the Sheet Metal rules of `kit.parts` apply unchanged) |
| close | `CloseRetainUndoMarks` closes the part's tab, marks kept, also for the active or only tab; closing the active tab leaves NX with no display part until a tab is activated |
| dialogs | none |

- **Display-bound calls** (gate 4s `display` mode) activate the target's tab, run, and
  re-activate the tab that was active (`Switch.restore`), so the engineer's tab comes back
  and the target keeps its tab (and its open sheet) for the next drawing call.
- **Sketch activation is display-bound** (NX 2406 crash): `Sketch.Activate` of an existing
  sketch with a dimension or an offset annotation in a part that is not displayed (quiet
  work part, or its own background tab) raised a General Fault Exception, then NX died.
  So every tool that activates an existing sketch runs with its part displayed, whatever
  the sketch holds: `display_bound=True` for the curve, edit, constraint, dimension,
  corner, copy and projection tools, `nx_finish_sketch` and `nx_check_sketch`;
  `nx_get_sketch` when `include` has geometry or status (constraints alone never
  activate); `nx_import_model` into a `sketch`. Gate 11 no longer re-activates a sketch of
  a part that is not displayed. `nx_create_sketch` creates, activates and parks a new
  sketch in the background safely. A sweep of every scenario beside a displayed
  engineer part (`run --beside-engineer`) found no other crasher; it made
  `nx_export_flat_pattern` display-bound (NX's export makes the display part work) and
  `nx_get_sheet_metal` a `work` switch (NX answers in the work part's units), and gate 3t
  pins the part of a wrong-kind ref or an anchor it cannot resolve, never falling back to
  the engineer's work part.
- **The engineer tab guard** (`kit.parts.engineer_tab`, `present`; benchmark bench3t:
  all 12 `nx_create_part` calls of four agents passed `display: true` and the engineer's
  tab was lost for 5 min 19 s; the first agent part would have taken it without
  `display` too, as `crowded()` counts agent parts only). Agents never take over the
  engineer's active tab: while the displayed part is the engineer's (not created, opened
  or granted to the agent, `Services.owns`), `nx_create_part`, `nx_open_part`,
  `nx_activate_part`, an import into a new part and a clone with `activate` never leave
  an agent part active and never change the work part, whatever `display` and `work`
  say. `display: true` (and `nx_activate_part` by default) gives the part its own tab
  behind the engineer's (`tab_behind`: shown, then the engineer's tab and work part
  re-activated, 0.2-0.9 s for a new tab); otherwise the part stays in the background
  without a tab (an import runs in the new part's tab, then gives the engineer's back).
  `data.display` and `data.work` are false, and an `NX_NOTE` says where the part is and
  that calls reach it with `"part": "<file>"` (`shown_note`); the read-back checks the
  engineer's tab is still the active one (`active_tab`). Ref-less calls then reach the
  agent's only part (rule G above). An `nx_open_part` of a part the engineer has
  displayed (already loaded) leaves it as it is.
- **create / open / activate without an engineer part displayed** (`kit.parts.present`):
  while the part is the agent's only part (rule G's count) its tab is left active, as
  before. With two or more agent parts loaded, `nx_create_part` does not display the new
  part, `nx_open_part` and `nx_activate_part` give the part its tab and re-activate the
  tab that was active (and its work part); `display: true` leaves the part's tab active,
  `false` gives it no tab. `work: true` with `display: false` makes the part the work part
  only where NX allows it (the display part or a component under it); elsewhere it is
  no longer refused (bench3t: a read-only `nx_open_part(display=false, work=true)` was
  `NX_WRONG_PART_CONTEXT`): the work part stays and the note says how calls reach the
  part. Whenever the part ends neither active nor work part, the same note says so
  (bench3t: `nx_activate_part` with its defaults replied `work: false, display: false`
  and nothing else).
- **Closing the active tab** re-activates the most recently active remaining tab
  (`_saving._tab_back`, through `kit.parts.activate`: a sheet-metal part comes back in
  Sheet Metal, no dialog); `data.displayed` names it. No other part takes a closed part's
  place in a tab.
- **Explicit loads** (`kit.parts.explicit`): a part `nx_create_part`, `nx_open_part` or an
  import's new part loaded by name is recorded; closing an assembly never unloads it
  (below).

**Part ownership, closes and parked sketches.**

- **Caller identity** (the `caller` call key, `core/target.check_caller`): the sidecar
  adds `{"id", "shared"}` to every NX call and replaces any `caller` an agent passes.
  `id` = the sidecar process (`sc-<8 hex>`), its MCP session (`.N`) and, when the MCP
  client puts one in the request's `_meta` (`agentId`, `sessionId`, `clientSessionId`,
  any namespace prefix), `:<that id>`. One agent per sidecar (the usual deployment): one
  id per agent. Subagents of one client share its MCP server, so they share one id;
  `shared` is true while that id ran calls at the same time within the last 120 s. A
  call without the key (an older sidecar, a direct bridge client) has no identity.
- **Busy table** (`core/busy.py`, `Services.part_activity()`): a committed call stamps
  the parts it worked in (`at`: its target, the parts its batch children changed, the
  part `nx_create_part`/`nx_open_part` made or reused, `tx.worked_in`, else the first
  part it loaded; loads also stamp `loaded_at` and record the loading caller in
  `owners`). A `model` or `session` call, the new part of `nx_create_part` and a part a
  call without target loaded also stamp `changed_at`; `nx_save_part` stamps `saved_at`,
  and so does `nx_open_part` for a part it loaded from its file; `file` calls (saves,
  exports) never change a part. Reads and `view` effects stamp nothing. The agent parts
  and their callers survive a host reload in the same NX process
  (`.nx-mcp/part-owners.json`: tag, file and caller; `adopt` at the first call).
- **Engineer parts** (not created or opened by an agent call, `Services.created`; a
  part only granted on the panel is still the engineer's): `nx_close_part` and
  `nx_activate_part` refuse them, `nx_save_part` refuses them unless granted
  (`NX_ENGINEER_PART`, category `blocked`, `details.action`; no `force`, development mode
  included: tooltest2 lost the stand-in engineer part `eng_desk2` to a `save: false`
  close). `nx_open_part` of a loaded engineer part changes nothing (no tab, no work
  part) and says so. Reads still read them.
- **Another agent's part** (its owner's id and the caller's id both known and
  different): `nx_close_part` and `nx_activate_part` refuse it without `force`
  (`NX_PART_BUSY`, `details.reason: other_agent`, `details.owner`), saved or not; saving
  it is allowed (it loses nothing); `nx_open_part` of it never shows it.
- **No identity tells the callers apart** (same id, or either unknown): a close is
  refused (`NX_PART_BUSY`, `reason: recent_change`) only when the caller's connection is
  `shared`, the close discards changes (`save` false, the part modified), the part has
  unsaved changes from the last 300 s (`BUSY_SECONDS`) and a call worked in it within
  30 s (`RECENT_SECONDS`). The message names the part being closed and its last change;
  the hint offers `force: true` or `save: true`. A solo connection (a sidecar per agent,
  the acceptance runner) never meets this rule, and a close that saves never does. In a
  shared connection an agent's own discarding close within 30 s of its own work is
  refused too (one retry with `force`). tooltest2 (30 agents, one sidecar): the old rule
  (keyed on the session-wide last call) refused 25 own closes and no foreign one; the
  foreign close of `parts_files_2_a` (changed 17 s, worked in 0 s before) meets this
  rule, own closes 2-56 s after own edits mostly do not.
- **Kept marks.** `nx_close_part` closes with `CloseRetainUndoMarks` (`data.undo_marks:
  kept`), displayed or not; closing the active tab brings the most recently active
  remaining tab back (`data.displayed`). Only an NX without the call drops every mark
  (`dropped`, an `NX_NOTE` when there was history). A kept-marks close is a `close`
  barrier (above): undoing across it loads the part again (seen live). In-call cleanups (a
  failed create or import, a clone) keep the plain close.
- **Assembly close: the precise unload rule.** Closing an assembly unloads only component
  parts the assembly itself brought in (its open, `load_components`, or a component call
  that loaded the file) and only when nothing else uses them. It keeps
  (`kept_components` with the reason) a part created or opened by name (`created or
  opened by name`), a part the agent did not load, a part another loaded assembly uses, the
  work or display part, a part with unsaved changes, and a component a call worked in after
  the assembly's load (`in use`). No part nobody asked to close is closed.
- **Parked sketches** (`core/parking.py`): no sketch is active between calls.
  `nx_create_sketch` without `finish` deactivates its sketch at the end of the call
  (model update, no view change) and parks it as its part's open sketch (`active` and
  `parked` true; `nx_get_sketch` reports `parked`); the sketch tools activate it by ref
  for each call (`kit.sketch.editing`);
  `nx_finish_sketch` finishes it (the ref, or the target part's open sketch). Gate 4's
  `no_active_sketch` refuses only in the part with the parked sketch, so other parts'
  calls never wait for it.

**Ask me first, one call at a time (phase P5, `interactive.py`).** The card shows one
call, the oldest waiting, on its own 60 s clock from the moment it shows (ending 5 s
before the bridge's queue wait); the answer (Run once, Run & switch to Auto, Not now)
applies to that call only, and "Answer within N s for this step (M more waiting)" says
how many wait. While two or more parts are loaded the card, the running line and every
activity line (panel and `.nx-mcp/activity.log`) name the target part's file first,
`[cover.prt] Extrude ...` (`dispatch.target_file` before the call, `envelope.part` after
it; a group of read lines never mixes parts). A queued call whose client gave up (its
`expires_at` passed, or its socket closed) is answered `NX_CALL_EXPIRED` without being
shown or run, and the activity log gets one line for it: `Skipped [b.prt] Extrude ...:
the agent stopped waiting`. A declined (`NX_CONTROL_DECLINED`) or unanswered
(`NX_CONTROL_PAUSED`) call gets only a panel notice for 30 s, no activity-log line.
A development snippet (`nx_dev_python`, the runner's engineer step) names no part.

**Panel "Parts in use" and engineer changes (`interactive.py`, `activity.py`).** Below
the activity list the panel lists the agent's loaded parts (created, opened or granted;
never the engineer's own), one line each: file, `shown` (the active tab), `in a tab` or
`background`, `modified` / `saved` / `new`, and the last agent step in it with its time
(`activity.touch`, `part_line`). The list scrolls (2 to 6 lines high at the 560x300
minimum); "Show part" or a double click activates that part's tab (`kit.parts.activate`,
an engineer action, refused with a notice while the engineer is in an NX command). It
refreshes after every call. An engineer change is a change of NX's undo marks (newest
mark or count) or of the loaded parts between two agent calls (`_edit_changes`); a
display, work part or tab change alone (a tab click, "Show part") is no edit and keeps
every agent's undo history. The ENGINEER log line says what changed
(`(changed: the number of NX undo marks)`) and names no part. `control.queue` in
`nx_get_status` counts the calls waiting for the UI thread at the moment of the read,
also when the status is answered from the running call's snapshot; `control.waiting` is
that count and `control.busy` agrees with it (true while a call runs or waits, the
engineer is in an NX command or a card waits; `activity` `queued` between two ticks).
The engineer-only and card-only counts are `engineer_busy` and `waiting_for_answer`
(benchmark bench3t read `busy: false, waiting: 0` next to `queue.waiting: 4`).

**Limits.**

- One NX undo stack for the session, shared by all agents: NX keeps about 180 marks
  (181 measured on NX 2406) and drops the oldest first, so with N agents working each
  has an undo depth of about 180/N; evicted marks are reported as `mark_evicted`.
- An engineer's display switch in the NX UI can drop every mark (`display_reset`); a
  tab click was not measured (NX MCP never clicks).
- A display-bound call on a background part shows its tab briefly and re-activates the
  engineer's (two tab switches, 63-92 ms each once the tab exists), also when that part is
  NX's quiet work part beside the engineer's displayed part.
- Analysis display objects (`nx_create_surface_analysis`) are made only in the displayed
  part (`kit.analysis_objects`); a target that is not displayed is refused
  `NX_WRONG_PART_CONTEXT`, and `nx_close_part` deletes a part's analysis objects first.
- Without an engineer part displayed, an import into a new part (`new_part_path`)
  still leaves the new part's tab active.
- Two agents in one part share its history: undo and checkpoints see one part, not two
  callers. Agents behind one shared sidecar have one caller id (`caller.shared`): only
  the recent-change rule protects their parts from each other's closes.
- `part-owners.json` is rewritten whenever the agent's parts change: after each
  committed changing call and after the host tracks the parts a call loaded
  (`Services.track`: development snippets and failed calls included); the parts an
  earlier host generation listed are adopted before it is rewritten, so owners survive
  successive reloads. A part matches its row by tag and file: one the engineer closed and
  reopened (a new tag), or one loaded by a host that never wrote the file (older code), is
  the engineer's after a host reload; tools then no longer close, show or save it (the
  engineer, or a development snippet `services.own(part)`).
- `uf.Assem.SetWorkPartQuietly` is undocumented; `NX_MCP_SWITCH=display` is the
  fallback.
- A preview measures its own (the parent's) part only: in a mixed-part preview the
  `before`/`after` state covers that part, not the children's other parts.
- Gate 13's annotation refresh after a batch runs for the batch's part only.
- The acceptance runner refuses to start a scenario beside loaded parts the agent left
  or parts outside `smoke/` (`blocking_parts`).

---

## 4. As built

The migration (F1-F4) is complete: the pre-catalogue executor and its mixins, `certified.py`,
`integration_server.py`, the `*_server.py` stubs, the manifest, `request_summary.py` and the
other name lists are deleted. What holds today:

- **Call path.** NX side: host (`nx_app/startup/nx_mcp_startup.py` on NX 2406,
  `nx_app/journals/start_2606.py` on 2606, `nx_app/journals/start_batch.py` batch via
  `nx_bridge.py`) -> `host/dispatch.py` -> `core/kernel.py`, with `host/services.py`
  (`NXServices`: receipts, object registry, ownership, undo history, checkpoints, snapshots,
  update-error cleanup). Sidecar: `sidecar/server.py` installs `tools/list`/`tools/call`,
  `proxy.py` runs gates 1-3, 14, 15 and calls the bridge, `local.py` runs `runs="sidecar"`
  tools through the same kernel over `SidecarServices`, `artifacts.py` keeps result snapshots
  (`result_id`) and the `nx-artifact://` resource.
- **Profiles.** `agent` (default) lists the 4 gateways plus the 14 core tools; `full` lists
  the gateways plus every visible catalogue tool. Discovery and the evidence gate use the build
  in the bridge descriptor. `NX_MCP_DEV=1` shows and runs untested tools; production refuses
  them (`NX_VERSION_UNSUPPORTED`).
- **Ownership (gate 6).** Every call owns the parts it created or opened; the engineer grants
  their work part with the panel checkbox **Allow agent to change <part>**, otherwise
  `NX_PART_NOT_GRANTED`. `file`/`session` tools that save or close a part call
  `tx.require_owned`. `@tool(new_part=...)` skips the work-part check for a call that works
  in a part it creates.
- **Several parts and parallel agents (phases P0-P6).** A call acts on the part its refs
  name or its `part` call key, else the work part while at most one agent part is
  loaded; the kernel switches NX to that part for the call and back (3.4). The seven
  ref-less core tools (`nx_list_components`, `nx_list_objects`, `nx_get_model_summary`,
  `nx_find_geometry`, `nx_check_model`, `nx_capture_view`, `nx_undo_operation`) list
  `part` in their schemas (`server.takes_part_key`, `PART_KEY`; see Call keys).
  `Tx.target` carries the resolved target; `tx.call` passes it to the calls a redo
  repeats.
- **Receipts (gate 7 before gate 2).** Repeating a finished call with the same `operation_id`
  and arguments replays its envelope; different arguments are `NX_IDEMPOTENCY_CONFLICT`, a
  running one `NX_OUTCOME_UNKNOWN`. A call an undo or restore reverted runs again (receipt
  `previous` links the reverted run); `nx_redo_operation` repeats such calls through
  `tx.call`, each a top-level operation with its own mark and history entry. The redo list
  (`services.redo_steps`) gets the calls an undo or restore reverted; the kernel drops a
  part's items when a new model call of that part enters the undo history,
  `engineer_changed` empties it, and a part close drops that part's entries.
  Retention: a background thread started at host start deletes receipts older than 7 days
  and all but the 20,000 newest (never a running one, which has a `running/<id>` marker
  that host start reads instead of every receipt); a pruned `operation_id` reads as
  `unknown` and runs again, since replay is meant for recent retries.
- **Sheet units (gate 9)** come from the referenced sheet or view, a new sheet's `units`, the
  current sheet, else part units; handlers get `tx.sheet` and `tx.units`.
- **View rollback.** `view` handlers record what they change with `tx.display.keep*`; a failure
  restores it. `restorable=True` returns a `restore_id`.
- **Long operations.** `tx.checkpoint(done, total)` reports progress, honours cancellation and
  enforces the budget per cost class (light 30 s, heavy 300 s, long 1800 s).
- **Compact schemas.** Listings omit reference and unit patterns (gate 1 checks them); repeated
  subschemas move to `$defs`. A variant's required fields reach `nx_get_tool_schemas` as the
  compact `required_by` map (1.2), not as top-level `anyOf` branches; two or more variants
  of one tool fetched together share one entry. Budgets are enforced by
  `tests/sidecar/test_budget.py` (section 6).
- **No NX prompts.** `host/dispatch.execute` runs every call under `services.quiet()`
  (`host/quiet.py`): NX's default update-failure action is `Undo` (NX undoes a failed update,
  code 670024 -> `NX_UPDATE_FAILED`, the kernel rolls back; never "Error During Update") and
  listing-window output goes to a file and into the result; the engineer's settings return
  after the call. `host/dialogs.py` counts only modal dialogs (disabled owner); in development
  mode a new one answers the running call at once with `NX_MODAL_DIALOG_OPEN`. A failed
  `nx_dev_python` snippet is undone and the display and work parts it started with return;
  a snippet that changed nothing (objects, loaded parts, modified flags) deletes its mark,
  leaves no undo-history entry and reports `not_applicable`.
- **Files.** `Files.publish` never overwrites; staging under `.nx-mcp/staging/<operation_id>/`
  is removed when the call ends.
- **Acceptance.** `python -m nx_mcp.acceptance run <domain>[/<name>] --owner <agent>` replays
  scenarios through a fresh stdio sidecar (`$part` is `smoke/<run-id>/...`), closes every part
  it loaded, and writes `acceptance/evidence/<build>/<domain>/<name>.json`: deterministic
  proof only (scenario and tool source digests, NX build, per-step pass/fail and checks, one
  `recorded` time), left untouched when only `recorded` would change, so an unchanged re-run
  yields no diff. Run id, owner, part paths, times, result digests and cleanup go to the local
  run log `<workspace>/.nx-mcp/acceptance-runs/<run-id>.json`.
  `python -m nx_mcp.acceptance fold` rebuilds `src/nx_mcp/evidence/*.json` from passing runs
  (deterministic: `recorded` comes from the evidence files).
  `scripts/generate_capability_matrix.py` and `scripts/generate_tool_reference.py` render
  `docs/capability-matrix.md` and `docs/tools.md` from the catalogue and that evidence.

## 5. Rules for agents

1. **Your scope.** One domain package per agent: `src/nx_mcp/domains/<d>/`,
   `tests/unit/<d>/` and `acceptance/<d>/`. Only the runner writes `evidence/<d>.json`. Never
   edit another domain.
2. **Never copy a helper.** Code that two domains need belongs in `kit/`. Add it there with a
   test, in a kit module no other agent is editing at the time, or ask the orchestrator.
   `core/`, `host/`, `sidecar/` and the bridge modules belong to the orchestrator: describe the change
   you need in your report.
3. **One tool is one `@tool` function.**
   - Write the signature with `core/types`, keyword-only after `tx`, and keep the docstring
     within budget.
   - Put `label`, `summary`, `keywords`, `effect` and `requires` in the decorator. There are no
     name lists, manifest entries or panel templates.
   - Validate arguments before touching `tx.session` or `tx.part`.
   - Build with `kit.builders.build` and verify with `kit.readback`.
   - Raise errors only through `errors.fail` or `native()`, and never `import NXOpen`.
4. **Every tool lands with** unit tests on `tests/kit/`, an acceptance scenario that covers
   every variant with numeric `expect` checks, runner evidence on NX 2406, and a green contract
   test.
5. **Live NX.**
   - Take `scripts/dev_lock.py acquire --owner <agent>`, then run
     `scripts/dev_reload.py --wait-ready`, then `dev_lock.py release`. Keep each session under
     15 minutes. `python -m nx_mcp.acceptance run|call --owner <agent>` takes the lock
     itself; `run --beside-engineer` replays scenarios while the engineer's own parts are
     loaded (never touched).
   - Look up builder members with `scripts/dev_python.py`.
   - Use disposable parts under `smoke/<agent>/` only. Never modify, save or close parts you
     did not create, and never touch `.nx-mcp/ui-stop` or `.nx-mcp/dev-exit`.
6. **Never** run git commands or install packages. Lint and format only your own files.

---

## 6. Tests, contracts and token budgets

```text
tests/kit/            the one shared fake: fake_nx(units), block/face/edge factories, on_commit hooks,
                      live_builders, undo_marks; run(nx, tool, **args) goes through the real kernel
tests/core/           validation parity (Pydantic vs stdlib), errors, every kernel gate and outcome,
                      references, receipts, evidence gating, summary rendering
tests/unit/<domain>/  handler behaviour on tests/kit, mirroring domains/
tests/sidecar/        profiles, gateways, schema generation, proxy, artifacts
tests/acceptance/     runner and expect operators against a fake bridge
tests/host/           dispatch, services, ownership, panel and startup app
tests/test_*.py       bridge, workspace, transport, generated docs
```

The `real_nx` marker runs the acceptance runner on the dedicated NX runner and remains the
release gate.

The **per-tool contract test** is parametrised over the registry. It checks:

1. **Spec complete.** All fields are valid, the surface 1.7 budgets hold, keywords are unique
   within the domain, and `example` validates.
2. **Summary.** For `example` and for every argument set in the tool's scenarios, the summary
   renders without falling back to `label`, within 60 characters, with one template per
   variant.
3. **Schema bounded.**
   - Every array has `maxItems`; every string has `maxLength` or is an enum, except references
     and quantity strings, whose bounds gate 1 checks (an agent copies a server-issued ref and
     writes '10 mm'; their lengths would only spend characters in every schema).
   - Numbers are finite, and there are no `additionalProperties`.
   - The tool definition is ≤ 1,600 characters, or ≤ 3,000 for discriminated unions.
4. **Evidence present.** v2406 evidence covers every variant. A `source_sha256` mismatch is a
   local warning and a release-build failure.
5. **Handler signature matches.** `tx` comes first, the other parameters are keyword-only with
   core-type annotations, and the returned keys match the `TypedDict`.
6. **Codes.** Every code reachable through `fail` or `native` (found by an AST scan) is in the
   table.
7. **Imports.** The domain module imports with NXOpen blocked and contains no `import NXOpen`.

**Token budgets.** `tests/sidecar/test_budget.py` measures what an agent reads (characters
of compact JSON, 3.8 characters per token) and fails above a cap.

The principle (engineer, 2026-10-13): tools must be explicit and schemas detailed. A failed
call and its retry cost about 1-3K tokens; about 100 more schema characters cost about 26
tokens per fetch. The budgets exist to catch bloat (repetition, filler, bytes no agent
uses), never to justify leaving out a rule an agent needs; when such a rule does not fit, the
cap changes in the same change, with the reason.

Every cap is a measured value plus headroom: about 3% on the `agent` listing (paid on every
turn), about 12% on the drift checks (percentiles, the typical task), and a small margin on
the hard per-row and per-entry caps. A cap moves only when what it measures changes in kind
(a new key on every row, a new section in the listing), never because a tool or a domain was
added: the caps are per tool, per row and per entry, percentiles, or (the `agent` listing,
whose `nx_find_tools` domain enum grows) a formula in the number of domains. The listings
leave out what carries no information for an agent: MCP annotation hints equal to MCP's
defaults (`readOnlyHint` false, `destructiveHint` true, `idempotentHint` false,
`openWorldHint` true), the `maxLength` of references and quantity strings and the page
offset's upper bound (gate 1 checks all three). They keep `additionalProperties: false`,
units in field descriptions, enums, item counts and defaults.

The table is generated from the test's own measurement (`python
scripts/generate_budget_table.py`; `--check`, a pre-commit hook, reports drift); edit the
caps in the test, never the table:

<!-- BEGIN budget table: generated by scripts/generate_budget_table.py -->

| Item (characters of compact JSON) | Cap | Measured |
|---|---|---|
| `agent` profile listing (4 gateways + 14 core tools; paid every turn) | 15,340 + 15 per domain = 15,520 (12 domains) | 15,412 (about 4.1K tokens) |
| `agent` server instructions (once per session) | 1,600 | 1,485 |
| Full listing (name + description + input schema, call keys included) | mean 1,140 per listed tool | mean 1,056.5 over 270 tools (285,255, about 75.1K tokens) |
| Tool definition (name + description + input schema, no call keys) | each 1,600; 3,000 for unions | max 1,589 (179 plain tools), 2,746 (87 unions) |
| Tool definition drift (75th percentile per kind) | plain 1,050, union 1,860 | plain 974.5, union 1,711.5 |
| `nx_find_tools` page (26 realistic queries) | row 170; rest of the page 400; 90th percentile page 1,640 | row max 163, rest max 385, 90th percentile 1,489.5 (max 1,784) |
| `nx_find_tools` domain overview (empty query, one row per domain) | row 140; rest 375; no total | row max 125, rest 333 (1,558 in all) |
| `nx_get_tool_schemas` entry, over the tool's definition | each 250; mean 85 | max 194, mean 78.6 |
| Typical modeling task (overview, 3 searches, 4 schemas in one fetch) | 13,650 | 12,503 (about 3.3K tokens) |

<!-- END budget table -->

Compact results before `result_id` paging aim at 4,000 characters (not measured by the test).
