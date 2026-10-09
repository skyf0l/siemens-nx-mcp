# Interactive NX MCP, NX 2406 and 2606

On NX 2406 run the bridge as an NX startup application (next section). On NX 2606,
play `examples/start_nx_interactive_2606.py` once in graphical NX. The journal returns. A pinned Win32 timer callback drains one authenticated bridge request at a time on the registering NX UI thread. The HTTP/MCP process remains separate; it never calls NXOpen. The NX MCP control window has three mode buttons at the top (**Mode: (•) Auto ( ) Agent drives ( ) Ask me first**, see [Panel modes](#panel-modes)), the agent's activity, **Open log** and **Stop bridge**, and the **Allow agent to change <part>** checkbox when the work part is yours. Long native operations can temporarily block NX; a mode change and stopping take effect between calls.

## NX 2406: run the bridge as an NX startup application (recommended)

NX 2406 unloads a played Python journal's interpreter as soon as it returns
(`GetUnloadOption` is not read for Python), so the 2606 timer journal stops at
once. Keeping a journal running instead (`start_nx_interactive_2406.py`) holds
NX's main loop inside a journal command: NX work that needs that loop - the
first part of a fresh session (starting Modeling), the first PDF export - then
waits forever and **NX freezes**. Do not use the resident journal for real work.

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
2. Set `NX_MCP_WORKSPACE` for NX (and the sidecar).
3. Start NX normally. The **NX MCP control** panel appears once NX's main window
   is up; the host starts in Auto. Startup failures go to
   `<workspace>/.nx-mcp/resident-error.txt`.
The bootstrap stays loaded until NX exits; **Stop bridge** stops the host.

Closing the control panel stops the bridge and unlocks NX, leaving the application and parts open.

## Panel modes

Pick the mode with the three buttons at the top of the panel. The window title
always says the mode and what the agent is doing.

| Mode | Window title | What the agent's steps do |
|---|---|---|
| **Auto** (default) | `NX MCP — Auto`; `NX MCP — Auto · agent step running` while a step runs; `NX MCP — Auto · agent waiting for you` while one waits | You keep working in NX. Each step takes NX only while it runs (NX's main window is briefly disabled, nothing stays locked) and gives it straight back. |
| **Agent drives** (default in the development loop) | `NX MCP — Agent drives` | NX input stays locked for the agent, also between steps, until you pick another mode. |
| **Ask me first** | `NX MCP — Ask me first`; `NX MCP — Agent is waiting` when a step waits for your answer | Every step waits for your answer on the panel. |

**Auto.** The idle line reads `·  Auto — you work in NX, agent steps run
between your commands`. Read-only steps (listing, measuring) run at once, even
in the middle of your command. A step that changes something waits while you
are busy in NX: an NX command or dialog is open, a menu is open, you are
dragging, or a mouse button is held. The panel then shows

```
⏸  Waiting until you finish your command (12 s): Blend 2 edges R3 mm
    “Round the outer edges”
```

and the step runs as soon as NX is free. After 2 minutes it is not run; the
agent gets `NX_UI_BUSY` (nothing changed, retry later). Steps that can take long
(exports, imports, assembly-wide updates) ask first with the request card
(**Run once** / **Not now**); each long step asks once.

When you change the model between two agent steps (a feature, an expression,
Ctrl+Z, hide/show, another display part), the next agent step notices it and
the panel shows `✎  14:32:40  You changed the model — agent will re-read its
references`. Only then are the agent's references and checkpoints dropped: your
changes have no operation receipt and must never be undone by a later agent
rollback. If you changed nothing, the agent continues where it was.

**Agent drives** locks NX with `UI.LockAccess` and keeps it locked between steps
(no unlock and relock per step; only steps that create, open or export files run
unlocked, as FileNew inside the lock can strand it). If you are busy in NX when
you pick it, NX is locked as soon as you finish (`⏸  Waiting until you finish
your command, then NX is locked for the agent`). NX is never handed back
automatically: pick Auto or Ask me first when you want it back. The top lines
then recap the agent's session (`Agent's last session: 6 changes, 1 failed
(14:31–14:33)`).

**Ask me first.** Agent requests are held, not refused. The title becomes
**NX MCP — Agent is waiting**, the NX taskbar button flashes (focus is never
taken), and the panel shows what the agent wants in plain words, built from the
request's real arguments and the objects' NX names, for example
`▶ Agent wants to: Extrude "profile" 10 mm (+2 more)`. If the agent added a note
(`nx_invoke(..., intent="Add the 10 mm mounting boss")`), it is shown below,
quoted and labelled as the agent's note. You then choose:

- **Run once**: the waiting steps run in order; the panel stays in Ask me first,
  so the next step asks again.
- **Run & switch to Auto**: the waiting steps run and the panel switches to Auto.
- **Not now**: they fail with `NX_UI_DECLINED`; the agent is told to ask in chat.
- No answer within 60 s: they fail with `NX_UI_PAUSED` and nothing ran.

Picking Ask me first while the agent works pauses after the current step.

**What the agent may change itself.** Status reads never wait. The agent may
always give you more control (`nx_set_control(mode="auto")` from Agent drives,
`mode="ask"` from anywhere; `mode="manual"` is the old name of ask). A request
for more control (`mode="auto"` from Ask me first, `mode="agent"` from Auto or
Ask me first) shows the request card like any other step, so the agent can never
leave Ask me first, or lock NX, without your click. `nx_set_control(mode="status")`
reports `mode` (`auto`, `agent` or `ask`), `owner` (agent or engineer), `busy`
(you are in an NX command), `waiting` (steps on the request card) and `activity`.

The mode you pick is kept when the bridge restarts in the same NX session
(development reloads, `.nx-mcp/ui-mode.json`); a mode the agent picked is not. A
new NX session starts in Auto (Agent drives in the development loop).

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
may wait in NX's queue up to 135 s (busy engineer, unanswered card) and then has
up to 120 s to run; the sidecar waits 270 s in all.

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
5. Ask for a STEP export in Auto: the card appears with **Run once** and
   **Not now**.
6. Pick **Ask me first**: every step shows the card with **Run once**, **Run &
   switch to Auto** and **Not now**; try each, and 60 s without an answer.
7. Pick **Agent drives**: NX is locked between steps. Pick **Auto**: NX is
   usable at once.

## Development loop (never for production work)

Start NX with the startup application in development mode:
`python scripts/dev_start_nx.py --user-dir nx_app --env NX_MCP_DEV=1`. The
agent gets control without a click, `scripts/dev_python.py` works, and
`python scripts/dev_reload.py --wait-agent` reloads all of `nx_mcp` from disk
(about two seconds; open parts stay open, object IDs reset): the bootstrap stops
the host, drops every `nx_mcp` module and starts a fresh host. It exits 1 and
prints the traceback if the new code does not load; the next reload recovers.
The panel mode you picked is kept across reloads. Changing
`nx_app/startup/nx_mcp_startup.py` itself needs an NX restart. The panel title
reads **NX MCP DEV**. `examples/start_nx_interactive_dev_2406.py` is the older
resident-journal variant and can freeze NX as described above.

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
start time, not a counter. Quick steps (cost `instant` or `light`) say only
`running`; heavier ones add `NX may pause`. When the step ends it becomes a ✓/✗/⚠ line in place.
In Agent drives, when the agent is idle, the top line reads `·  Idle since
14:32 — waiting for the agent's next request`, so you know it is safe to switch
to Auto.

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
