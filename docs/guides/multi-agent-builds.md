# Multi-agent builds in one NX

How to orchestrate several agents that build one product (for example a machine made of
rotors with hundreds of blades, cases, shafts and housings) in one shared NX session
through the NX MCP.

All agents share one NX main thread. A call from one agent waits in NX's queue behind every
other agent's call. One slow call slows every agent, and one hung call stops them all.

## 1. Spec first

Write a `SPEC.md` in the project folder before any agent starts. Every brief points to it.

- **One frame for everything.** Model every part in the product frame (for example X = main
  axis, Z up, mm, X = 0 at a named plane). The assembly then places every component at
  the origin with identity orientation, and joints need no positioning work.
- **An interface table with an owner per interface.** Each row gives the interface (flange,
  bearing seat, shaft spline, case joint), its position and radii, and the one part that owns
  it. List static interfaces as well as rotating ones: an interface without an owner tends
  to be added late or left as a gap. Give the required gap for each neighbour pair.
- **Check the numbers in Python before launch.** Compute each part's mass from its own
  dimensions (a tube's OD, ID and length give its mass) and compare with its budget. Check
  that blade chords and row gaps fit the axial length, and that parts which must not overlap
  do not. A wrong budget or an impossible fit makes builders shrink geometry or report
  a false over-budget part.
- **Reduction rules up front.** If very high counts (thousands of airfoils) may be too slow,
  say what an agent may reduce (which row, minimum fraction) and that it must report it.

## 2. Concurrency

- **2-3 builders per NX.** With more, even light calls can wait minutes in the queue.
  Start the next builder when one finishes.
- **At most 3 NX calls in flight across all agents.** A builder that fires 6-9 calls at
  once makes each of them wait behind the others (minutes for a light call) and delays every
  other agent. Send calls one after another, or a few at a time.
- **No host reload or NX MCP update while builders run.** A reload refuses the calls that
  arrive meanwhile. Short refs (`b:123`) are re-bound afterwards while NX still has their
  objects; a ref that fails `NX_STALE_REFERENCE` must be listed again.
- **One heavy call at a time across all agents.** Announce heavy calls in the briefs (section
  3) and have the orchestrator grant them in turn, or give heavy checks to one agent at the end.
- **Save before any verification.** A save queued behind a hung call never runs; work done
  since the last save is then lost at the restart. Save after each major feature and always
  before `nx_check_model`, `nx_get_model_summary`, interference or mass checks.
- **Never run heavy reads in parallel on one body.** `nx_check_model`, bounds and mass on the
  same large body at once can hold NX for a long time. Run them one after another, light
  ones first.
- **Split very large patterned bodies.** Uniting every patterned row into one growing body
  makes each pattern and unite slower than the last and gives one body of many thousands
  of faces, on which health checks can run for a long time. Keep one body per blade row
  (or per stage) and let the assembly carry them.
- Pass `"part": "<file name>"` in every call without refs once two parts are loaded, core
  tools included. Never save, close or modify another agent's part.
- Relay interface facts between neighbours as soon as one side is built (the exact flange
  radii and faces), and record the measured gaps.

## 3. Heavy calls and a busy NX

Heavy calls, which can hold NX for minutes:

- `nx_check_model` with `clash: true` on bodies with many faces, lofted faces most of all
  (the self-overlap check takes up to an hour on one large solid; by default it skips
  solids over 2,000 faces and reports `complete: false`), and `nx_get_model_summary` or
  bounds and mass on very large assemblies;
- patterns of hundreds of instances, and unites of hundreds of bodies (unite in batches of
  at most 50 tools and check the volume after each);
- interference checks and motion sweeps on large assemblies, and STEP export of the whole
  assembly.

**Busy or dead?** `nx_get_status` answers within seconds while NX runs a step, hangs or is
gone: `operation.tool` and `operation.elapsed_seconds` show the running call, and when NX
itself cannot answer, `bridge.state` says `busy` (computing: wait), `blocked_by_dialog`
(the engineer must answer a dialog), `unresponsive` or `down`, with `bridge.health.hint`
saying what to do. A busy NX keeps the same `bridge.session_id` when it answers again; a
new `session_id` means NX was restarted. A heavy call can hold NX for most of an hour and
NX then answers again. Meanwhile reads give up after 30 s in the queue, and once one call
has held NX unresponsive for 2 minutes other calls fail at once with `NX_BUSY` (nothing ran).

**While waiting:**

- Poll `nx_get_status` at most once a minute. Send no other NX calls.
- Never re-send a heavy read after it timed out or hung: the first one is still queued or
  running, and a second one doubles the wait.
- After a timeout or `unknown` outcome, query `nx_get_operation` with its operation_id before
  retrying a mutation.
- Do light offline work (layout scripts, the report draft) meanwhile.

**Before declaring work lost**, check the `.prt` file timestamps in the workspace and the
`nx_save_part` operation's outcome. Parts saved before the hang reopen unchanged.

**Before any restart** (the orchestrator decides, never a builder): tell every agent to stop
calling NX, wait until each one confirms it has stopped, check which parts are saved, then
restart. After the restart, tell each agent exactly which of its parts are on disk.

**Watchdog:** while builders run, the orchestrator checks `nx_get_status` every few minutes
instead of waiting idle for reports. A builder that loses the bridge reports it at once.

## 4. Tool facts to state in every brief

- **Catalogue tools run only through `nx_invoke` (mutations) or `nx_invoke_read` (reads).**
  `nx_set_camera`, `nx_activate_part`, `nx_save_part` and most others are not MCP tools of
  their own; calling `mcp__nx__nx_set_camera` fails. Only the core tools listed by the server
  are direct MCP tools. Name tools in briefs as "`nx_set_camera` via `nx_invoke`".
- **Load the core tools agents need upfront**, including `nx_get_result`,
  `nx_undo_operation`, `nx_find_geometry` and `nx_get_operation`, so no agent stalls looking
  for them.
- **Do not assume numpy.** Neither NX's Python nor the agent's shell Python may have it:
  compute coordinates with the standard library (`math`) in a script, never by hand.
- **Undo is per part.** Once another part changed after your operation, `nx_undo_operation`
  and `nx_restore_checkpoint` compensate instead of using NX's undo: they delete what your
  operations created (features, sketches, curves, points) and set back values whose tools
  kept them (expressions, attributes, names, materials, poses). A delete or another edit
  in between blocks them (`NX_CROSS_PART`; `nx_list_checkpoints` shows `restorable` and
  `blocked_by`), and saves expire undo marks. Then fix forward: remove a bad feature with
  `nx_delete_objects` or `nx_set_feature_suppression`.
- **Leave no reference curves in a sketch that a feature uses.** An extra open curve becomes an
  extra body or a misleading error. Use a separate sketch for construction geometry.
- **Check unite results.** After a unite of many bodies, the volume must be close to target
  plus tools minus their overlap; a section (`nx_measure_section`) shows a missing wall.
- **Re-measure after rework.** A mass or clearance measured before a change is stale. Report
  only values measured on the final geometry, and re-run interference checks after any
  rebuild of a part that touches a neighbour.
- **Check each target of a combine.** After a subtract or intersect with several targets,
  measure every target (volume or a key distance): read the result's warnings, which name
  a target the tools missed. After a pattern, measure one copy.
- **Pass features, not guessed ids.** A pattern feature passed as `bodies` stands for every
  body it made; never build body lists by counting ids (`b:%d`), which breaks as soon as NX
  numbers objects differently.
- A sealed-void warning on a part that is hollow by design is a fact to report, not geometry
  to change.

## 5. The engineer's live view

- **Copy the engineer's display rules into every brief word for word**; a paraphrased rule
  is a weaker rule.
- **One agent owns the view.** Every display or camera call waits in the shared queue: with
  four builders each refreshing the view after every step, view calls alone cost tens of
  minutes. Unless the engineer asks otherwise, one agent (the orchestrator or a builder by
  turns) displays the part in work and fits the camera at milestones or on a timer; the
  other builders work on their parts with the `part` key and never move the view.
- Hide datums before fitting a part that sits far from the origin, or the fit includes them.
- During integration one agent owns the display: the assembly, fit on the whole product.

## 6. Final reports

- Every number comes from a tool result in this session, not from a layout script or the spec.
  Say which tool measured it.
- List what was not checked and why (a part skipped by a heavy check, a pair without a
  clearance test, an export that failed). "All tests passed" only when every listed test ran.
- **Say "sampled" wherever a check was sampled** (3 blades of a row, nearest bodies instead
  of whole parts), and report a partial `nx_check_model` (`complete: false`) as partial.
- **Decide verdicts after measuring.** A test brief lists the checks and their limits, never
  their expected outcome.
- Report deviations from the spec (reduced counts, shortened chords, simplified airfoils,
  gaps below the required value) and over-budget masses.
- Count incidents accurately (how many freezes, who restarted NX, what was rebuilt).
- When asked "is it realistic?", separate what was modelled from what a real product has.
- **Integration by a fresh agent.** Start the integration or test agent with a brief file
  (spec, parts, interfaces, measured facts so far) rather than resuming a builder whose
  context is already large.

## Brief template

Fill the `<...>` fields; keep the engineer's rules verbatim.

```text
You are one of <N> agents building <product> in a LIVE NX session through the `nx` MCP
tools. An engineer watches the NX window. Load the tools first with ToolSearch
"select:mcp__nx__nx_get_status,mcp__nx__nx_find_tools,mcp__nx__nx_get_tool_schemas,
mcp__nx__nx_invoke,mcp__nx__nx_invoke_read,mcp__nx__nx_get_operation,mcp__nx__nx_get_result,
mcp__nx__nx_list_parts,mcp__nx__nx_find_geometry,mcp__nx__nx_measure_distance,
mcp__nx__nx_undo_operation,mcp__nx__nx_capture_view,mcp__nx__nx_check_model".

FIRST read the spec: <workspace>/projects/<project>/SPEC.md. It is binding (frame,
interface table, budgets). Tool paths are relative to the workspace root.

YOUR SCOPE: <part files>. You own these interfaces: <rows of the interface table>.
Neighbours: <part: owner agent, facts known so far>.

ENGINEER'S RULES (verbatim): "<...>"

Tools:
- Find tools with nx_find_tools (several queries at once), read args with
  nx_get_tool_schemas, call catalogue tools ONLY via nx_invoke (nx_invoke_read for reads).
  nx_set_camera, nx_activate_part, nx_save_part are NOT MCP tools.
- Compute every coordinate with a Python script (standard library; do not assume numpy).
- Pass "part": "<your file>" in every call without refs. Never save, close or modify a part
  you did not create.
- Undo after other agents' work only removes what you created and sets values back; when
  it refuses (NX_CROSS_PART), remove a bad feature with nx_delete_objects or
  nx_set_feature_suppression. Keep reference curves out of sketches used by features.

Shared NX:
- Save after each major feature and before every check or measurement.
- Heavy calls (<list for this product, e.g. nx_check_model with clash: true on bodies
  over 2,000 faces, unites of 50+ bodies>) only after "<how to get a turn>".
  Never run two reads at once on one large body. One body per blade row.
- If a call times out or NX stops answering: do not resend it; poll nx_get_status at most
  once a minute; query nx_get_operation before retrying a mutation; tell the orchestrator.
  Never restart or kill NX.

Live view: <one owner, e.g. "the orchestrator moves the view; never call nx_activate_part
display or nx_set_camera yourself">. Captures to files under projects/<project>/captures/;
inline images only when you must inspect something.
At most 3 NX calls in flight at once; send the next one when one returns.

Verify (after saving): bounds and key radii, nx_measure_mass with the material assigned,
clearances to neighbours with nx_measure_distance, nx_check_model where the part is light.
After a combine with several targets, measure each target; after a pattern, one copy.
Re-measure (mass, clearances, interference) after any rework. Say "sampled" for sampled
checks and "partial" for a check_model with complete: false.

Finish: save, capture <part>_done.png, close your parts. Report (<= 300 words): parts and
paths, bodies, counts used, material, measured mass and bounds, deviations from the spec,
what you did NOT check and why, tool problems. Every number from a tool result.
```
