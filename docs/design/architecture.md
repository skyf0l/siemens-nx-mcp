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
               receipts, evidence
kit/           shared NX helpers: geom, args, units, builders, select, sketch, drawing, expr,
               readback, result
domains/       session, parts_files, sketch, modeling, freeform, assembly, inspection, drawing,
               sheet_metal, display: one package each, one @tool function per tool
host/          NX side: dispatch (bridge call -> kernel), services (NXServices)
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
| `Doc(text)` | field description, ≤ 80 chars | `description` |
| `Range(ge, le, gt, lt)` | finite numeric bounds | `minimum`, `maximum`, ... |
| `Items(min, max)` | list length; required on every list | `minItems`, `maxItems` |
| `Text(max, pattern)` | string bounds | `maxLength`, `pattern` |
| `RefKind(*kinds)` | accepted reference kinds | description (gate 1 checks the pattern; section 4) |
| `Quantity(dim)` | `length`, `sheet_length`, `angle` or `count`; allows unit strings and NX expression names or formulas | `[number, string]`, `maxLength` 80 |
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
| `description` | docstring | first sentence ≤ 120 chars, whole text ≤ 300 |
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
- `@refresher(kind)` for `nx_update_annotations` and for the kernel's refresh.

**Import-time validation.** A violation raises `CatalogueError`: the sidecar refuses to
start, and `dev_reload.py` prints the error. The checks:

- name, verb and uniqueness, and the budgets in surface 1.7;
- known `effect` and `requires` values;
- `tx` first, keyword-only parameters built from core types, `Items` on every list, and
  defaults that validate;
- summary placeholders that name real parameters, and `variants` naming a `Literal` field;
- `batchable` implies `model` with `cost` ≤ `light`; `previewable` implies `model`;
- no `new`, `save` or `replace` path on a `read` tool;
- known hook kinds.

### 1.3 Other `core/` modules

| Module | Responsibility |
|---|---|
| `errors.py` | Table of 22 categories and 67 codes (surface 1.8), each with `retry` and a default `hint`. `fail(code, message, *, field=None, hint=None, details=None, nx_code=None, retry=None)` raises `NXError` (a subclass of `NXToolError`). `native(codes=None, default="NX_ERROR")` context manager (below) |
| `kernel.py` | `run(spec, args, ctx)` and `run_child(spec, args, parent_tx)`, the gate order of section 3. Nothing else applies gates |
| `tx.py` | `Tx`, the handler's only route to NX and to the services (3.2) |
| `references.py` | short refs, kinds, anchors, invalidation |
| `receipts.py` | Receipts (successor of `recovery.OperationStore`): fingerprint, idempotent replay, states, outcome, progress, `.cancel` flag. A receipt keeps the request's `arguments`, redacted (`redact`: file contents and long strings become size notes, lists keep 100 items, at most 8,000 characters), which `nx_get_operation` echoes, and a development failure's `traceback`. Running receipts from an earlier session become `unknown`. Also holds the undo history for `nx_undo_operation` and checkpoints |
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
  `.nx-mcp/refs.json` at host start, so a ref from an earlier session or reload fails as
  `stale_reference` and never aliases another object.
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
  (`NXServices.revalidate`) instead of dropping them, and expires the undo history and
  checkpoints (`expired_reason: engineer_change`).
- Part close and host reload invalidate the refs in their scope; a closed part's
  checkpoints stay listed as expired (`part_closed`).

### 1.4 `kit/`: shared NX know-how, never copied

| Module | API (`tx` first wherever NX is touched) |
|---|---|
| `geom` | pure math: `add`, `sub`, `dot`, `cross`, `norm`, `unit(v, field)`, `tol(*pts)`, `parallel`, `same_direction`, `on_plane`, `plane_through(pts, field)`, `orthonormal_frame`, `matvec`, `matmul`, `transpose`, `xyz(nx_point)`, `rows(nx_matrix)` |
| `args` | cross-field rules the schema cannot express: `need(**fields)`, `exactly_one(**fields)`, `distinct(field, values)`. Each raises `invalid_argument` with a JSON-pointer `field` |
| `units` | `part_units(tx)` → `"mm"`/`"in"`, `sheet_units(tx, sheet)`, `factor(source, target)`, `say(value, units)` → `"10 mm"`. The spelling `"inch"` does not exist |
| `builders` | `with builders.build(tx, factory, *a) as b:` then `b.commit()` (`CommitFeature` or `Commit`). `Destroy()` always runs, and rejections pass through `native(default="NX_BUILD_FAILED")`. Also `point(tx, p)`, `direction(tx, v, origin)`, `collector(tx, refs, "Edge"/"Face"/"Body")` |
| `select` | `planar(tx, ref)` → `(obj, origin, normal)` for a planar face or datum plane of the work part. `plane`, `axis`, `direction(tx, value)` resolve the union types. `bodies`, `faces`, `edges(tx, refs)`, `owned_by_work_part(tx, obj)` |
| `sketch` | `with sketch.editing(tx, ref) as sk:` activates the sketch and restores the previous state, with `sk.frame`, `sk.to_part(p2)` and `sk.to_local(p3)`. `sketch.solve(tx, sk)` returns status and DOF, or raises `solver_failed`. `sketch.owns(tx, sk, curve)` is the one ownership check |
| `drawing` | `with drawing.on_sheet(tx, sheet) as sh:` activates the sheet and restores the previous sheet and display afterwards (it does not switch or restore the NX application; open). `sh.units`, `sh.point(SheetPoint)`, `drawing.view(tx, ref)`, `drawing.refresh(tx, views)`. `visible_edges(tx, view)` and `edge_in_view` give the model edges a view draws in sheet coordinates; `annotation_box` (an annotation's sheet bounds) and `crowding` (views it overlaps, off-sheet) |
| `explode` | `shown(tx)`: the explosion shown in the work view of the displayed work assembly, else None; `assembled_note(tx)`: the note that NX measures assembled (unexploded) poses while one is shown |
| `expr` | `create(tx, name, formula, units)`, `set_formula`, `value`, `alias(feature, "distance")` (semantic parameter names), `bind` |
| `readback` | `expect(ok, check, **actual)`, `expect_close(actual, wanted, check, tol=None)` (scalar or vector, scaled tolerance), `expect_same_direction`, `expect_on_plane`. Each raises `verification_failed` with `details.check` |
| `result` | `rows(tx, objs, kind)`, `page(rows, page)`, `measured(value, "length")`, `rename(tx, obj, name)` (returns the name as NX normalised it) |

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
| NX hosts | `interactive.py`: UI-thread host (timer tick, Win32 panel with the two modes (Auto, Ask me first) and the "Allow agent to change <part>" grant, holds); `activity.py`: panel history and `.nx-mcp/activity.log`; `nx_bridge.py`: batch lifecycle. Bootstraps: `nx_app/startup/nx_mcp_startup.py` (2406), `nx_app/journals/start_2606.py`, `nx_app/journals/start_batch.py` |
| Bridge | `bridge.py`: descriptor, authenticated loopback server and client (protocol, token and the 1 MiB limit); `result_transport.py`: bounded receipts with pageable full results |
| `sidecar/` | `server.py`: `create_server()`/`install()`, profile from `NX_MCP_SURFACE`. `agent` (default) lists 4 gateways + 14 core tools; `full` lists the gateways and every visible catalogue tool, with `readOnlyHint`/`destructiveHint` from `effect`. `schema.py`: core types -> Pydantic and JSON Schema. `proxy.py`: validates, resolves paths and calls the bridge. `local.py`: `runs="sidecar"` tools. `gateways.py`: `nx_find_tools` (ranked, filtered by evidence for the descriptor's build), `nx_get_tool_schemas`, `nx_invoke`, `nx_invoke_read`. `artifacts.py`: result snapshots and the `nx-artifact://` resource. `http_surface.py` (top level): optional HTTP transport |
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
| 2 | Workspace paths | S (`Workspace.resolve`), N (`ensure_inside` + mode) · C | Checked from `spec.paths`. A path without a suffix gets the marker's first one (`parts/plate` → `parts/plate.prt`). `.nx-mcp` is reserved; `new` must not exist, `read` must exist, `save` may write only the part's own file, and `replace` may be an existing file (never a directory) that the tool replaces atomically only when asked (`Files.stage(path, replace=True)`). An existing `new` or `save` path fails `NX_ALREADY_EXISTS` with `details.suggested_path` (the first free `<stem>-<n>`). In a ref\|path union a ref-shaped value (`p:3`) is a ref and is not resolved as a path (`proxy._takes_refs`) |
| 3 | Variant inference, evidence/version | S (build from the descriptor), N · C | N first fills an omitted variant of a `variant_from` tool from the referenced object and checks that variant's `required_by` fields. The tool and the value of its variant field must be tested on the connected build. On an unknown build only `read` and `view` tools run. A refusal is `unavailable/NX_VERSION_UNSUPPORTED` and names the variants available here. Listings show only what runs: untested variants leave the schema enum, `tool#variant` schemas and `nx_find_tools`, and a tool with no variant left is hidden; before NX connects listings assume the default build (v2406) and say so, and a change sends `tools/list_changed`. `NX_MCP_DEV=1` still offers untested variants, marked. A tested tool/variant that failed with `NX_LICENSE_UNAVAILABLE` is remembered for the NX session: refused before NX ("remembered from <time>") and hidden via `nx_get_status` `bridge.license_unavailable` until NX restarts |
| 4 | `requires` | N · C | Read-only state checks (`NX_NO_WORK_PART`, `NX_SKETCH_ACTIVE`, `NX_WRONG_APPLICATION`, ...) whose hint names the tool to run. They run before the hold, so the engineer is never asked to approve a request that cannot succeed, and again after it |
| 5 | Engineer control hold | N (host) | Auto: each call takes NX's main window for itself only (no `UI.LockAccess`) and gives it back; reads never wait, other calls wait silently while the engineer is in an NX command or dialog (at most 120 s, then `NX_UI_BUSY`), and a non-read call is refused with `NX_MODAL_DIALOG_OPEN` (not started) while a modal NX dialog is open. No request card in Auto, long steps included. Ask me first: every call waits on the panel card (`spec.summary` and the agent's `intent`); **Run once** or **Run & switch to Auto** runs it, **Not now** returns `NX_CONTROL_DECLINED`, and 60 s without an answer returns `NX_CONTROL_PAUSED`. Only `nx_get_status` skips the hold (it reports the mode), and batch children inherit the parent's call. Only the engineer changes the mode; no tool does |
| 6 | Part ownership | N · C | In production, `model` and `file` effects and `nx_close_part` may change only parts the agent created, opened when they were not already loaded, or was granted by the engineer on the panel. The work part is checked first. After the handler, any non-owned part whose `IsModified` flipped forces a rollback. Error: `engineer_control/NX_PART_NOT_GRANTED`. Off with `NX_MCP_DEV=1` |
| 7 | Receipt | N | Every non-`read` effect gets a fingerprint of tool plus canonical arguments. The same id with the same arguments replays the committed result (`replayed: true`); different arguments give `NX_IDEMPOTENCY_CONFLICT`. Children write progress into the parent's receipt |
| 8 | Undo mark | N | `model` only: a visible mark `NX MCP: <label>`. Children share the parent's mark; a preview's mark is always undone |
| 9 | Unit conversion | N · C | `Quantity` fields become floats in the target part's or sheet's units, then canonical forms replace their alternatives (an axis-angle `Pose` becomes `{translation, rotation}`). Gate 1 already rejected wrong dimensions, so this gate cannot fail on arguments |
| 10 | Handler | N · C | `handler(tx, **args)` |
| 11 | Read-back | N · C | The handler verifies with `kit.readback`. For `model` tools the kernel also turns a snapshot diff into `changes`, drops inherited NX 2406 feature errors, and treats a new feature in error as `geometry_failed/NX_UPDATE_FAILED` with rollback. The same applies to every other part the handler declared with `tx.touch_part` (`kernel._readback_parts`) |
| 12 | Reference invalidation | N · C | After a commit: refs of objects NX no longer has. After a rollback: refs of objects the undo did not restore (1.3) |
| 13 | Annotation refresh | N | If `refresh_annotations` is set, the `@refresher` hooks run inside the same undo mark, and what they refresh is added to `changes.modified` |
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
| `long` | translators, PDF, assembly-wide checks | may outlast the 120 s bridge wait. The sidecar then returns `unknown` with `retry: after_status_check`, and the agent polls `nx_get_operation` |

Handlers that loop call `tx.checkpoint()` between native calls, and the kernel calls it
between batch children. Each checkpoint:

1. writes progress to the receipt (`nx_get_status` reads it from the worker-thread snapshot
   without queueing);
2. honours `nx_cancel_operation` by raising `NX_CANCELLED`, which rolls back a `model` tool;
3. enforces the cost class's cooperative budget (light 30 s, heavy 300 s, long 1800 s): over
   budget raises `NX_CANCELLED` with `details.reason = "budget"`.

A single native call cannot be interrupted, and the host never pumps NX's main loop.

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
- **Receipts (gate 7 before gate 2).** Repeating a finished call with the same `operation_id`
  and arguments replays its envelope; different arguments are `NX_IDEMPOTENCY_CONFLICT`, a
  running one `NX_OUTCOME_UNKNOWN`.
- **Sheet units (gate 9)** come from the referenced sheet or view, a new sheet's `units`, the
  current sheet, else part units; handlers get `tx.sheet` and `tx.units`.
- **View rollback.** `view` handlers record what they change with `tx.display.keep*`; a failure
  restores it. `restorable=True` returns a `restore_id`.
- **Long operations.** `tx.checkpoint(done, total)` reports progress, honours cancellation and
  enforces the budget per cost class (light 30 s, heavy 300 s, long 1800 s).
- **Compact schemas.** Listings omit reference and unit patterns (gate 1 checks them); repeated
  subschemas move to `$defs`. A variant's required fields reach `nx_get_tool_schemas` as the
  compact `required_by` map (1.2), not as top-level `anyOf` branches. Budgets are enforced by
  `tests/sidecar/test_budget.py`.
- **No NX prompts.** `host/dispatch.execute` runs every call under `services.quiet()`
  (`host/quiet.py`): NX's default update-failure action is `Undo` (NX undoes a failed update,
  code 670024 -> `NX_UPDATE_FAILED`, the kernel rolls back; never "Error During Update") and
  listing-window output goes to a file and into the result; the engineer's settings return
  after the call. `host/dialogs.py` counts only modal dialogs (disabled owner); in development
  mode a new one answers the running call at once with `NX_MODAL_DIALOG_OPEN`. A failed
  `nx_dev_python` snippet is undone and the display and work parts it started with return.
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
     15 minutes.
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
   - Every array has `maxItems`; every string has `maxLength` or is an enum.
   - Numbers are finite, and there are no `additionalProperties`.
   - The tool definition is ≤ 1,200 characters, or ≤ 2,500 for discriminated unions.
4. **Evidence present.** v2406 evidence covers every variant. A `source_sha256` mismatch is a
   local warning and a release-build failure.
5. **Handler signature matches.** `tx` comes first, the other parameters are keyword-only with
   core-type annotations, and the returned keys match the `TypedDict`.
6. **Codes.** Every code reachable through `fail` or `native` (found by an AST scan) is in the
   table.
7. **Imports.** The domain module imports with NXOpen blocked and contains no `import NXOpen`.

**Token budgets.** `tests/sidecar/test_budget.py` measures these at 3.8 characters per token
and fails above the cap (`-s` prints the measured values).

| Item | Cap | Measured |
|---|---|---|
| Full listing (name + description + input schema, call keys included) | 1,040 chars per listed tool on average | 166 tools (162 + 4 gateways), 159,006 chars, mean 957.9 |
| Tool definition (without call keys) | median 700 chars; each ≤ 1,200, or ≤ 2,500 for unions | median 692.5, max 2,431 |
| `agent` profile listing | 15,000 chars (about 4K tokens) | 14,906 chars |
| `nx_find_tools` page of 10 rows | 1,400 chars | at most 1,371 |
| `nx_get_tool_schemas` | each entry at most 200 chars over its definition; 900 chars per tool on average plus the mean definition's excess over 700 | 1,055 chars per tool |
| Compact result before `result_id` paging | 4,000 chars | |
