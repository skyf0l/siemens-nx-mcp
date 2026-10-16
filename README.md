# NX MCP Server

Let an AI assistant (Claude Code, Codex or any MCP client) model, inspect and draw in
Siemens NX on Windows: parts, sketches, curves, features, synchronous edits and body
repair, freeform surfaces, sheet metal, assemblies (arrangements, clearance sets,
weight and centre of gravity), motion, topology optimization and strength checks,
measurements and surface analysis, drawings, part families, Knowledge Fusion rules,
render materials and decals, and exports (STEP, PDF, DXF/DWG and more). You keep
working in NX while the agent works next to you.

Tested on NX 2406; NX 2412 to 2606 are expected to work (see
[Supported NX versions](#supported-nx-versions)). The full list of tools (269) is in
[docs/tools.md](docs/tools.md).

## 1. Install

You need Git and Python 3.10 or newer. In a terminal:

```powershell
git clone https://github.com/skyf0l/siemens-nx-mcp.git
cd siemens-nx-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

This creates `.venv\Scripts\nx-mcp.exe`, the program your AI client starts.

## 2. Start NX with the MCP add-on

Start NX, press **Alt+F8** (Play Journal) and run `nx_app\journals\start.py` from
this folder, whatever your NX release (`start_2406.py` and `start_2606.py` do the
same). It lists the NX build, its Python version and any NXOpen part it cannot load,
then a small **NX MCP** panel opens: the add-on runs until you close NX. Do this each
time you start NX. Playing it again never starts a second add-on in the same NX.

Optional, to start it automatically with NX: run this once, then restart NX.

```powershell
setx UGII_USER_DIR "C:\path\to\siemens-nx-mcp\nx_app"
```

If you already use `UGII_USER_DIR`, add the `nx_app` folder to `custom_dirs.dat` instead.

## 3. Connect your AI client

**Claude Code:**

```powershell
claude mcp add nx --scope user -- "C:\path\to\siemens-nx-mcp\.venv\Scripts\nx-mcp.exe"
```

**Codex:**

```powershell
codex mcp add nx -- "C:\path\to\siemens-nx-mcp\.venv\Scripts\nx-mcp.exe"
```

or add to `%USERPROFILE%\.codex\config.toml`:

```toml
[mcp_servers.nx]
command = 'C:\path\to\siemens-nx-mcp\.venv\Scripts\nx-mcp.exe'
tool_timeout_sec = 300
```

Then ask your assistant, for example: *"Create a 100 x 60 x 10 mm plate with four
6.6 mm holes 10 mm from the edges, and show me a picture."* In Claude Code, `/mcp`
shows whether **nx** is connected.

Some NX calls (exports, imports) take a minute or more. If your client gives up too
early, raise its tool timeout: `tool_timeout_sec` in Codex, the `MCP_TOOL_TIMEOUT`
environment variable (milliseconds, e.g. `300000`) for Claude Code.

## Working together in NX

- The agent works in parts it created or opened. It never closes your parts, and
  changes or saves one only after you tick **Allow agent to change <part>** on the
  panel.
- It does not take over the part you are looking at: while your part is on screen,
  its parts open in the background or in a tab behind yours.
- The **NX MCP** panel shows what the agent is doing and lists its parts
  (**Show part** brings one up).
  - **Auto** (default): the agent's steps run while you work. A step that changes
    something waits while you are in an NX command or dialog (at most 2 minutes);
    reads run at once.
  - **Ask me first**: every agent step waits for your OK.
- Several agents can work in the same NX at once, each in its own parts; one agent
  does not close or bring up another's parts.
- Files go in the workspace folder `C:\Users\<you>\NX_MCP_WORKSPACE`; the agent reads
  and writes files only there. Its `.nx-mcp` subfolder holds the activity log and NX
  MCP's own state.

More about the panel: [INTERACTIVE-NX.md](INTERACTIVE-NX.md).

## Supported NX versions

| NX release | Support |
| --- | --- |
| 2406 | **Tested**: the tools are checked on live NX before a release |
| 2412, 2506, 2512, 2606 | **Expected to work**: every NXOpen call NX MCP makes exists there, but it is not tested on them |
| 2306, 2312 | **Best effort**: some tools may fail (about 35 on 2306, 8 on 2312) |

NX's own Python must be 3.10 or newer: `start.py` refuses an older one and says so.
The MCP program needs Python 3.10 or newer too.

On a release it was not tested on, the agent is offered every tool tested on another
release, marked untested. A tool that fails there because NX lacks a call it needs
reports `NX_VERSION_UNSUPPORTED`: nothing is changed, and the tool is not offered again
until NX restarts. Set `NX_MCP_UNTESTED=read_only` for NX and the client to offer
only the tools that read or change the view on such a release.

`.venv\Scripts\python -m nx_mcp.report --capabilities` writes
`<workspace>\.nx-mcp\capabilities.html`, a page for you only: per domain, the tools
tested on your NX, offered untested, not supported there, and refused for a licence in
this NX session.

**Reporting a problem.** Run `.venv\Scripts\python -m nx_mcp.report` while NX runs: it
writes `<workspace>\nx-mcp-report-<build>-<date>.json` (NX, NX MCP, Python and Windows
versions, the tools that failed for a missing NXOpen call, the last verify's counts;
never licences, error messages, part names, paths or user names). Check it, then attach
it to an issue with the lines `start.py` printed, the tool name, the error code the
agent got, and `<workspace>\.nx-mcp\resident-error.txt` if the add-on did not start.
Leave out which NX licences or modules you have, and your parts, drawings and file
names; a small part made only to show the problem is welcome.

**Verifying your NX release** (to make it a tested one). With NX running and no part
loaded in it, from this folder:

```powershell
.venv\Scripts\python -m nx_mcp.acceptance verify --owner <your-name>
```

It replays every acceptance scenario on new parts under `<workspace>\smoke\` (or only
the domains you name, e.g. `verify modeling sketch`) and closes them; it refuses to
start while other parts are loaded, and counts a tool NX refuses for a licence as
skipped without recording it. Scenarios that play the engineer need NX started with
`NX_MCP_DEV=1`; without it they are skipped. At the end it prints the files under
`acceptance\evidence\<build>\` to send (in a pull request or attached to an issue) and a
summary. Those files hold only pass/fail, check counts, error codes and numbers.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| The agent says NX is not running | Start NX (step 2) and check that the NX MCP panel is open. NX takes a while to start; the panel appears once NX's main window is up |
| The agent says NX is blocked by a dialog | Answer the dialog in NX; the agent never clicks NX dialogs. A dialog an agent step opened that stays unanswered for 20 s is flagged on the panel, and NX's taskbar button flashes |
| The agent says NX is busy (`NX_BUSY`) | Another step still runs in NX and nothing new was started; the agent waits and retries. A long step (a large export, a check on a very large body) can hold NX for minutes |
| The agent says you are busy in NX (`NX_UI_BUSY`) | Finish or cancel your NX command, then let the agent retry |
| The agent says a part is yours (`NX_PART_NOT_GRANTED`) | Tick **Allow agent to change <part>** on the panel if you want it to |
| The agent says a licence is missing (`NX_LICENSE_UNAVAILABLE`) | Some tools need extra NX licences. NX MCP lets NX decide: when NX refuses, nothing is changed, the error names the licence feature the command needs (`details.license`) so you know which one to ask for, and the agent stops offering that tool for 15 minutes, then asks NX again. Only the few commands where NX would otherwise block on its "License Not Available" dialog (switching applications, Topology Optimization, variational sweep) are checked first |
| No NX MCP panel | Play the journal again (step 2); with the optional auto-start, check `UGII_USER_DIR` and restart NX |
| **nx** is not connected in the client | Check the path to `nx-mcp.exe` and that step 1 finished |

## Settings (optional)

Set these in the client's server entry (`claude mcp add ... -e NAME=value`, or `env` in
Codex), and for NX where noted.

| Variable | Use |
| --- | --- |
| `NX_MCP_WORKSPACE` | Another workspace folder. Set the same value for NX and the client |
| `NX_MCP_SURFACE` | `full` shows every NX tool to the client directly (more context, fewer steps); default `agent` |
| `NX_MCP_BRIDGE_DESCRIPTOR` | Only for a non-standard setup: the bridge file NX writes (for NX, `NX_MCP_UI_DESCRIPTOR` sets where it writes it) |

## For developers

Architecture: [docs/design/architecture.md](docs/design/architecture.md); tool rules:
[docs/design/tool-surface.md](docs/design/tool-surface.md); how to add a tool and the
live NX test loop: [CLAUDE.md](CLAUDE.md); tested scope per NX build:
[docs/capability-matrix.md](docs/capability-matrix.md). Several agents building one
product in one NX: [docs/guides/multi-agent-builds.md](docs/guides/multi-agent-builds.md).

```powershell
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pre_commit install --install-hooks
.venv\Scripts\python -m pre_commit run --all-files
.venv\Scripts\python -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
.venv\Scripts\python -m mypy src/nx_mcp
```

## License and credits

MIT. Based on [DreamEnding/NX_MCP](https://github.com/DreamEnding/NX_MCP) and its fork
[xuio/NX_MCP](https://github.com/xuio/NX_MCP); now an independent project, with
their commits kept in its history.
