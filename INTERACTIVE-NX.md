# Interactive NX MCP, v2606

Play `examples/start_nx_interactive_2606.py` once in graphical NX. The journal returns. A pinned Win32 timer callback drains one authenticated bridge request at a time on the registering NX UI thread. The HTTP/MCP process remains separate; it never calls NXOpen. The NX MCP control window says who has control (window title and first line) and has one toggle, **Give control to agent** / **Take control back**, plus **Stop bridge**. Long native operations can temporarily block NX; taking control back and stopping take effect between calls (the toggle then reads **Take control back after this step**).

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
   is up; the host starts with you in control. Startup failures go to
   `<workspace>/.nx-mcp/resident-error.txt`.
The bootstrap stays loaded until NX exits; **Stop bridge** stops the host.

Agent mode locks NX model editing with `UI.LockAccess`. **Take control back** releases it for manual work and invalidates agent references and checkpoints: manual changes have no operation receipt and must not be undone by a later agent rollback. Reacquire references after giving control to the agent again. If an NX dialog is open, the handover is refused and the panel says to close it first. Closing the control panel stops the bridge and unlocks NX, leaving the application and parts open.

## When the agent needs control

While you have control, agent requests are held, not refused. The panel title
becomes **NX MCP — Agent is waiting**, the NX taskbar button flashes (focus is
never taken), and the panel shows what the agent wants in plain words, built
from the request's real arguments and the objects' NX names, for example
`▶ Agent wants to: Extrude "profile" 10 mm (+2 more)`. If the agent added a note
(`nx_invoke(..., intent="Add the 10 mm mounting boss")`), it is shown below,
quoted and labelled as the agent's note. You then choose:

- **Give control to agent**: the waiting requests run in order.
- **Not now**: they fail with `NX_UI_DECLINED`; the agent is told to ask in chat.
- No answer within 60 s: they fail with `NX_UI_PAUSED` and nothing ran.

Status reads and handing control back never wait. The agent cannot take control
itself: `nx_ui_control(mode="agent")` is a request like any other. Giving control
still invalidates references taken before your manual work, so a waiting request
that points at geometry you have since edited fails safely as stale.

## Development loop (never for production work)

Start NX with the startup application in development mode:
`python scripts/dev_start_nx.py --user-dir nx_app --env NX_MCP_DEV=1`. The
agent gets control without a click, `nx_dev_python` is available, and
`python scripts/dev_reload.py --wait-agent` reloads all of `nx_mcp` from disk
(about two seconds; open parts stay open, object IDs reset): the bootstrap stops
the host, drops every `nx_mcp` module and starts a fresh host. It exits 1 and
prints the traceback if the new code does not load; the next reload recovers.
Taking control back is kept across reloads. Changing
`nx_app/startup/nx_mcp_startup.py` itself needs an NX restart. The panel title
reads **NX MCP DEV**. `examples/start_nx_interactive_dev_2406.py` is the older
resident-journal variant and can freeze NX as described above.

## What the agent is doing

While the agent works, the panel shows the step in plain words with the agent's
note and its start time (`Started 14:32:05 · NX may freeze until this step
finishes`). The panel cannot repaint during a native call, so it shows a start
time, not a running counter. When the agent is idle but holds control, it shows
`Idle since 14:32`, so you know it is safe to take control back.

Below that, **Recent agent activity** lists the last four entries:

```
✓  14:32:05  Extrude "profile" 10 mm → EXTRUDE(2)  7.0 s
·  14:32:09  3 read-only or view steps (model unchanged)
✗  14:32:20  Drill hole Ø6.6 mm × 12 mm deep — rolled back, model unchanged (…)
⚠  14:32:31  Blend 2 edges R3 mm — partly applied, check the model
```

Failures always state what happened to the model; a long summary is shortened
before the model state is. Read-only and view-only steps are grouped into one
line. When you take control back, the panel recaps the agent's session
(`Agent's last session: 6 changes, 1 failed (14:31–14:33)`).

Every step, including each read-only one, is appended to
`<workspace>/.nx-mcp/activity.log` with its tool name and the agent's note.
**Open log** opens it in your default text editor. The log rotates to
`activity.log.1` above 5 MB.

`nx_screenshot` uses `Part.Views.CreateImageExportBuilder` and returns a PNG artifact plus MCP image content. It captures the displayed CAD viewport, never the desktop. White/transparent/original backgrounds and shaded/shaded-with-edges/static-wireframe styles are exposed. Width and height are advisory: the installed graphics driver can use the actual device size, so both requested and actual resolution are returned. These are viewport renderings, not photorealistic ray tracing. Images above 8 MiB use chunked artifact download. `nx_view_info` reports the display-part view axes, NX view origin, absolute origin, scale, and style.

`nx_check_interference` examines the cross-product of two selected bodies/components, including nested body occurrences. `nx_check_clearance` examines every distinct body pair in selected groups or the full assembly. Conservative boxes only prune provably separated pairs; reported distances use native geometry. Native solid intersection distinguishes penetration, contact and separation and measures pairwise overlap volumes in mm³. Temporary interference solids are rolled back explicitly: NX builder Reset alone does not remove them. Body/feature counts are checked afterward. Pairwise overlap volumes are not geometric union volume. Custom reference-set edge cases and general solid-validity certification remain outside the validated scope.

Use a stable operation ID for mutation retry. A queued request that expires before execution is discarded. If execution already began, a transport timeout reports an unknown outcome; query the durable receipt. Do not send simultaneous NXOpen mutations. Save, manual handoff, and part lifecycle changes can invalidate native recovery marks; query checkpoint state.

Validation includes native GUI thread identity, visible sketch/extrusion, pause/resume, native PNGs, cubes with 500 mm³ overlap, contact and 2 mm separation, a 3 mm clearance requirement, and a rotated nested assembly. Saved flags and checkpoints survive collision inspection. The prior eleven batch regression groups remain separate evidence; tool status does not imply every NX feature has been tested.
