# Feature-group development brief (parallel agents)

You implement one feature group of the NX MCP server and validate it live on
the single running Siemens NX 2406. Read `CLAUDE.md` first (conventions and
"Live NX development loop"), then the reference implementation
`src/nx_mcp/modeling_server.py` + `src/nx_mcp/datum_features.py` and the test
`tests/test_datum_features.py`.

## Your files only
Edit ONLY the files named in your task. Other agents edit the other groups in
the same checkout at the same time. Never edit shared files (hardened.py,
integration_server.py, request_summary.py, capability_manifest.json, README,
CLAUDE.md, other groups' files). If you need a shared helper, copy it into
your module. If a shared change is truly required, describe it in your final
report instead. No git commands at all (no add/commit/stash/checkout/restore).
Do not install packages. Never touch `.nx-mcp/dev-exit` or `.nx-mcp/ui-stop`.

## How a tool is defined
- Schema: a function `nx_<name>(...)` in your `<group>_server.py`. Its
  signature is the MCP input schema (use `Literal[...]` for choices, `list[float]`
  for vectors, `str` for object IDs) and its docstring is the agent-facing
  description: exact semantics, units (lengths in work-part units, angles in
  degrees, sheet coordinates for drawings), what is returned, limits.
- Handler: method `_<name>` (name without `nx_`) on your mixin, with exactly
  the same parameter names and defaults (tests/test_feature_groups.py checks).
- `READ_ONLY` (never changes NX state), `NON_MODEL` (changes views/files, not
  geometry) and `SUMMARIES` ({tool: lambda p, c: "one line"} for the NX panel;
  see request_summary.py for the context helpers c.obj/c.count/c.length/c.path).
- Executor helpers you can call: `self._resolve(id, {kinds})`,
  `self._reference(obj, kind, part, label)`, `self._work_part()`,
  `self._units()`, `self._sketch_frame(sketch)`, `self._engineering_owned(id, kind)`,
  `self._engineering_collector(objects, "Edge"|"Face"|"Body")`,
  `self._engineering_result(feature)`, `self.nxopen`, `self.session`.
  The executor wraps every mutation in an undo mark and rolls back on any
  exception; you do not manage undo yourself.
- Rules: validate all arguments BEFORE touching NXOpen (raise
  `NXToolError("NX_INVALID_ARGUMENT", ...)`); builders: create -> configure ->
  commit -> `Destroy()` in `finally`; read back what NX built and raise
  `NX_VERIFICATION_FAILED` on mismatch; use stable codes (NX_INVALID_ARGUMENT,
  NX_NOT_FOUND, NX_OBJECT_TYPE_MISMATCH, NX_OBJECT_OWNER_MISMATCH,
  NX_NON_PLANAR, NX_VERIFICATION_FAILED, NX_UNSUPPORTED_ARGUMENT).
  Import NXOpen submodules inside handlers, after validation.

## Local tests
`tests/test_<group>.py`: argument validation through `rig.e.execute(...)`
(the `rig` fixture), pure helpers, and schema checks. Run only:
`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_<group>.py tests/test_feature_groups.py --basetemp .pytest-tmp-<group>`
Lint/format ONLY your own files: `.venv/Scripts/python.exe -m ruff check <files>`
and `ruff format <files>`.

## Live NX (one NX for everyone: always hold the lock)
```
.venv/Scripts/python.exe scripts/dev_lock.py acquire --owner <group>   # waits
.venv/Scripts/python.exe scripts/dev_reload.py --wait-agent           # loads ALL code from disk
.venv/Scripts/python.exe scripts/dev_python.py "print(dir(NXOpen.Features.XBuilder))"   # inspect real APIs
.venv/Scripts/python.exe scripts/dev_call.py steps.json               # drive your tools via MCP
.venv/Scripts/python.exe scripts/dev_lock.py release --owner <group>
```
- Keep each locked session under ~15 minutes; release between sessions (others
  wait). Re-acquire refreshes the lock. Object IDs reset at every reload.
- If dev_reload fails because ANOTHER group's file does not import, release the
  lock, wait a few minutes, retry. Never fix their file.
- Use `dev_python.py` to discover real builder members/enums before guessing.
- Work in your own scratch parts: `smoke/<group>/...prt` (nx_create_part). You
  may modify anything in NX, but do not close or save other agents' parts.
- Verify numerically: nx_measure_volume, nx_get_bounding_box, nx_list_topology,
  nx_measure_distance/angle, nx_sketch_info, nx_list_dimensions,
  nx_list_annotations, nx_feature_parameters. For drawings also nx_screenshot
  (look at the PNG with the Read tool) or nx_export_drawing_pdf.
- dev_call.py step format: `{"tool", "args", "intent", "save": label,
  "mutation": false for read-only, "expect_error": true}`; reference earlier
  results with "$label.path.to.id" (e.g. "$ext.feature.id", "$sk.object.id").
- Save a runnable end-to-end live test (starting with nx_create_part of a new
  part) as `acceptance/live/<group>.json`.

## Evidence and docs
- `src/nx_mcp/capability_fragments/<group>.json`: `{tool: {"status",
  "evidence_type", "scope"}}`. Only tools that passed live verification get
  `"status": "tested", "evidence_type": "real_NX_v2406_scoped"` and a scope that
  says exactly what was verified (numbers). Others: `"experimental"`,
  `"local_contract_tests"`, scope says why.
- `docs/features/<group>.md`: per tool purpose, arguments, example, verified
  scope, known limits.

## Final report (under 400 words)
Tools and status (live-verified with evidence, or not and why), files written,
NX API quirks learned, any shared change you need from the orchestrator.
