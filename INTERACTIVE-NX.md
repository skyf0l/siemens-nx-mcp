# Interactive NX MCP, NX 2406 and 2606

The NX MCP add-on runs inside graphical NX. A Win32 timer on NX's UI thread runs one
authenticated bridge request at a time; the MCP process your AI client starts stays
separate and never calls NXOpen.

The **NX MCP control** panel has, from top to bottom: the mode buttons
(**Mode: (•) Auto ( ) Ask me first**, see [Panel modes](#panel-modes)); the agent's
activity, newest first; **Parts in use**, the agent's parts; the **Allow agent to
change <part>** checkbox when the work part is yours; and the buttons **Open log**,
**Show part** and, at the right, **Stop bridge**. While a step waits for your answer
in Ask me first, **Run once**, **Run & switch to Auto** and **Not now** replace **Open
log** and **Show part**. Long native operations can briefly pause NX; a mode change
and stopping take effect between steps. The panel window can be resized.

## Starting the add-on

### NX 2406: play `start_2406.py` (default)

Start NX, press **Alt+F8** (Play Journal) and run `nx_app/journals/start_2406.py`. The
host and panel start at once, the journal returns, and the bridge keeps running until
NX exits or you press **Stop bridge**. It runs the startup application's own code
(`play()` in `nx_app/startup/nx_mcp_startup.py`).

Why this journal does not die or freeze: its first line is `# nx: threaded`. NX 2406
runs such a journal in its main Python interpreter, which stays loaded for the whole
session, so the timer it arms keeps ticking after it returns (a journal without that
line runs in a sub-interpreter that NX unloads on return, and its timer never fires).
Nothing keeps running inside the journal command, so NX work that needs NX's own main
loop (the first Modeling start, the first PDF export of a session) works. Never keep
a journal running to host the bridge: NX then freezes on exactly that work.

If the bridge already runs in this NX (from the startup application or an earlier
play), the journal only shows the panel with `NX MCP bridge is already running in
this NX (started by the ...)`; it never starts a second bridge. After **Stop bridge**,
playing the journal starts the host again. If the host cannot start, the journal
fails with the error (also in `<workspace>/.nx-mcp/resident-error.txt`).

### NX 2406: start with NX automatically (optional)

The startup application `nx_app/startup/nx_mcp_startup.py` starts the same host each
time NX starts. Its first line, `# nx: threaded`, keeps it loaded for the session;
NX calls its `startup()` at launch and a timer on NX's UI thread then ticks the host
between NX commands. Setup, once per machine:

1. Point NX at the `nx_app` folder: set `UGII_USER_DIR=<checkout>\nx_app` for NX, or,
   if `UGII_USER_DIR` is already used, add the folder as a line to your
   `custom_dirs.dat` (`UGII_CUSTOM_DIRECTORY_FILE`). NX loads
   `<folder>\startup\*.py` at launch.
2. NX and the MCP process must use the same workspace. Both default to
   `<home>\NX_MCP_WORKSPACE`; set `NX_MCP_WORKSPACE` for both to use another one.
3. Start NX normally. The panel appears once NX's main window is up. Startup failures
   go to `<workspace>/.nx-mcp/resident-error.txt`.

### NX 2606

Play `nx_app/journals/start_2606.py` once in graphical NX; the journal returns and NX
keeps the timer host. `start_2406.py` should also host NX 2606, but that is not
verified. Until NX 2606 has acceptance evidence, only the tools that read or change
the view are offered there.

Closing the panel stops the bridge and gives NX back, leaving NX and its parts open.

## Panel modes

Pick the mode with the two buttons at the top of the panel. Only you change the
mode; the agent cannot. The panel starts in Auto, also in the development loop. The window
title always says the mode and what the agent is doing.

| Mode | Window title | What the agent's steps do |
|---|---|---|
| **Auto** (default) | `NX MCP — Auto`; `NX MCP — Auto · agent step running` while a step runs; `NX MCP — Auto · agent waiting for you` while one waits for your command to end | You keep working in NX. Each step takes NX only while it runs (NX's main window is briefly disabled, nothing stays locked) and gives it straight back. No request card. |
| **Ask me first** | `NX MCP — Ask me first`; `NX MCP — Agent is waiting` when a step waits for your answer | Every step waits for your answer on the panel. |

**Auto.** The idle line reads `·  Auto — you work in NX, agent steps run
between your commands`. Read-only steps (listing, measuring) run at once, even
in the middle of your command. Any other step waits silently while you are busy in
NX: an NX command or dialog is open, a menu is open, you are dragging, or a mouse
button is held. The panel then shows

```
⏸  Waiting until you finish your command (12 s): Blend 2 edges R3 mm
    “Round the outer edges”
```

and the step runs as soon as NX is free. After 2 minutes it is not run; the
agent gets `NX_UI_BUSY` (nothing changed, retry later). Steps that can take long
(exports, imports, assembly-wide updates) simply run; their running line says
`— running, NX may pause`.

**Modal dialogs.** A modal NX dialog never has an agent step run under it. If one is
open when a step that is not a pure read is about to start (for example it opened
just after the busy check), the step is refused with `NX_MODAL_DIALOG_OPEN` and
nothing ran; reads still answer. If a step itself opens a dialog that waits for an
answer, the dialog is named in the activity list (a ⚠ line), in the log and in the
step's result, and until someone answers it every other agent call is refused at once
with `NX_MODAL_DIALOG_OPEN`; the status read still answers and names the dialog. The
agent never clicks a dialog: it asks you to answer it.

**Your changes.** When you change the model between two agent steps (a feature, an
expression, Ctrl+Z, hide/show, opening or closing a part), the next agent step
notices it and the panel shows `✎  14:32:40  You changed the model — agent will
re-read its references`; the log line adds what changed (NX's undo marks or the
loaded parts) and never names a part. Clicking another tab or **Show part** alone is
not a change. Only then does the agent re-check its references (those of objects you
deleted stop working; the others keep working) and drop its rollback checkpoints:
your changes have no operation receipt and must never be undone by a later agent
rollback. If you changed nothing, the agent continues where it was.

**Sketches.** A sketch left active on a drawing sheet (NX re-activates a sheet's last
sheet sketch when the sheet opens) does not block the agent's model steps: it is
closed quietly first. An active model sketch still does (`NX_SKETCH_ACTIVE`).

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

- **Run once**: the step shown runs (a change waits until your NX command ends); the panel
  stays in Ask me first, so the next step shows its own card.
- **Run & switch to Auto**: the step shown runs and the panel switches to Auto; the
  steps still waiting then run as in Auto.
- **Not now**: the step shown fails with `NX_CONTROL_DECLINED`; the agent is told to
  ask in chat. The next waiting step gets its own card.
- No answer within 60 s of a step's card: that step fails with `NX_CONTROL_PAUSED`
  and did not run. Each step has its own 60 s.

Picking Ask me first while the agent works pauses after the current step.

**What the agent sees.** Only the status read (`nx_get_status`) never waits. Its
`control` section reports `mode` (`auto` or `ask`), `mode_label` (`Auto` or
`Ask me first`), `engineer_busy` (you are in an NX command or dialog),
`waiting_for_answer` (steps waiting on the card), `activity`, the step `running`
now, `queue` (the steps waiting for NX at the moment of the read, also while a step
runs, and how long the oldest has waited), `waiting` (the larger of the two counts),
`busy` (any of these) and `blocked_by_dialog` while a dialog holds NX. No tool
changes the mode: the agent asks you in chat when it needs another one.

The mode you pick is kept when the bridge restarts in the same NX session
(development reloads, `.nx-mcp/ui-mode.json`). A new NX session starts in Auto.

## How fast a step starts

A request starts as soon as it arrives: the bridge's listening socket posts a
window message to the panel (`WSAAsyncSelect`, FD_ACCEPT), which runs the host at
once on NX's UI thread instead of at the next timer tick. NX holds Python's
interpreter lock while it is idle, so the host also lets the bridge's worker threads
run until the request is queued and its answer written. Measured on NX 2406, client
side, p50 / p95 (previous transport in brackets): a status read 14 / 22 ms
(124 / 188), `nx_list_parts` 16 / 23 ms (156 / 186), a datum plane 40 / 50 ms
(181 / 227); through a fresh MCP sidecar `nx_invoke` of the read 26 / 45 ms
(145 / 180) and of the datum plane 58 / 88 ms (191 / 234). Auto costs the same.
`NX_MCP_FAST_WAKE=0` (set for NX) falls back to the timer.

A request may wait in NX's queue up to 135 s (busy engineer, unanswered card, another
step running) and then has up to 120 s to run (a heavy step 330 s, a long one such as
an import or export 1830 s); the MCP process waits for that plus 15 s. A step that
could not start within 135 s of being sent, or whose agent gave up (cancelled the
call), never runs later: it fails with `NX_CALL_EXPIRED` and the log says `Skipped
...: the agent stopped waiting`. If a step started but its answer was lost, its
outcome is unknown: the agent checks it with `nx_get_operation` before retrying.

While two or more parts are loaded, every line of the panel history and of
`.nx-mcp/activity.log` names the part file the step worked in first, e.g.
`✓  14:32:40  [cover.prt] Extrude "profile" 10 mm → EXTRUDE(3)  0.4 s`.

## Your parts and the agent's parts

The agent may change or save only the parts it created or opened itself in this NX
session, and parts you allow; it never closes or brings up a part you loaded. When
the work part is one of yours, the panel shows a checkbox above the buttons:

```
☐ Allow agent to change bracket
```

Tick it to let the agent change and save that part; untick it to withdraw the
permission. Until you tick it, the agent's changes to it fail with
`NX_PART_NOT_GRANTED` before anything happens (a change that slips through is undone),
and the agent is told to ask you in chat. Reading, measuring and view changes
(camera, visibility, captures) are always allowed. The checkbox follows the work
part; the agent's own parts never show it. Permissions last until NX closes or the
bridge restarts. Saving the part under a new name (File > Save As) withdraws it. In
the development loop (`NX_MCP_DEV=1`) there is no checkbox and changes are not
restricted.

Several agents can share one NX: an agent does not close or bring up a part another
agent loaded (`NX_PART_BUSY`) unless it insists with `force`.

## NX tabs and "Parts in use"

Every part the agent shows opens in its **own NX window tab**; the agent never
replaces the part in your tab.

- While the active tab shows one of **your** parts, the agent never makes its own
  part active: a part it creates or opens stays loaded in the background, or gets a
  tab behind yours when the agent asks with `display: true`.
- Otherwise, while the agent has a single part, creating, opening or activating it
  shows its tab. Once two or more agent parts are loaded (several agents, or one
  agent with several parts), the active tab stays active: a new part is only
  loaded, and an opened or activated part gets its tab in the background (unless the
  agent asks with `display: true`).

Model steps work in their part without showing it (NX makes it the work part for the
step, then your work part comes back). Drawing, capture, camera and Sheet Metal
steps, and steps that edit an existing sketch, need their part on screen:
they bring the part's own tab to the front for the step and give your tab and work
part back when they end, so the agent's tab can show for a moment even while one of
your parts is on screen. They never leave an agent part in front of yours. A drawing sheet
stays open in its part's tab. Closing a part closes its tab; when it was the active
tab, the tab you had before comes back. NX's undo list survives all of it.

Closing an assembly unloads only the component parts that assembly brought in
(its open, or a component step that loaded the file). Parts created or opened by
name, by the agent or by you, stay loaded until someone closes them.

Below the activity list, **Parts in use** lists the agent's parts, one line each:

```
cover.prt  ·  in a tab  ·  modified  ·  14:32:05 Extrude "profile" 10 mm
bolt.prt  ·  background  ·  saved  ·  14:31:50 Create thread M6
```

(`shown`: the active tab; `in a tab`; `background`: loaded without a tab;
`modified` / `saved` / `new`: never saved.) Your own parts are never listed. Select
a line and click **Show part** (or double-click it) to bring that part's tab to the
front; while you are in an NX command the panel asks you to finish it first. The
list refreshes after every agent step and scrolls when long.

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
start time, not a counter. Quick steps say only `running`; heavier ones add `NX may
pause`. When the step ends it becomes a ✓/✗/⚠ line in place.

Failures always state what happened to the model; a long summary is shortened
before the model state is. Read-only and view-only steps are grouped into one
line. Your own changes between agent steps show as a ✎ line. While a step
waits for your answer, the request card replaces the list.

Every step, including each read-only one, is appended to
`<workspace>/.nx-mcp/activity.log` with its tool name and the agent's note.
**Open log** opens it in your default text editor. The log rotates to
`activity.log.1` above 5 MB.

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
8. With one of your parts on screen, let two agents work: your tab stays active,
   each agent part appears in **Parts in use**, and **Show part** brings its tab to
   the front.

## Development loop (never for production work)

See "Live NX development loop" in [CLAUDE.md](CLAUDE.md#live-nx-development-loop).
