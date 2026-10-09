# Independent verification brief

You did NOT write the code you verify. Your job is to prove, on the live NX
2406, that each assigned tool behaves exactly as its schema and description
promise, and to find what its author missed. Read `docs/features/AGENT_BRIEF.md`
for the repository rules, the NX lock protocol and the dev scripts; they all
apply. Use your own scratch folder (scratchpad/<your-name>/) and your own NX
parts under `smoke/verify_<your-name>/`. Always hold the NX lock for any NX
command and release it after each session (sessions under ~15 minutes).

## For every assigned tool
1. Schema: read it as an agent sees it (`scripts/dev_call.py` with
   `nx_discover_tools` `{"query": "<tool>", "include_schema": true}`). Check
   required vs optional arguments, enum values, defaults, that the description
   states units, return fields and limits correctly, and that `nx_invoke` with
   the documented minimal arguments works. Read-only tools must not demand an
   operation_id; mutations must accept one.
2. Replay: run the author's `acceptance/live/<group>.json` end to end on a fresh
   part (rename the part path in a copy under your scratch folder; never edit
   the original file unless it is wrong).
3. Adversarial: for EVERY argument option (each enum value, each optional
   argument, boundaries, invalid values, wrong object kinds, IDs from another
   part, stale IDs) check the result numerically (volumes, bounding boxes,
   read-back geometry, dimension values, nx_list_* inventories). Confirm that
   rejected calls leave the model unchanged (volume, feature list,
   nx_model_health) and return a stable error code. Check that the NX panel
   summary for the tool reads correctly (`from nx_mcp.request_summary import summarize`).
4. Verdict per tool: PASS (with your evidence numbers) or FAIL (exact repro).

## Fixing
You may fix bugs in your assigned groups' own files (the
`<group>_server.py`, `<group>.py`, `tests/test_<group>.py`, capability fragment,
docs/features/<group>.md, acceptance/live/<group>.json) - their authors are
finished. Add a regression test for each fix and re-verify live. Do not edit
any other file; report shared-file problems instead. If a tool cannot be made
to work, set its capability fragment entry to "experimental" with the reason.
No git commands.

## Final report (under 450 words)
Per tool: PASS/FAIL with key evidence; bugs found and fixed (file, test);
schema/description corrections; open problems needing a shared-file change.
