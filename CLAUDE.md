# CLAUDE.md

## Project

NX MCP is a two-process Siemens NX automation server. The sidecar
(`python -m nx_mcp.server`) owns stdio, schemas, validation and workspace paths and
imports without NX. The NX-side host owns NXOpen and the session; they talk over an
authenticated loopback bridge. Design of record: `docs/design/architecture.md`
(section 5 = agent rules) and `docs/design/tool-surface.md`.

## Layout (`src/nx_mcp`)

- `core/`: NX-free. `catalogue.py` (`@tool`, `ToolSpec`, registry and its rules),
  `types.py`, `validate.py`, `kernel.py` (one gate order for every call), `tx.py`,
  `errors.py`, `references.py`, `receipts.py`, `evidence.py`, `summary.py`; several
  parts: `target.py` (gate 3t), `switching.py` (gate 4s), `marks.py`, `busy.py`,
  `parking.py`, `undoable.py` (what an undo can compensate).
- `kit/`: shared NX know-how, one module per subject (architecture.md 1.4): analysis_objects,
  args, bodies, builders, capture, curve, deform, display, drawing, explode, expr, facts,
  features, files, geom, kinematics, licenses, massprops, measure, model, parts, pose,
  positioning, readback, records, result, select, selftest, sketch, snapshots, surface,
  switching, units.
- `domains/<domain>/`: every public tool, one `@tool` function each (session,
  parts_files, sketch, modeling, freeform, assembly, motion, inspection, drawing,
  sheet_metal, display, optimization).
- `host/dispatch.py` -> `core/kernel.py`, with `host/services.py` (`NXServices`),
  `host/quiet.py` (no NX prompts), `host/dialogs.py` (modal dialogs) and
  `host/heartbeat.py` (NX-free: the sidecar's view of a busy or hung NX).
  Hosts: `nx_app/startup/nx_mcp_startup.py` (startup app, any NX build; `nx_app/journals/start.py`
  plays the same bootstrap, `start_2406.py`/`start_2606.py` are shims for it),
  `nx_app/journals/start_batch.py` (batch, `nx_bridge.py`); `interactive.py` is the
  UI-thread host and panel. Workspace: `NX_MCP_WORKSPACE`, else `<home>/NX_MCP_WORKSPACE`
  (`workspace.default_workspace()`); NX and the sidecar must use the same one.
- `sidecar/`: `server.py` generates the `agent` profile (default: 4 gateways + 14 core
  tools, 18 in all) and `full` (gateways + every visible catalogue tool); `proxy.py`,
  `gateways.py`, `schema.py`, `local.py`, `artifacts.py`.
- `acceptance/`: scenario runner; `evidence/<domain>.json` is written only by it.
- Tests: `tests/kit/` (the shared fake NX), `tests/core/`, `tests/unit/<domain>/`,
  `tests/sidecar/`, `tests/host/`, `tests/acceptance/`.

## Adding or changing a tool

1. One `@tool(...)` function in `src/nx_mcp/domains/<d>/`: `tx` first, keyword-only
   arguments typed with `core/types`, docstring = description; `label`, `summary`,
   `keywords`, `effect`, `requires` in the decorator. No name lists or manifests.
2. Validate arguments before touching `tx.session`/`tx.part`; build with
   `kit.builders.build` (create -> configure -> commit -> `Destroy()` always), verify
   with `kit.readback`; raise only via `errors.fail` or `native()` (keeps `nx_code`).
3. Unit test in `tests/unit/<d>/` on `tests/kit/` (it runs the real kernel).
4. Acceptance scenario `acceptance/<d>/<name>.json` covering every variant with
   numeric `expect` checks; run it on live NX, then `python -m nx_mcp.acceptance fold`.
5. Regenerate `docs/capability-matrix.md`, `docs/tools.md` and the budget table in
   `docs/design/architecture.md` (`scripts/generate_capability_matrix.py`,
   `scripts/generate_tool_reference.py`, `scripts/generate_budget_table.py`); never edit
   them by hand. Budgets (`tests/sidecar/test_budget.py`) catch bloat, never a rule an
   agent needs: description 500 characters (first sentence 120), field 120, tool
   definition 1,600 (3,000 for unions).

## Agent rules

- One domain per agent: `domains/<d>/`, `tests/unit/<d>/`, `acceptance/<d>/`.
  `core/`, `host/`, `sidecar/` and the bridge belong to the orchestrator: report
  the change you need.
- Never copy a helper: code two domains need goes into `kit/` with a test.
- Never import NXOpen outside the NX-side host modules; the sidecar must import
  without NX.
- Pass opaque references, never display names, where NX needs an object.
- Every model mutation runs under the kernel's undo mark and rolls back on failure.
- Files go through the workspace checks on both process boundaries.
- Live NX: hold the dev lock, use disposable parts under `<workspace>/smoke/<agent>/`,
  close the parts you created when done. Never modify, save or close parts you did
  not create, and never touch `.nx-mcp/ui-stop` or `.nx-mcp/dev-exit`. `NX_MCP_DEV=1`
  keeps the part-ownership gate on: catalogue tools refuse the engineer's parts
  (`NX_PART_NOT_GRANTED`, `NX_ENGINEER_PART`) unless they ticked the panel checkbox;
  `nx_dev_python` acts outside the gate, so never point it at their parts.
- Panel modes: Auto (default, also in development) and Ask me first. Only the engineer
  changes the mode (no tool does); never click the panel or NX dialogs.
- No git commands or package installs unless asked; lint only your own files.

## NX host (2406 tested, any build)

NX runs the host as the startup application `nx_app/startup/nx_mcp_startup.py`;
its first line `# nx: threaded` keeps it loaded for the session and a UI-thread timer
ticks the host. Keep that line first. In an NX started without `nx_app`, play
`nx_app/journals/start.py` (`start_2406.py` and `start_2606.py` run it): also
`# nx: threaded` first (NX then runs it in its main interpreter, which outlives the
journal; without the line it runs in a sub-interpreter unloaded on return). It prints
the NX build (tested or not), Python (3.10+ required, older refused) and missing NXOpen
namespaces, then calls the same bootstrap's `play()`, which only reports "already
running" when the startup app or an earlier play hosts the bridge, in this interpreter
or another (a process-wide named marker: never two hosts in one NX).
The bootstrap imports the NXOpen namespaces tools use once at start
(`interactive.NXOPEN_NAMESPACES`); a tool needing another namespace asks the orchestrator
to add it. Never hold NX's main loop in a long-running journal: NX work that needs the loop
(first Modeling start, first PDF export) then freezes NX (the old resident 2406
journals did exactly that and were removed).

## Live NX development loop

```powershell
python scripts/dev_start_nx.py --user-dir nx_app --env NX_MCP_DEV=1   # start NX (found, or --nx-root; cwd <workspace>/.nx-mcp/nx-cwd)
python scripts/dev_lock.py acquire --owner <agent>                    # before any NX use
python scripts/dev_lock.py refresh --owner <agent>                    # while you hold it (or acquire again)
python scripts/dev_reload.py --owner <agent> --wait-ready  # reload host code; exit 1 prints the traceback
python -m nx_mcp.acceptance run <domain>[/<name>] --owner <agent>     # replay scenarios (holds the lock)
python -m nx_mcp.acceptance run <domain> --beside-engineer --owner <agent>  # with engineer parts loaded
python -m nx_mcp.acceptance call --inline '[...]' --owner <agent>     # ad-hoc calls, no evidence
python scripts/dev_python.py --owner <agent> "print(dir(NXOpen.Features.HoleFeatureBuilder))"  # probe NXOpen
python -m nx_mcp.acceptance verify [<domain>...] --owner <agent>  # a volunteer's run on another NX build
python -m nx_mcp.acceptance fold                 # rebuild src/nx_mcp/evidence/*.json
python scripts/dev_lock.py release --owner <agent>
```

- The lock: `run` and `call` hold it for their whole duration (every scenario, every
  step), refresh it every 30 s and release it at the end, also on an error or Ctrl+C;
  a lock you held before stays yours. A lock not refreshed for 10 minutes is stale: the
  next acquire takes it over and prints whose it was. Holding it by hand (dev_reload,
  dev_python), run `dev_lock.py refresh` at least every few minutes. A waiting
  `acquire` gives up (exit 3) when a process that started it exits.
- One owner, one caller: every `run` and `call` with the same `--owner` presents the
  caller id `dev-<owner>` to NX, so parts one call created are yours in the next
  (close them without `force`). A part the engineer allowed on the panel stays allowed
  across `dev_reload` (same NX process, tag and file).
- Failure records (`*.last-failure.json`) keep error codes, check paths and numbers
  only; a run NX refused a licence in writes no evidence (`fold` rewrites older ones).
  `verify` is `run` for volunteers on another NX build: no `--beside-engineer`, licence
  refusals skipped, then a fold and the evidence files to contribute;
  `python -m nx_mcp.report` writes their field report.
- `dev_reload.py` and `dev_python.py` run only for the lock holder (`--owner`, exit 3
  otherwise, NX untouched); `--force-unlocked` (a warning) is for the orchestrator when no
  agent uses NX.
- `dev_python.py` probes create or open their own part under `smoke/<agent>/` first
  and never work in the parts that happen to be loaded: with no work part a probe is
  refused (NXOpen calls on a missing part open NX's blocking "Null tag not allowed"
  alert); `--no-part` is for a snippet that creates or opens its part itself. A probe
  stops after `--timeout` seconds (default 120, between Python lines; one blocking NX
  call cannot be interrupted).

Changes to `nx_app/startup/nx_mcp_startup.py` need an NX restart. Keep sessions
under 15 minutes. `scripts/dev_nx_windows.py` shows blocking NX dialogs; close a
hung NX with `taskkill //IM ugraf.exe //F`. Check results with measurements,
`nx_check_model` and `nx_capture_view`.

## Quality commands

```powershell
python -m pip install -e ".[dev]"
python -m pre_commit install --install-hooks
python -m pre_commit run --all-files     # ruff, format, generated-doc checks, pytest, mypy
python -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
python -m mypy src/nx_mcp
```

Fake-NX tests verify only the simulated NXOpen seam; `real_nx` acceptance on the
dedicated NX runner is the release gate.
