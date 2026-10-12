# NX MCP Server

Let an AI assistant (Claude Code, Codex or any MCP client) model, inspect and draw in
Siemens NX on Windows: parts, sketches, features, assemblies, drawings, sheet metal,
measurements and exports. You keep working in NX while the agent works next to you.

Supported: NX 2406 and NX 2606. The full list of tools is in [docs/tools.md](docs/tools.md).

## 1. Install

You need Python 3.10 or newer. In a terminal, in this folder:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

This creates `.venv\Scripts\nx-mcp.exe`, the program your AI client starts.

## 2. Start NX with the MCP add-on

**NX 2406:** tell NX where the add-on is, once, then start NX as usual:

```powershell
setx UGII_USER_DIR "C:\path\to\NX_MCP\nx_app"
```

(If you already use `UGII_USER_DIR`, add the `nx_app` folder to your `custom_dirs.dat`
instead.) When NX starts, a small **NX MCP** panel opens: the add-on is running.

**NX 2606**, or an NX 2406 started without the step above: in NX, press **Alt+F8**
(Play Journal) and run `nx_app\journals\start_2606.py` (or `start_2406.py`). Do this
each time you start NX.

## 3. Connect your AI client

**Claude Code:**

```powershell
claude mcp add nx --scope user -- "C:\path\to\NX_MCP\.venv\Scripts\nx-mcp.exe"
```

**Codex:**

```powershell
codex mcp add nx -- "C:\path\to\NX_MCP\.venv\Scripts\nx-mcp.exe"
```

or add to `%USERPROFILE%\.codex\config.toml`:

```toml
[mcp_servers.nx]
command = 'C:\path\to\NX_MCP\.venv\Scripts\nx-mcp.exe'
tool_timeout_sec = 300
```

Then ask your assistant, for example: *"Create a 100 x 60 x 10 mm plate with four
6.6 mm holes 10 mm from the edges, and show me a picture."* In Claude Code, `/mcp`
shows whether **nx** is connected.

Some NX calls (exports, imports) take a minute or more. If your client gives up too
early, raise its tool timeout: `tool_timeout_sec` in Codex, the `MCP_TOOL_TIMEOUT`
environment variable (milliseconds, e.g. `300000`) for Claude Code.

## Working together in NX

- The agent works only in parts it created or opened. It never closes or saves your
  parts, and it does not take over the part you are looking at.
- The **NX MCP** panel shows what the agent is doing and lists its parts
  (**Show part** brings one up).
  - **Auto** (default): the agent's steps run while you work; a step waits while you
    are in an NX command or dialog.
  - **Ask me first**: every agent step waits for your OK.
- Several agents can work in the same NX at once, each in its own files.
- Files go in the workspace folder `C:\Users\<you>\NX_MCP_WORKSPACE`.

More about the panel: [INTERACTIVE-NX.md](INTERACTIVE-NX.md).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| The agent says NX is not running | Start NX (step 2) and check that the NX MCP panel is open |
| The agent says NX is blocked by a dialog | Answer the dialog in NX; the agent never clicks NX dialogs |
| No NX MCP panel in NX 2406 | Check `UGII_USER_DIR` (step 2), restart NX, or play `start_2406.py` |
| **nx** is not connected in the client | Check the path to `nx-mcp.exe` and that step 1 finished |

## Settings (optional)

Set these in the client's server entry (`claude mcp add ... -e NAME=value`, or `env` in
Codex), and for NX where noted.

| Variable | Use |
| --- | --- |
| `NX_MCP_WORKSPACE` | Another workspace folder. Set the same value for NX and the client |
| `NX_MCP_SURFACE` | `full` shows every NX tool to the client directly (more context, fewer steps); default `agent` |
| `NX_MCP_BRIDGE_DESCRIPTOR` | Only for a non-standard setup (e.g. batch NX): the bridge file NX writes |

## For developers

Architecture: [docs/design/architecture.md](docs/design/architecture.md); tool rules:
[docs/design/tool-surface.md](docs/design/tool-surface.md); how to add a tool and the
live NX test loop: [CLAUDE.md](CLAUDE.md); tested scope per NX build:
[docs/capability-matrix.md](docs/capability-matrix.md).

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
