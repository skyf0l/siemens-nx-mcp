# Interactive NX MCP, NX 2406 and 2606

On NX 2406 run the bridge as an NX startup application, or play
`nx_app/journals/start_2406.py` in an NX started without it (next section). On NX 2606,
play `nx_app/journals/start_2606.py` once in graphical NX. The journal returns. A pinned Win32 timer callback drains one authenticated bridge request at a time on the registering NX UI thread. The HTTP/MCP process remains separate; it never calls NXOpen. The NX MCP control window has two mode buttons at the top (**Mode: (•) Auto ( ) Ask me first**, see [Panel modes](#panel-modes)), the agent's activity, **Open log** and **Stop bridge**, and the **Allow agent to change <part>** checkbox when the work part is yours. Long native operations can temporarily block NX; a mode change and stopping take effect between calls.

## NX 2406: run the bridge as an NX startup application (recommended)

NX 2406 unloads a played Python journal's interpreter as soon as it returns
(`GetUnloadOption` is not read for Python), so the 2606 timer journal stops at
once. Keeping a journal running instead holds
NX's main loop inside a journal command: NX work that needs that loop - the
first part of a fresh session (starting Modeling), the first PDF export - then
waits forever and **NX freezes**. Never keep a journal running to host the bridge.

Use the startup application instead: `nx_app/startup/nx_mcp_startup.py`. Its
first line, `# nx: threaded`, makes NX keep the module loaded for the whole
session (the same flag Siemens' Check-Mate Python checkers use). NX calls its
`startup()` at launch; a timer on NX's UI thread then ticks the host between NX
commands, exactly like the 2606 timer host. Nothing is played, there is no
"Work in Progress" window, and the first part / first PDF of a session work
(verified on NX 2406).

Setup, once per machine:
1. Point NX at the `nx_app` folder: set `UGII_USER_DIR=<checkout>\nx_app` for NX,
   or, if `UGII_USER_DIR` is already used, add the folder as a line to your
   `custom_dirs.dat` (`UGII_CUSTOM_DIRECTORY_FILE`). NX loads
   `<folder>\startup\*.py` at launch.
2. NX and the sidecar must use the same workspace. Both default to
   `<home>\NX_MCP_WORKSPACE`; set `NX_MCP_WORKSPACE` for both to use another one.
3. Start NX normally. The **NX MCP control** panel appears once NX's main window
   is up; the host starts in Auto. Startup failures go to
   `<workspace>/.nx-mcp/resident-error.txt`.
The bootstrap stays loaded until NX exits; **Stop bridge** stops the host.

### NX 2406 without the startup application: play `start_2406.py`

For an NX session started without `nx_app` (another user directory, a colleague's
machine, one-off use), play `nx_app/journals/start_2406.py` once in graphical NX
(**Alt+F8**, enter the file, **Run**). It runs the startup application's own code
(`play()` in `nx_mcp_startup.py`): the host and panel start at once, the journal
returns, and the bridge keeps running until NX exits or you press **Stop bridge**.

Why this journal does not die or freeze: its first line is also `# nx: threaded`.
NX 2406 runs such a journal in its main Python interpreter, which stays loaded
for the whole session, so the timer it arms keeps ticking after it returns (a
journal without that line runs in a sub-interpreter that NX unloads on return,
and its timer never fires). Nothing keeps running inside the journal command, so
the first Modeling start and the first PDF export work as with the startup app.
Verified on NX 2406.9160: timer ticking 50 s after the journal returned, then a
first Modeling session and a first PDF export in the same fresh session, no
dialogs.

Use the startup application when NX should always come up with the bridge; play
the journal when NX was started without it. Both coexist: the journal finds the
bootstrap the startup app (or an earlier play) loaded, and if its host is running
it only shows the panel with `NX MCP bridge is already running in this NX
(started by the startup app)`; it never starts a second bridge. After **Stop
bridge**, playing the journal starts the host again. If the host cannot start,
the journal fails with the error (also in `resident-error.txt`). The journal
should also host NX 2606, which keeps journals loaded anyway, but that is not
verified; on 2606 keep using `start_2606.py`.

Closing the control panel stops the bridge and unlocks NX, leaving the application and parts open.

## Panel modes

Pick the mode with the two buttons at the top of the panel. Only you change the
mode; the agent cannot. The window title always says the mode and what the agent
is doing.

| Mode | Window title | What the agent's steps do |
|---|---|---|
| **Auto** (default, also in the development loop) | `NX MCP — Auto`; `NX MCP — Auto · agent step running` while a step runs; `NX MCP — Auto · agent waiting for you` while one waits for your command to end | You keep working in NX. Each step takes NX only while it runs (NX's main window is briefly disabled, nothing stays locked) and gives it straight back. No request card. |
| **Ask me first** | `NX MCP — Ask me first`; `NX MCP — Agent is waiting` when a step waits for your answer | Every step waits for your answer on the panel. |

**Auto.** The idle line reads `·  Auto — you work in NX, agent steps run
between your commands`. Read-only steps (listing, measuring) run at once, even
in the middle of your command. A step that changes something waits silently
while you are busy in NX: an NX command or dialog is open, a menu is open, you
are dragging, or a mouse button is held. The panel then shows

```
⏸  Waiting until you finish your command (12 s): Blend 2 edges R3 mm
    “Round the outer edges”
```

and the step runs as soon as NX is free. After 2 minutes it is not run; the
agent gets `NX_UI_BUSY` (nothing changed, retry later). Steps that can take long
(exports, imports, assembly-wide updates) simply run; their running line says
`— running, NX may pause`.

A modal NX dialog never has an agent step run under it. If one is open when a
step that changes the model, the view or a file is about to start (for example
it opened just after the busy check), the step is refused with
`NX_MODAL_DIALOG_OPEN` and nothing ran; reads still answer. A dialog that opens
during a step is named in the step's result and in the activity log. The agent
never clicks a dialog: it asks you to answer it.

When you change the model between two agent steps (a feature, an expression,
Ctrl+Z, hide/show, another display part), the next agent step notices it and
the panel shows `✎  14:32:40  You changed the model — agent will re-read its
references`. Only then are the agent's references and checkpoints dropped: your
changes have no operation receipt and must never be undone by a later agent
rollback. If you changed nothing, the agent continues where it was.

**Ask me first.** Agent requests are held, not refused. The title becomes
**NX MCP — Agent is waiting**, the NX taskbar button flashes (focus is never
taken), and the panel shows what the agent wants in plain words, built from the
request's real arguments and the objects' NX names, for example
`▶ Agent wants to: Extrude "profile" 10 mm`. The card shows one step at a time,
the oldest waiting one; when more wait it says so (`Answer within 42 s for this
step (2 more waiting)`), and your answer applies to the step shown only. While
two or more parts are loaded, the step names the part file it works in first:
`▶ Agent wants to: [cover.prt] Extrude "profile" 10 mm`. If the agent added a note
(`nx_invoke(..., intent="Add the 10 mm mounting boss")`), it is shown below,
quoted and labelled as the agent's note. You then choose:

- **Run once**: the step shown runs; the panel stays in Ask me first, so the next
  step shows its own card.
- **Run & switch to Auto**: the step shown runs and the panel switches to Auto; the
  steps still waiting then run as in Auto.
- **Not now**: the step shown fails with `NX_CONTROL_DECLINED`; the agent is told to
  ask in chat. The next waiting step gets its own card.
- No answer within 60 s of a step's card: that step fails with `NX_CONTROL_PAUSED`
  and did not run. Each step has its own 60 s.

Picking Ask me first while the agent works pauses after the current step.

**What the agent sees.** Only the status read (`nx_get_status`) never waits. Its
`control` section reports `mode` (`auto` or `ask`), `mode_label` (`Auto` or
`Ask me first`), `busy` (you are in an NX command or dialog), `waiting` (steps
waiting for your answer), `activity`, the step `running` now and `queue` (the steps
waiting for NX, and how long the oldest has waited). No tool changes the
mode: the agent asks you in chat when it needs another one.

The mode you pick is kept when the bridge restarts in the same NX session
(development reloads, `.nx-mcp/ui-mode.json`). A new NX session starts in Auto.

## How fast a step starts

A request starts as soon as it arrives: the bridge's listening socket posts a
window message to the panel (`WSAAsyncSelect`, FD_ACCEPT), which runs the host at
once on NX's UI thread instead of at the next 62.5 ms timer tick. NX holds
Python's interpreter lock while it is idle, so the host also lets the bridge's
worker threads run until the request is queued and its answer written. Measured
on NX 2406, client side, p50 / p95 (previous transport in brackets): a status
read 14 / 22 ms (124 / 188), `nx_list_parts` 16 / 23 ms (156 / 186), a datum
plane 40 / 50 ms (181 / 227); through a fresh MCP sidecar `nx_invoke` of the read
26 / 45 ms (145 / 180) and of the datum plane 58 / 88 ms (191 / 234). Auto costs
the same. `NX_MCP_FAST_WAKE=0` (set for NX) falls back to the timer. A request
may wait in NX's queue up to 135 s (busy engineer, unanswered card, another
step running) and then has up to 120 s to run (a heavy step 330 s, a long one
such as an import or export 1830 s); the sidecar waits for that plus 15 s. A
step that could not start within 135 s of being sent, or whose agent gave up
(cancelled the call), never runs later: it fails with `NX_CALL_EXPIRED`.

While two or more parts are loaded, every line of the panel history and of
`.nx-mcp/activity.log` names the part file the step worked in first, e.g.
`✓  14:32:40  [cover.prt] Extrude "profile" 10 mm → EXTRUDE(3)  0.4 s`.

## Your parts and the agent's parts

The agent may change, save or close only the parts it created or opened itself
in this NX session, and parts you allow. When the work part is one of yours, the
panel shows a checkbox above the buttons:

```
☐ Allow agent to change bracket
```

Tick it to let the agent change, save or close that part; untick it to withdraw
the permission. Until you tick it, the agent's changes to it fail with
`NX_PART_NOT_GRANTED` before anything happens, and the agent is told to ask you
in chat. Reading, measuring and view changes (camera, visibility, captures) are
always allowed. The checkbox follows the work part; the agent's own parts never
show it. Permissions last until NX closes or the bridge restarts. Saving the
part under a new name (File > Save As) withdraws it. In the development loop
(`NX_MCP_DEV=1`) there is no checkbox and no restriction.

## Checking the panel by hand

The automated tests cannot open a real NX command dialog. After an update,
check once in graphical NX:

1. Pick **Auto**. Ask the agent for a small change (a datum plane). It runs at
   once; NX stays usable; the title returns to `NX MCP — Auto`.
2. Open an NX command dialog (Insert > Datum/Point > Datum Plane) and leave it
   open. Ask the agent for another change: the panel shows `⏸  Waiting until you
   finish your command (… s)`. Ask it to list the parts: that answers at once.
   Cancel the dialog: the waiting step runs within a moment.
3. Leave a dialog open for over 2 minutes while the agent asks for a change:
   the agent gets `NX_UI_BUSY` and nothing changed.
4. Between two agent steps, add a feature yourself (or press Ctrl+Z). The next
   agent step adds the ✎ line. Without your edit no ✎ line appears.
5. Ask for a STEP export in Auto: no card appears; the running line says
   `— running, NX may pause` and NX is usable again when it ends.
6. Pick **Ask me first**: every step shows the card with **Run once**, **Run &
   switch to Auto** and **Not now**; try each, and 60 s without an answer.
7. Pick **Auto** again: NX is usable at once and the next step runs without a
   card.

## Development loop (never for production work)

See "Live NX development loop" in [CLAUDE.md](CLAUDE.md#live-nx-development-loop).

## What the agent is doing

The window title says the mode and what the agent does; the panel body is one
list of the agent's activity, newest first. The step running now is the top
line, in plain words with its start time and the agent's note below it:

```
▶  14:32:31  Blend 2 edges R3 mm — running, NX may pause
               “Round the outer edges”
✗  14:32:20  Drill hole Ø6.6 mm × 12 mm deep — rolled back, model unchanged (…)
·  14:32:09  3 read-only or view steps (model unchanged)
✓  14:32:05  Extrude "profile" 10 mm → EXTRUDE(2)  7.0 s
```

The panel cannot repaint during a native call, so the running line shows a
start time, not a counter. Quick steps (cost `light`) say only `running`;
heavier ones (`heavy`, `long`) add `NX may pause`. When the step ends it becomes
a ✓/✗/⚠ line in place.

Failures always state what happened to the model; a long summary is shortened
before the model state is. Read-only and view-only steps are grouped into one
line. Your own changes between agent steps show as a ✎ line. While a step
waits for your answer, the request card replaces the list.

Every step, including each read-only one, is appended to
`<workspace>/.nx-mcp/activity.log` with its tool name and the agent's note.
**Open log** opens it in your default text editor. The log rotates to
`activity.log.1` above 5 MB.

`nx_capture_view` uses `Part.Views.CreateImageExportBuilder` and returns a PNG artifact plus MCP image content. It captures the displayed CAD viewport, never the desktop. White/transparent/original backgrounds and shaded/shaded-with-edges/static-wireframe styles are exposed. Width and height are advisory: the installed graphics driver can use the actual device size, so both requested and actual resolution are returned. These are viewport renderings, not photorealistic ray tracing. Images above 8 MiB use chunked artifact download. `nx_get_camera` reports the display-part view axes, NX view origin, absolute origin, scale, and style.

`nx_check_interference` examines the cross-product of two selected bodies/components, including nested body occurrences. A clearance check examines every distinct body pair in selected groups or the full assembly. Conservative boxes only prune provably separated pairs; reported distances use native geometry. Native solid intersection distinguishes penetration, contact and separation and measures pairwise overlap volumes in mm³. Temporary interference solids are rolled back explicitly: NX builder Reset alone does not remove them. Body/feature counts are checked afterward. Pairwise overlap volumes are not geometric union volume. Custom reference-set edge cases and general solid-validity certification remain outside the validated scope.

Use a stable operation ID for mutation retry. A queued request that expires before execution is discarded. If execution already began, a transport timeout reports an unknown outcome; query the durable receipt. Do not send simultaneous NXOpen mutations. Save, engineer edits between agent steps, and part lifecycle changes can invalidate native recovery marks; query checkpoint state.

Validation includes native GUI thread identity, visible sketch/extrusion, pause/resume, native PNGs, cubes with 500 mm³ overlap, contact and 2 mm separation, a 3 mm clearance requirement, and a rotated nested assembly. Saved flags and checkpoints survive collision inspection. The prior eleven batch regression groups remain separate evidence; tool status does not imply every NX feature has been tested.
