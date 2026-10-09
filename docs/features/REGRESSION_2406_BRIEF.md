# NX 2406 regression brief (wave 2: every remaining tool)

Goal: no tool is "experimental" on NX 2406. Each assigned tool must end as
either **PASS** (proven working on the live NX 2406, with numbers) or
**DISABLED on 2406** (it cannot work on this NX version, for a concrete
reason you proved, such as a missing NXOpen API or a missing license). A broken
tool must never stay callable. Fixing it is always preferred over disabling it.

Read `docs/features/AGENT_BRIEF.md` (repository rules, NX lock protocol, dev
scripts) and `docs/features/VERIFIER_BRIEF.md` (how to verify a tool: schema,
adversarial options, model unchanged after rejection, panel summary). Both
apply, except for the file rules below. Use `scratchpad/<your-name>/` for your
step files and `smoke/<your-name>/` for your NX parts. Look at
`acceptance/validate_*.py`, `tests/` and `docs/` for the 2606 evidence of how
each tool is meant to be used.

## Per tool
1. Find its handler (`grep -rn "def _<tool without nx_>" src/nx_mcp`), schema
   and description (`nx_discover_tools` with `include_schema`).
2. Exercise every documented option on the live NX 2406 through
   `scripts/dev_call.py` and check results numerically (volumes, bounding
   boxes, read-back geometry, inventories, exported file sizes, PNG/PDF
   produced). Check that rejected calls leave the model unchanged and return
   a stable error code, never a raw `NX_API_ERROR` for bad input.
3. On failure, find the 2406 cause (`scripts/dev_python.py` to inspect the
   real NXOpen API) and fix it so that the tool works on 2406 and stays
   correct on 2606 (do not delete 2606 paths; branch on `self.nx_version`
   only when the APIs really differ). Add a regression test, reload, re-verify.
4. Only one option is impossible on 2406? Refuse just that option on 2406
   with `NXToolError("NX_UNAVAILABLE_ON_VERSION", ...)` and say so in the
   description. The whole tool is impossible on 2406? Mark it DISABLED in your
   results file (the executor then refuses it on 2406 before touching NX).

## Files
- You may edit any `src/nx_mcp/*.py` and `tests/*.py` needed for your tools.
  Other agents edit the same shared files at the same time: make small,
  targeted Edit-tool changes only; never rewrite or reformat a whole existing
  file; if an Edit fails because the file changed, re-read it and retry. Run
  `ruff check` / `ruff format` only on files you changed.
- Do NOT edit `src/nx_mcp/capability_manifest.json`, CLAUDE.md, README.md or
  `scripts/validate_agent_ux.py`. Record evidence in your results file:
  `docs/features/regression-2406/<your-name>.json`:
  `{"nx_tool": {"verdict": "pass" | "disabled", "evidence": "numbers and
  cases", "reason": "why it cannot work on 2406 (disabled only)", "fixes":
  ["file: what changed"]}}` with one entry for EVERY assigned tool.
- Run the full unit suite before you finish:
  `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp-<your-name>`.
  If it fails in code you did not touch, re-run once (another agent may be
  mid-edit) and report it.
- No git commands. Do not close, save or modify parts you did not create.
- `nx_ui_control`: never leave control with the engineer. If the agent loses
  control and cannot get it back, release the lock and report at once.

## Final report (under 450 words)
Counts PASS / DISABLED; per tool one line (verdict + key evidence); bugs fixed
(file, test); every DISABLED reason; shared problems you saw but did not fix.
