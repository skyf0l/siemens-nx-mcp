# CLAUDE.md

## Project

NX MCP is a two-process Siemens NX automation server. The external FastMCP
sidecar owns stdio and validation; an NX-journal-owned authenticated loopback
bridge owns NXOpen and the current NX session.

## Commands

```powershell
python -m pip install -e ".[dev]"
python -m pre_commit install --install-hooks
python -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
python -m nx_mcp.real_smoke --workspace D:\NX_MCP_WORKSPACE --iterations 20
```

## Production path

- `server.py`: sidecar entry point; always exposes every tool and requires
  `NX_MCP_WORKSPACE`.
- `certified.py`: typed FastMCP tools and workspace validation.
- `bridge.py`: authenticated loopback protocol, descriptor client, and object IDs.
- `nx_bridge.py`: live NX command executor and manual bridge lifecycle.
- `contracts.py`: Pydantic input/output and error contracts.

All 189 tools are always registered: `certified.py` defines the core schemas and
`integration_server.py` with the `*_server.py` modules defines the rest, then
re-registers every tool behind one uniform proxy. `NX_MCP_SURFACE=agent` only
changes the presentation (13 discover/invoke entry points). Every public tool
needs a typed definition and a handler in `HardenedExecutor`; there is no untyped
fallback and no arbitrary journal-execution tool.

On NX 2406 the NX side runs as an NX startup application,
`nx_app/startup/nx_mcp_startup.py`: its first line `# nx: threaded` keeps it
loaded for the session and a UI-thread timer ticks the host (driver "startup").
Keep that line first. Never hold NX's main loop in a long-running journal: NX
work that needs the loop (first Modeling start, first PDF export) then freezes
NX. `examples/` holds the 2606 timer journal, the batch `start_nx_bridge.py` and
the older 2406 resident journals (freeze-prone, not for real work). Live NX
acceptance suites live in `acceptance/`.

## Live NX development loop

Start NX yourself with the startup app in development mode:
`python scripts/dev_start_nx.py --user-dir nx_app --env NX_MCP_DEV=1` (close a
hung NX with `taskkill //IM ugraf.exe //F`; `scripts/dev_nx_windows.py` shows
blocking NX dialogs). Hold `scripts/dev_lock.py` while using NX. Edit code,
then `python scripts/dev_reload.py --wait-agent` reloads the NX-side host from
disk (exit 1 prints the import traceback); bootstrap changes need an NX
restart. `scripts/dev_python.py` inspects NXOpen inside NX. Drive the new
tool through a fresh `python -m nx_mcp.server` stdio client with
`NX_MCP_BRIDGE_DESCRIPTOR=%LOCALAPPDATA%\nx-mcp\interactive-bridge.json`, on
disposable parts under `<workspace>/smoke/`; check results with measurements,
`nx_model_health` and `nx_screenshot`. The engineer's open parts stay loaded:
never modify, save or close parts you did not create.

## Conventions

- Keep NXOpen imports inside the NX bridge lifecycle; the sidecar must import
  without NX installed.
- Register public tools explicitly. Every input and output has a typed schema.
- File operations must pass `Workspace` validation on both process boundaries.
- Pass opaque object IDs between calls; never pass display-name strings to NX
  builders where an NX object is required.
- Every model mutation gets a visible undo mark and rolls back on failure.
- Builders use create → configure → commit → `Destroy()` in `finally`.
- Preserve the native NX exception code as `nx_code`; use stable domain error
  codes for callers.
- New default tools require local MCP/bridge tests and a real-NX test on the
  supported build.
- `fake_nx` tests verify only the simulated NXOpen seam; the `real_nx` marker
  is the release acceptance gate on the dedicated NX runner.
