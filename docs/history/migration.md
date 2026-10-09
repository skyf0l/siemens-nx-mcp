# Migration to the catalogue architecture (F1-F4, history)

Moved from section 4 of [architecture.md](../design/architecture.md) once the migration finished: the plan and the as-built notes of each step. Mentions of legacy code (`HardenedExecutor`, `LegacyServices`, manifest, `*_server.py`) describe code that F4 deleted.

## Coexistence and migration

**During migration:**

- The sidecar serves catalogue tools plus legacy tools that are not yet replaced. Legacy tools
  are wrapped as pseudo-specs, with their effect taken from today's lists, so `nx_find_tools`
  and `nx_invoke` cover both.
- `HardenedExecutor.execute` starts with
  `if spec := catalogue.get(method): return self.kernel.run(spec, params, ctx)`; every other
  tool takes the legacy path.
- `Tx` reuses the existing services through a `LegacyServices(executor)` adapter:
  `OperationStore`, `ObjectRegistry` (with short-ref aliases), `_history` (for
  `nx_undo_operation`), checkpoints, `_snapshot`, `_clear_update_errors`,
  `_drop_inherited_feature_errors`, the legacy annotation refresher and the review epoch.
- The host takes panel summaries and activity grouping from `spec`, falling back to
  `request_summary` for legacy tools.

**Each domain migration deletes**, in the same change:

- the replaced legacy tools (surface section 3, "Replaces") and their mixin code;
- every name-list entry: `READ_ONLY`/`NON_MODEL` in both places, `SIDE`, `PATHS`,
  `DESCRIPTIONS`, `VIEW_ONLY`, `CORE`/`DOMAINS`/`DEFAULTS`, guidance sets, `PAYLOADS`, summary
  templates, manifest and fragments, the handler map, the batch allow-list, and the
  refresh/topology exemptions;
- their old tests.

A test fails while any catalogue name or replaced legacy name remains in a legacy list. A legacy
tool may be deleted only once no scenario `setup` step calls it. The deleting agent rewrites
those steps; this is its only edit outside its own domain, and it never touches an `expect`
step.

| Step | Content | Parallel |
|---|---|---|
| F1 | `core/`, NX dispatch in `HardenedExecutor.execute`, `LegacyServices`. Pilots: `nx_create_part` (file, `new` path, ownership), `nx_create_datum_plane` (model, variants), `nx_measure_distance` (read, two refs) | with F2 |
| F2 | `kit/`, consolidated from today's copies (5 vector-math, 6 number validators, 10 native-error mappings, 82 `Destroy` blocks, 4 sketch-ownership checks), plus `tests/kit/` | with F1 |
| F3 | `sidecar/` generation, both profiles and the 4 gateways (replacing `nx_discover_tools` and the 13-entry agent surface); the acceptance runner and evidence writer; the scenario base set, migrated by the orchestrator: `nx_open_part`, `nx_find_geometry`, `nx_list_objects` (body, face, edge, feature, sketch listers), `nx_measure_mass`, `nx_measure_bounds`, `nx_check_model` | after F1 |
| D1-D10 | Domains, ordered by what the next feature wave needs: modeling; drawing; parts_files with the feature tree and expressions; sketch; assembly; sheet_metal; freeform; inspection; display; session. Each domain also registers its hooks | one agent per domain |
| F4 | Delete the legacy code. `host/nxservices.py` absorbs the rest of `NXOpenExecutor`, and Tx tracking replaces `side_effects.py`. Update CLAUDE.md, README and `docs/architecture.md` | last |

**F1 as built (deviations and additions).**

- Pilots are `nx_create_datum_plane` and a first slice of `nx_list_objects` (kinds body, face,
  edge, datum, feature, sketch). `nx_create_part` and `nx_measure_distance` were not migrated:
  their names are live legacy handlers, and a catalogue entry would change their arguments and
  result shape under the current sidecar before F3. A test fails if a catalogue name shadows a
  legacy handler; the migrating change deletes the legacy tool first.
- Gate 3 lets untested tools run with `NX_MCP_DEV=1`, so the acceptance runner can record their
  first evidence. Production refuses them.
- Gate 4 runs once, in the kernel after the hold; the pre-hold run is host work.
- Gate 6: before the handler only `model` effects check the work part; `model` and `file`
  effects get the after-handler `IsModified` check. Ownership tracking and the panel grant:
  see the F1b notes below.
- Any failure after the handler returned counts as touched and rolls back. The rollback
  snapshot is compared before undoing.
- Receipts replay a failed result too (same id, same arguments); a running or unknown receipt
  gives `NX_OUTCOME_UNKNOWN`. Read tools report `not_applicable` on failure.
- `changes.references` is `listed` with `stale_kinds: ["face", "edge"]` after a
  `topology` commit. A returned `data.page` is lifted to the envelope's `page`.
- Batch placeholders are 0-based: `"$0.data.feature"`; a row resolves to its `id`.
- Additional `Tx` members: `tx.run(tool, args)` (child call), `tx.preview()`,
  `tx.kind_of(obj)`, `tx.services`, `tx.outcome`, and `tx.resolve(ref, *kinds, work_part=True)`.
- `@lister(kind)` is `fn(tx, owner, owner_kind) -> [(obj, kind, extra)]`; rows are built only
  for the requested page. Hook keys may be a kind group (`datum`).
- `NXToolError` now lives in `core/errors.py` (re-exported by `runtime`) and `NXError` subclasses
  it, so legacy `except NXToolError` blocks and the legacy executor handle kit errors with their
  own code. `NXError.as_dict()` is the legacy bridge payload (plus `category`, `field`);
  `error_dict()` is the envelope's `error`. `legacy_kit.py` keeps only legacy names for kit
  helpers (deleted in F4).
- A failed catalogue call reaches the bridge as `KernelError`: the legacy keys (`code`,
  `message`, `details.mutation_outcome`) plus the full error envelope in `details.envelope`.
- Sheet quantities and `view` rollback: see the F1b notes below. `session` failures report
  `unknown`.

**F1b as built (kernel features for the domain migrations).**

- **Sheet units (gate 9).** A tool whose signature uses `SheetLength` or `SheetPoint` (derived
  `spec.sheet`) gets its target sheet from the arguments, in this order: the first reference
  argument that is a `drawing_sheet` or a `drawing_view` (the sheet showing it,
  `services.sheet_of`); for a tool that creates a sheet, its top-level `units` argument; the
  work part's current sheet; else part units. `SheetLength` values and unit strings (`"10 mm"`,
  `"0.5 in"`) are converted to that sheet's units; `SheetPoint` stays plain numbers in sheet
  units. The handler gets `tx.sheet` (the sheet object or None) and
  `tx.units = {"length", "angle", "sheet_length"}`; the envelope's `units` carries
  `sheet_length`. NXOpen placement points still take part units: convert with
  `kit.drawing.Sheet.point`. An unresolvable reference never fails gate 9; the handler reports
  it. Annotation, dimension and table references do not name a sheet yet: the drawing domain
  adds them to `services.sheet_of` (host) or passes `sheet`/`view` explicitly.
- **View rollback (`effect="view"`).** Before changing the display a handler records what it
  will change: `tx.display.keep(objs, appearance=True)` (blank state, colour, transparency;
  bodies expand to faces), `tx.display.keep_camera()`, `tx.display.keep_highlights()`. A failure
  restores what was kept (outcome `rolled_back`); a failed restore is
  `rollback_failed/NX_RESTORE_FAILED`, outcome `partial`; a touched handler that kept nothing
  reports `unknown`. With `@tool(..., restorable=True)` (view tools only) a success returns
  `restore_id`, a legacy display snapshot that now also holds the camera, so legacy
  `nx_restore_display` restores it (and the display domain's `nx_restore_display`). `model`
  failures also put kept display state back after the undo. The services call the legacy
  `VisualToolsMixin` code (`_display_pairs`, `_apply_display_record`, `_set_highlights`,
  `_save_display_snapshot(records, camera)`) and `_view_info`/`_restore_camera`.
- **Part ownership (gate 6).** Every call, catalogue or legacy, owns the parts it loaded
  (created, or opened when not already loaded); an owned part keeps ownership when the agent
  saves it under a new path. Ownership is keyed by part key plus file path, so a reused NX tag
  never inherits it. The engineer grants the work part with the panel checkbox **Allow agent to
  change <part>** (`dispatch.grant_state`/`set_grant`, production only). `NX_PART_NOT_GRANTED`
  (`engineer_control`, `retry: ask_engineer`) carries a hint telling the agent to ask the
  engineer in chat. During migration `dispatch.run_legacy` applies the same rule to legacy
  tools in production: model tools check the work part and undo a change to a foreign part;
  `nx_save_part`, `nx_save_as` and `nx_close_part` check their part (after the workspace check);
  reads, legacy view tools (`dispatch.LEGACY_VIEW`) and file exports are not gated.
  `NX_MCP_DEV=1` turns it all off. **Domain agents:** `file` and `session` tools that save or
  close a specific part call `tx.require_owned(part, field="/part")` first; the kernel checks
  only the work part of `model` tools.
- **Long operations.** `tx.checkpoint(done, total)` writes progress to the receipt, honours
  `nx_cancel_operation` and enforces the cooperative budget per cost class (instant/light 30 s,
  heavy 300 s, long 1800 s; unknown 300 s, measured from gate 9). Over budget raises
  `interrupted/NX_CANCELLED` with `details.reason = "budget"`, `budget_seconds`,
  `elapsed_seconds` and a hint to split the call; a `model` tool rolls back. Handlers that loop
  over many objects or native calls call `tx.checkpoint()` between them. The panel's running
  line says "NX may pause" only for `heavy`/`long` catalogue tools.
- **Panel and activity log.** Catalogue calls show `spec.summary` (with the sheet's units for
  sheet tools) while waiting, running and in the history; the created object's name comes
  from `data.<feature|sketch|body|...>` or `changes.created`. A failed catalogue call shows
  the model state from `mutation_outcome` (`not_applicable` = "model unchanged"); `read` and
  `view` catalogue steps are grouped with read-only steps.

**F3 as built (deviations and additions).**

- `sidecar/`: `schema.py` (core types → Pydantic `TypeAdapter` + JSON Schema, parity-tested
  against `core.validate`), `proxy.py` (gates 1-3, 14, 15 in S), `gateways.py`, `legacy.py`
  (legacy pseudo-specs), `server.py` (`install()` replaces `tools/list` and `tools/call` on the
  FastMCP server that still registers the legacy tools). There is no separate `artifacts.py` or
  `http.py` yet: the artifact resource, result store and compact presentation stay in
  `agent_surface.py` and `http_surface.py` serves both profiles, reused until F4.
- `agent` lists the 4 gateways plus the core tools that exist in the catalogue (today only
  `nx_list_objects`); `full` lists gateways, catalogue tools and every legacy tool. The former
  helpers `nx_result`, `nx_inspect` and `nx_result_cleanup` are replaced by the sidecar catalogue
  tools `nx_get_result` and `nx_delete_results` (`domains/session/results.py`), which read and
  prune both snapshot folders (`agent-results`, `bridge-results`).
  `nx_discover_tools` and the 13-entry agent surface are gone; `scripts/validate_agent_ux.py`
  still calls `nx_discover_tools` and needs `nx_find_tools` + `nx_get_tool_schemas`.
- Legacy tools stay callable directly (both profiles) and through both gateways; discovery rows
  mark them `legacy`, rank them below catalogue tools (an exact name still wins) and omit their
  guessed domain. Their results keep the legacy shape. A catalogue or gateway name equal to a
  legacy name raises `CatalogueError` at sidecar start.
- `operation_id` and `intent` are listed only on directly listed non-read catalogue tools;
  `nx_get_tool_schemas` omits them (the gateway carries `intent`, `operation_id` goes inside
  `arguments`). Optional `= None` fields are documented by omission (no `null` branch).
- The sidecar evidence gate and discovery filter use the build from the bridge descriptor;
  without a descriptor the gate is left to NX. `NX_MCP_DEV=1` in the sidecar shows and runs
  untested tools (marked `tested: false`); the acceptance runner starts its sidecar that way.
- Any `unknown` outcome gets `retry: after_status_check` and a hint naming
  `nx_operation_status` with the `operation_id` (a `long` tool outlasting the bridge wait).
- Acceptance: steps run through `nx_invoke` (catalogue read tools through `nx_invoke_read`) with
  `detail=full`; legacy results are unwrapped to `ok`/`error`/`mutation_outcome`. `$part` is
  `smoke/<run-id>/<domain>_<name>_<run>.prt` (NX refuses a second loaded part with the same
  basename). After each scenario, also on failure, the runner closes every part it loaded without
  saving (`"cleanup": "save"|"keep"` overrides) and re-activates the earlier display/work part.
  Evidence per run is `acceptance/evidence/<build>/<domain>/<name>.json` (a failed run writes
  `<name>.last-failure.json`); `fold` rebuilds `src/nx_mcp/evidence/*.json` from passing,
  non-stale runs only. `nx_status` reports only `v2406`, so `nx_build` is that key until the host
  reports the full release. The runner reads the commit from `.git` files (no git process).
- Budgets (`tests/sidecar/test_budget.py`): agent listing, a 10-row discovery page and the
  per-tool definition caps are enforced as written. The 700 median applies from 10 catalogue
  tools; `nx_get_tool_schemas` may add at most 200 characters to a tool's definition (900 per
  tool once definitions meet the median). The `full` listing's 110,000 cap applies to the
  generated part; the legacy part may not grow beyond today's 218,951 characters.

**After wave 1 as built (core fixes and the domain agents' additive core changes).**

- **Compact schemas.** Listings and `nx_get_tool_schemas` leave out the regular expressions
  of references (`REF_PATTERN`) and unit strings: gate 1 checks them (Pydantic in the
  sidecar, `core.validate` in NX). A reference is `{type: string, maxLength: 128,
  description: "Ref: <kinds>"}`; a quantity is `{type: [number, string], maxLength: 40}`
  with the unit in its description (`QUANTITY_MAX` = 40 is enforced by both validators). A
  subschema used more than once moves to `$defs` when that is shorter (greedy, named after
  the core type: `Plane`, `Limit`, `Point3`, `Length`, ...; otherwise by JSON type); a
  field keeps its own description and default next to its `$ref`. A tool's own pattern
  (`Name`, a `Text` pattern) stays listed. `operation_id`/`intent` carry no description; the
  server instructions explain them. A test asserts that the listed schema accepts every
  call gate 1 accepts. `evidence: {build: true}` means every variant is tested.
- **Budget measurement.** The per-tool caps and the 700 median measure the tool's own
  definition (name, description, input schema without the 87 characters of call keys, as
  `nx_get_tool_schemas` returns it); the full listing's 110,000 cap counts the call keys.
  The 2,500 cap also applies to a tool whose argument is a union of two or more object
  forms (`Limit`, `Axis`, `linear | circular`), the discriminated unions of surface 1.7. At
  106 catalogue tools: median 673 (751 with call keys), largest 2,317; the wave-1 tools
  shrank by 27 %. The full listing's catalogue part is 97,032 characters: at this rate
  152 tools would exceed its 110,000 cap, so new tools must stay near the median.
- **Gate 1.** A required field never accepts `null` (only an optional `= None` field does).
  A union reports the best-matching form: the form whose discriminator (`Literal` field)
  the value names, then the one knowing most of its keys, then the deepest error. When the
  value's discriminator names no form, the error is at the union and lists the accepted
  values (`type 'until_face' is not accepted here; expected one of 'distance', ...`).
- **Gate 7 before gate 2.** With a caller's `operation_id`, `Receipts.replay` runs right
  after gate 1: repeating a finished call with the same arguments replays its envelope
  instead of failing on what it created (`NX_ALREADY_EXISTS` for a `new` path);
  different arguments are `NX_IDEMPOTENCY_CONFLICT`, a running one `NX_OUTCOME_UNKNOWN`.
- **Gate 6, `new_part`.** `@tool(new_part="<param>")` names a model tool's `new` `.prt`
  path parameter; when it is given the call works in a part it creates, so the work part
  (possibly the engineer's) is not checked before the handler. The after-handler
  `IsModified` check still undoes a change to a part the agent does not own.
  `nx_import_model(target=new_part)` uses it.
- **Files.** `Files.publish` never overwrites: every target is checked first, then each
  file is linked into place with `core.tx.publish_new` (`os.link`, exclusive create and
  copy where hard links fail); `kit.files.publish_new` is the same function. The kernel
  removes `.nx-mcp/staging/<operation_id>/` when a top-level call ends (committed or not;
  this also clears `kit.files.temp_path` leftovers), and `staging` itself once empty.
- **Inherited update errors (gate 11).** After NX refuses a feature and the kernel rolls
  it back, NX 2406 attaches the refused feature's update error to a feature created later
  (the internal datum `SKETCH(n:1B)` of the next sketch, the next chamfer). Updating that
  feature drops the stale message (`_drop_inherited_feature_errors`), but NX does not
  update features while a sketch is active, and `nx_create_sketch` leaves its sketch
  active, so the sketch failed once with the old message. `LegacyServices.feature_errors`
  now deactivates the active sketch with a model update, re-reads the errors and activates
  the sketch again; a real error survives that update and still fails the call. Live:
  `acceptance/modeling/blend.json` creates a sketch right after the refused blend
  (committed, model healthy).
- **Registry.** `ObjectRegistry.register` refreshes the stored object on an identity hit,
  so a tag NX reuses after an undo inside one call never resolves to the deleted object.
- **Gate 13.** The kernel runs `services.refresh_annotations(spec)` (legacy, now only when
  the part has legacy measured sheet-metal callouts) and then every `@refresher` hook
  (`services.refreshers()`; the host returns all registered hooks, services without NX
  none), inside the call's mark; a refresher failure rolls the call back.
  `nx_list_objects(kind)` covers every kind with a `@lister` (a test keeps `ListKind` and
  the registered listers equal; a domain adding a lister adds its kind). `nx_delete_objects`
  (session domain, not migrated yet) will dispatch to the `@deleter` hooks. The host's
  snapshot reports drawing tables (parts lists, bend tables, tabular notes, title blocks)
  as `drawing_table` in `changes`.
- **Additive changes kept.** `sidecar/local.py` runs `runs="sidecar"` tools through the
  same kernel over `SidecarServices` (no NX, receipts in the shared operation store, gate 2
  on the sidecar's workspace). The proxy attaches an inline PNG for `data.image = {path,
  sha256, inline: true}` (hash re-checked, 8 MiB cap, otherwise a warning) and a
  `nx-artifact://workspace/...` resource link for a result naming `data.path` with
  `data.sha256`. `tx.checks()` marks refusals raised before the first change as
  `not_started`; the kernel accepts that only when no file was published and, for a model
  tool, the snapshot is unchanged (a misplaced `checks()` still rolls back).
  `Services.forget_parts(keys)` drops the references, ownership, undo history and
  checkpoints of parts a tool closed. `host/dispatch.legacy_ids` maps catalogue short refs
  inside legacy arguments to their legacy IDs (until F4). Hook kind `sheet`
  (`@sheet_locator`) maps annotations, dimensions and tables to their sheet for gate 9.
- **Capability matrix.** `scripts/generate_capability_matrix.py` lists every catalogue tool
  with its acceptance evidence per build and variant (`src/nx_mcp/evidence/`), then the
  remaining legacy manifest tools; the pre-commit check also runs on evidence and domain
  changes.

F4 deletes:

- `hardened.py` and its 27 mixin modules, and `nx_bridge.NXOpenExecutor`;
- `integration_server.py`, `certified.py`, `contracts.py`, `schema_types.py`, the
  `*_server.py` stubs and `feature_groups.py`;
- `request_summary.py`, `output_schemas.py`, `agent_guidance.py`, `agent_surface.py`;
- `manifest.py`, `capability_manifest.json`, `capability_fragments/`;
- `side_effects.py` and `docs/features/AGENT_BRIEF.md`.
