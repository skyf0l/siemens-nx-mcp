# NX MCP Server

Let an AI assistant (Claude Code, Codex or any MCP client) model, inspect and draw in
Siemens NX on Windows: parts, sketches, curves, features, synchronous edits and body
repair, freeform surfaces, sheet metal, assemblies (arrangements, clearance sets,
weight and centre of gravity), motion, topology optimization and strength checks,
measurements and surface analysis, drawings, part families, Knowledge Fusion rules,
render materials and decals, and exports (STEP, PDF, DXF/DWG and more). You keep
working in NX while the agent works next to you.

Tested on NX 2406. NX 2606 can host it, but until it is tested there it offers only
the tools that read or change the view. The full list of tools (269) is in
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

Start NX, press **Alt+F8** (Play Journal) and run `nx_app\journals\start_2406.py`
from this folder (`start_2606.py` for NX 2606). A small **NX MCP** panel opens: the
add-on runs until you close NX. Do this each time you start NX.

Optional, to start it automatically with NX 2406: run this once, then restart NX.

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

## Troubleshooting

| Problem | Fix |
| --- | --- |
| The agent says NX is not running | Start NX (step 2) and check that the NX MCP panel is open. NX takes a while to start; the panel appears once NX's main window is up |
| The agent says NX is blocked by a dialog | Answer the dialog in NX; the agent never clicks NX dialogs. A dialog an agent step opened that stays unanswered for 20 s is flagged on the panel, and NX's taskbar button flashes |
| The agent says NX is busy (`NX_BUSY`) | Another step still runs in NX and nothing new was started; the agent waits and retries. A long step (a large export, a check on a very large body) can hold NX for minutes |
| The agent says you are busy in NX (`NX_UI_BUSY`) | Finish or cancel your NX command, then let the agent retry |
| The agent says a part is yours (`NX_PART_NOT_GRANTED`) | Tick **Allow agent to change <part>** on the panel if you want it to |
| The agent says a licence is missing (`NX_LICENSE_UNAVAILABLE`) | Some tools need extra NX licences. NX refuses them with this error, nothing is changed, and the agent stops offering that tool for 15 minutes, then asks NX again |
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
