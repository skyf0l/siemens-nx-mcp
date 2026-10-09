# NX MCP code architecture: catalogue, kernel, kit, domains

Status: proposal for review, companion to [tool-surface.md](tool-surface.md) (names,
arguments, results, errors, profiles). This document says where code lives, how one call is
processed and how the code base migrates. Section 5 replaces `docs/features/AGENT_BRIEF.md`
for all new work.

**Problem.** One tool is spread over 9-12 places that must agree. About 17 name lists
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
  `# nx: threaded`, ticked by a UI-thread timer, never holding NX's main loop.
- Undo/rollback, workspace checks on both process boundaries, version and evidence gating,
  and license mapping stay at least as strong as today.
- There is no automatic hand-back of control, and all behaviour stays tested.

---

## 1. Package layout (`src/nx_mcp`)

```text
core/          NX-free: types, validate, catalogue, summary, errors, kernel, tx, references,
               receipts, evidence
kit/           shared NX helpers: geom, args, units, builders, select, sketch, drawing, expr,
               readback, result
domains/       session, parts_files, sketch, modeling, freeform, assembly, inspection, drawing,
               sheet_metal, display: one package each, one @tool function per tool
host/          NX side: startup, interactive host, panel, activity log, dispatch, nxservices
transport/     authenticated loopback bridge, descriptor, MainThreadDispatcher, result store
sidecar/       MCP server: profiles, schema generation, proxy, gateways, artifacts, HTTP
acceptance/    scenario runner and evidence writer
evidence/      <domain>.json, written only by the acceptance runner
interactive.py shim re-exporting host.startup, so the NX bootstrap never changes
```

Import rules are enforced by an AST test:

- `core` imports nothing else from the package, and `kit` imports only `core`.
- `domains/<d>` imports only `core`, `kit` and its own package.
- Only `host` and `transport` may `import NXOpen`.
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
| `RefKind(*kinds)` | accepted reference kinds | `pattern` + description |
| `Quantity(dim)` | `length`, `sheet_length` or `angle`; allows unit strings | number or unit string |
| `PathArg(mode, suffixes)` | workspace path; mode `read`, `new`, `dir` or `save` | `string` |

The aliases follow surface 1.4: `Ref`, `RefTo(*kinds)`, `Point2`, `SheetPoint`, `Point3`,
`Vec3`, `Mat3`, `Pose`, `Length`, `SheetLength`, `Angle`, `Units = Literal["mm", "in"]`,
`Plane`, `Axis`, `Direction`, `SketchPoint`, `Limit`, `Combine`, `Page`, `Name`, `PathIn`,
`PathOut`.

- Unions are written with `|`: `Plane = RefTo("datum_plane", "face") | PrincipalPlane |
  PlaneByNormal`.
- A `TypedDict` sets `__pydantic_config__ = {"extra": "forbid"}` as a plain dict, so no
  pydantic import is needed.
- Fixed vectors are `tuple[float, float, float]`, which gives `minItems == maxItems`.

**Unit rule.**

- A plain number is in part units for `Length`, sheet units for `SheetLength`, and degrees
  for `Angle`.
- A string `"<number> <unit>"` is converted explicitly. Lengths use the pattern
  `^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\s*(mm|in)\s*$`; angles use the same with
  `deg|rad`. A wrong dimension (`"30 deg"` for a `Length`) therefore fails schema
  validation.
- Points, vectors and matrices take plain numbers only.
- The kernel converts at gate 9 (section 3), so handlers see only floats in part units and
  degrees. Results always state `units`.

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
| `invalidates` | decorator; default `"topology"` | `"topology"`: face and edge refs of touched parts go stale. `"deleted"`: only deleted objects (datums, annotations, display) |
| `cost` | decorator | `instant`, `light`, `heavy` or `long` (3.3) |
| `runs` | decorator; default `"nx"` | `"sidecar"` for workspace and result tools that never reach NX |
| `example` | decorator | one minimal argument set, returned by `nx_get_tool_schemas` |
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
- no `new` or `save` path on a `read` tool;
- known hook kinds.

### 1.3 Other `core/` modules

| Module | Responsibility |
|---|---|
| `errors.py` | Table of 22 categories and 65 codes (surface 1.8), each with `retry` and a default `hint`. `fail(code, message, *, field=None, hint=None, details=None, nx_code=None)` raises `NXError`, which subclasses the legacy `NXToolError` (one hierarchy until F4). `native(codes=None, default="NX_ERROR")` context manager (below) |
| `kernel.py` | `run(spec, args, ctx)` and `run_child(spec, args, parent_tx)`, the gate order of section 3. Nothing else applies gates |
| `tx.py` | `Tx`, the handler's only route to NX and to the services (3.2) |
| `references.py` | short refs, kinds, anchors, invalidation |
| `receipts.py` | Receipts (successor of `recovery.OperationStore`): fingerprint, idempotent replay, states, outcome, progress, `.cancel` flag. Running receipts from an earlier session become `unknown`. Also holds the undo history for `nx_undo_operation` and checkpoints |
| `evidence.py` | Loads `evidence/*.json`. `available(spec, build, variant)`, `known_build(build)`, discovery filter, writer API for the runner |

**`native()` mapping.**

- `ErrorCode` 948801 or 949895 → `unavailable/NX_LICENSE_UNAVAILABLE`.
- A code listed in `codes` → its stable code.
- Anything else → `nx_error/NX_ERROR`.
- `nx_code` and the native message are always kept.
- A Python exception → `NX_ERROR` with its type name. A traceback is included only with
  `NX_MCP_DEV=1`.

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
  The kernel never re-resolves silently.
- During migration, `references.py` wraps the legacy `ObjectRegistry`, so the `obj_...` and
  short spellings of a ref both resolve.

**Invalidation.**

- After every commit, refs to deleted objects go stale. With `invalidates="topology"` the
  face and edge refs of touched parts also go stale.
- After a rollback, every loaded part's refs go stale, because NX undo is session-wide. The
  exception is a before/after snapshot that proves nothing structural changed
  (`references: preserved`).
- Part close, engineer take-back and host reload invalidate the refs in their scope.

### 1.4 `kit/`: shared NX know-how, never copied

| Module | API (`tx` first wherever NX is touched) |
|---|---|
| `geom` | pure math: `add`, `sub`, `dot`, `cross`, `norm`, `unit(v, field)`, `tol(*pts)`, `parallel`, `same_direction`, `on_plane`, `plane_through(pts, field)`, `orthonormal_frame`, `matvec`, `matmul`, `transpose`, `xyz(nx_point)`, `rows(nx_matrix)` |
| `args` | cross-field rules the schema cannot express: `need(**fields)`, `exactly_one(**fields)`, `distinct(field, values)`. Each raises `invalid_argument` with a JSON-pointer `field` |
| `units` | `part_units(tx)` → `"mm"`/`"in"`, `sheet_units(tx, sheet)`, `convert(value, dim, to)`, `say(value, units)` → `"10 mm"`. The spelling `"inch"` does not exist |
| `builders` | `with builders.build(tx, factory, *a) as b:` then `b.commit()` (`CommitFeature` or `Commit`). `Destroy()` always runs, and rejections pass through `native(default="NX_BUILD_FAILED")`. Also `point(tx, p)`, `direction(tx, v, origin)`, `collector(tx, refs, "Edge"/"Face"/"Body")` |
| `select` | `planar(tx, ref)` → `(obj, origin, normal)` for a planar face or datum plane of the work part. `plane`, `axis`, `direction(tx, value)` resolve the union types. `bodies`, `faces`, `edges(tx, refs)`, `owned_by_work_part(tx, obj)` |
| `sketch` | `with sketch.editing(tx, ref) as sk:` activates the sketch and restores the previous state, with `sk.frame`, `sk.to_part(p2)` and `sk.to_local(p3)`. `sketch.solve(tx, sk)` returns status and DOF, or raises `solver_failed`. `sketch.owns(tx, sk, curve)` is the one ownership check |
| `drawing` | `with drawing.on_sheet(tx, sheet) as sh:` activates the sheet and restores the previous sheet and display afterwards (it does not switch or restore the NX application; open). `sh.units`, `sh.point(SheetPoint)`, `drawing.view(tx, ref)`, `drawing.refresh(tx, views)` |
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
| `host/` (NX) | `startup.py`: `start()`, `import_nxopen_namespaces()`; the `nx_mcp/interactive.py` shim keeps the bootstrap untouched. `interactive.py`: host, tick, control, holds. `panel.py`: Win32 panel plus a new "Allow agent to change <part>" grant toggle. `activity.py`. `dispatch.py`: catalogue tools go to the kernel, the rest to legacy. `nxservices.py`: work part, snapshots, undo marks, update-error cleanup (today's `NXOpenExecutor` core) |
| `transport/` | `bridge.py`: descriptor, server, dispatcher and client; protocol, token and the 1 MiB limit are unchanged. `results.py`: one snapshot store, `.nx-mcp/results`, for bridge and sidecar |
| `sidecar/` | `server.py`: `create_server(profile)`. `agent` (default) lists 4 gateways + 14 core tools. `full` lists all tools, with `readOnlyHint`/`destructiveHint` from `effect`. `schema.py`: core types → Pydantic and JSON Schema. `proxy.py`: one generic proxy that validates, resolves paths and calls the bridge, or runs `runs="sidecar"` tools locally. `gateways.py`: `nx_find_tools` (ranked over name, keywords, label and description, filtered by evidence for the build in the bridge descriptor), `nx_get_tool_schemas`, `nx_invoke`, `nx_invoke_read`. `artifacts.py`, `http.py` |
| `acceptance/` | `runner.py` replays `acceptance/<domain>/*.json` through a real stdio MCP client (the production path). Fixtures in `acceptance/fixtures/` are saved in NX 2406 and copied to `smoke/<agent>/`. Every `expect` is machine-checked. A passing run writes `src/nx_mcp/evidence/<domain>.json` for the connected build |

---

## 2. Worked example: `nx_create_datum_plane`

Today this tool lives in `modeling_server.nx_datum_plane`, `DatumFeaturesMixin._datum_plane`,
a `request_summary` template, the manifest and copied vector helpers. In the new form it is one
function, one test and one scenario. The legacy `flip` argument is dropped: a negative distance
goes the other way.

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
    reference: RefTo("datum_plane", "face") | None = None,
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
`matches`. An `error.*` path asserts a failure. During migration, a `"setup": true` step may still
call a legacy tool (section 4).

---

## 3. The kernel: one gate order for every call

### 3.1 Gate order

**S** marks a gate that runs in the sidecar and **N** one that runs on the NX side. **C** marks a
gate repeated for every child of `nx_run_batch` and `nx_preview_changes`. A failure before gate 7
always reports `not_started`.

| # | Gate | Where | Rule |
|---|---|---|---|
| 1 | Schema validation | S (Pydantic), N (`validate.py`) · C | Rejects an unknown tool, extra fields, out-of-bounds values and unit strings of the wrong dimension. The sidecar checks batch children against the child spec, which must be `batchable` or `previewable`. `"$2.data.feature"` placeholders are allowed only in Ref fields |
| 2 | Workspace paths | S (`Workspace.resolve`), N (`ensure_inside` + mode) · C | Checked from `spec.paths`. `.nx-mcp` is reserved; `new` must not exist, `read` must exist, and `save` may write only the part's own file |
| 3 | Evidence/version | S (build from the descriptor), N · C | The tool and the value of its variant field must be tested on the connected build. On an unknown build only `read` and `view` tools run. A refusal is `unavailable/NX_VERSION_UNSUPPORTED`, and the tool is hidden from discovery |
| 4 | `requires` | N · C | Read-only state checks (`NX_NO_WORK_PART`, `NX_SKETCH_ACTIVE`, `NX_WRONG_APPLICATION`, ...) whose hint names the tool to run. They run before the hold, so the engineer is never asked to approve a request that cannot succeed, and again after it |
| 5 | Engineer control hold | N (host) | While the engineer has control, the request waits; the panel card shows `spec.summary` and the agent's `intent`. **Give control** runs it, **Not now** returns `NX_CONTROL_DECLINED`, and 60 s without an answer returns `NX_CONTROL_PAUSED`. Only `nx_get_status` and `nx_set_control(mode=manual)` skip the hold, and batch children inherit the parent's control. Control changes only by the engineer's click or the agent's explicit `nx_set_control(manual)`, so agents may hold it for hours. One exception: a host fault that cannot restore the UI lock releases NX, and the panel says so |
| 6 | Part ownership | N · C | In production, `model` and `file` effects and `nx_close_part` may change only parts the agent created, opened when they were not already loaded, or was granted by the engineer on the panel. The work part is checked first. After the handler, any non-owned part whose `IsModified` flipped forces a rollback. Error: `engineer_control/NX_PART_NOT_GRANTED`. Off with `NX_MCP_DEV=1` |
| 7 | Receipt | N | Every non-`read` effect gets a fingerprint of tool plus canonical arguments. The same id with the same arguments replays the committed result (`replayed: true`); different arguments give `NX_IDEMPOTENCY_CONFLICT`. Children write progress into the parent's receipt |
| 8 | Undo mark | N | `model` only: a visible mark `NX MCP: <label>`. Children share the parent's mark; a preview's mark is always undone |
| 9 | Unit conversion | N · C | `Quantity` fields become floats in the target part's or sheet's units. Gate 1 already rejected wrong dimensions, so this gate cannot fail on arguments |
| 10 | Handler | N · C | `handler(tx, **args)` |
| 11 | Read-back | N · C | The handler verifies with `kit.readback`. For `model` tools the kernel also turns a snapshot diff into `changes`, drops inherited NX 2406 feature errors, and treats a new feature in error as `geometry_failed/NX_UPDATE_FAILED` with rollback |
| 12 | Reference invalidation | N · C | After a commit: touched parts, as `spec.invalidates` says. After a rollback: all loaded parts (1.3) |
| 13 | Annotation refresh | N | If `refresh_annotations` is set, the `@refresher` hooks run inside the same undo mark, and what they refresh is added to `changes.modified` |
| 14 | Result projection | N (envelope), S (compaction) | The surface 1.6 envelope with `units`, `frame`, `changes` and `warnings`. With `detail=compact`, arrays over 20 items go to the result store |
| 15 | Error mapping | N, S | `NXError` becomes `{category, code, message, hint, field, retry, nx_code, details}`; `retry` comes from the category table |

### 3.2 `Tx` and the outcome rule

Members of `Tx`:

- `tx.nx`, `tx.uf`: the NXOpen modules. `tx.session`, `tx.part` (work part): mark the
  transaction *touched* when accessed. `tx.units`: `{"length": "mm", "angle": "deg"}`.
- `tx.resolve(ref, *kinds)`: the live object, or `stale_reference`, `wrong_kind` or
  `wrong_owner`. `tx.ref(obj, kind=None)`: a result row with its anchor.
- `tx.warn(code, msg)` and `tx.checkpoint(done=None, total=None)` (3.3).
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

Once the legacy executor is gone and the AST rule proves that domains reach NX only through
`tx`, Tx tracking replaces `side_effects.py`.

### 3.3 Long operations

| Cost | Typical tools | Kernel behaviour |
|---|---|---|
| `instant` | registry and state reads | none |
| `light` | sketch curves, datums, annotations | allowed in a batch |
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

## 4. Coexistence and migration

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
  effects get the after-handler `IsModified` check. Legacy `nx_create_part` and newly opened
  `nx_open_part` record ownership (`dispatch.after_legacy`). The panel grant is not built yet
  (`services.own(part)` exists).
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
- Sheet quantities use part units until the drawing domain supplies `services.sheet_units`.
  `view` and `session` failures report `unknown` until display snapshots are wired.

F4 deletes:

- `hardened.py` and its 27 mixin modules, and `nx_bridge.NXOpenExecutor`;
- `integration_server.py`, `certified.py`, `contracts.py`, `schema_types.py`, the
  `*_server.py` stubs and `feature_groups.py`;
- `request_summary.py`, `output_schemas.py`, `agent_guidance.py`, `agent_surface.py`;
- `manifest.py`, `capability_manifest.json`, `capability_fragments/`;
- `side_effects.py` and `docs/features/AGENT_BRIEF.md`.

---

## 5. Rules for agents (replaces `docs/features/AGENT_BRIEF.md`)

1. **Your scope.** One domain package per agent: `src/nx_mcp/domains/<d>/`,
   `tests/unit/<d>/` and `acceptance/<d>/`. Only the runner writes `evidence/<d>.json`. Never
   edit another domain; the one exception is legacy `setup` steps (section 4).
2. **Never copy a helper.** Code that two domains need belongs in `kit/`. Add it there with a
   test, in a kit module no other agent is editing at the time, or ask the orchestrator.
   `core/`, `host/`, `sidecar/` and `transport/` belong to the orchestrator: describe the change
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
     `scripts/dev_reload.py --wait-agent`, then `dev_lock.py release`. Keep each session under
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
tests/contract/       per-tool contract test, import/AST rules, no-legacy-list-entry check
tests/sidecar/        profiles, gateways, schema generation, proxy, artifacts
tests/acceptance/     runner and expect operators against a fake bridge
tests/legacy/         today's tests, deleted per domain as tools migrate
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

**Token budgets.** A catalogue test measures these at 3.8 characters per token and fails above
the cap.

| Item | Cap |
|---|---|
| Full listing, 152 tools | 110,000 chars (about 29K tokens) |
| Median tool definition | 700 chars |
| `agent` profile listing | 15,000 chars (about 4K tokens) |
| `nx_find_tools` page of 10 rows | 1,400 chars |
| `nx_get_tool_schemas` | 900 chars per tool on average |
| Compact result before `result_id` paging | 4,000 chars |
