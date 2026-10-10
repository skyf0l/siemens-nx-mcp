# NX MCP Server

NX MCP lets an MCP client inspect and edit Siemens NX (Windows, NX 2406 and NX 2606)
through typed tools. Two processes split the work:

```text
MCP client -> sidecar (python -m nx_mcp.server: stdio, schemas, validation, paths)
           -> authenticated loopback bridge
           -> NX-side host -> host/dispatch.py -> core/kernel.py -> NXOpen
```

The sidecar imports without NX. Every public tool is one `@tool` function in
`src/nx_mcp/domains/<domain>/`; the catalogue (`nx_mcp.core.catalogue`) currently holds
148 tools in 10 domains, listed with their arguments in the generated
[tool reference](docs/tools.md). The kernel runs every call through the same gates
(validation, workspace paths, evidence, part ownership, undo mark and rollback, receipts).
There is no arbitrary journal-execution tool. Design: [architecture](docs/design/architecture.md)
and [tool surface](docs/design/tool-surface.md).

## Install

Python 3.10+ in the sidecar environment:

```powershell
python -m pip install -e ".[dev]"
python -m pre_commit install --install-hooks
```

The NX side loads this checkout's `src` directory; it needs no `mcp` or `pydantic`.

## Start the NX side

NX and the sidecar must use the same workspace. Both default to
`<home>\NX_MCP_WORKSPACE` (for example `C:\Users\<you>\NX_MCP_WORKSPACE`); set
`NX_MCP_WORKSPACE` for both to use another directory. Then:

- **NX 2406, every session:** point NX at this checkout's `nx_app` folder once
  (`UGII_USER_DIR=<checkout>\nx_app`, or a line in `custom_dirs.dat`) and start NX
  normally. NX loads the startup application `nx_app/startup/nx_mcp_startup.py`
  and keeps it loaded; nothing is played.
- **NX 2406, one session:** in an NX started without `nx_app`, play
  `nx_app/journals/start_2406.py` (Alt+F8, Run). It starts the same host and panel
  and returns; the bridge stays up until NX exits. Playing it again, or in an NX
  that already runs the startup application, only reports that the bridge is running.
- **NX 2606:** in graphical NX, play `nx_app/journals/start_2606.py`. It
  returns; a Win32 timer runs queued calls on the NX UI thread.
- **Batch:** `nx_app/journals/start_batch.py` under `run_journal.exe`.

Details, panel and troubleshooting: [INTERACTIVE-NX.md](INTERACTIVE-NX.md). Do not
attach batch and graphical hosts to the same workspace.

## Configure the sidecar

```powershell
$env:NX_MCP_WORKSPACE = 'C:\CAD\NX_MCP_WORKSPACE'  # optional: only if NX uses it too
$env:NX_MCP_BRIDGE_DESCRIPTOR = Join-Path $env:LOCALAPPDATA 'nx-mcp\interactive-bridge.json'
python -m nx_mcp.server
```

| Variable | Meaning |
| --- | --- |
| `NX_MCP_WORKSPACE` | Workspace shared by NX and the sidecar, default `<home>\NX_MCP_WORKSPACE`; set the same value for both or neither. Every path must resolve inside it |
| `NX_MCP_BRIDGE_DESCRIPTOR` | Bridge descriptor written by the NX host (port, token, NX build) |
| `NX_MCP_SURFACE` | `agent` (default): 4 gateways (`nx_find_tools`, `nx_get_tool_schemas`, `nx_invoke`, `nx_invoke_read`) plus 14 core tools. `full`: the gateways plus every catalogue tool tested on the connected build |
| `NX_MCP_DEV` | `1` only in the development loop: untested tools become visible and callable |

The optional `python -m nx_mcp.http_surface` serves both profiles on
`127.0.0.1:8765` (`NX_MCP_HTTP_HOST`/`NX_MCP_HTTP_PORT`). The bridge token does not
protect the HTTP endpoint, so it refuses non-loopback addresses unless
`NX_MCP_HTTP_ALLOW_REMOTE=1` is set behind separate access control.

## Working with the engineer

The NX MCP control panel has two modes: **Auto** (default, also in development;
you keep working, each agent step takes NX only while it runs, waits while you are
in a command or dialog, and never runs under a modal NX dialog) and **Ask me
first** (every step waits for your answer). Only you change the mode; no tool does.
The agent may change only parts it created or opened, or a work part you grant with
**Allow agent to change <part>**. See [INTERACTIVE-NX.md](INTERACTIVE-NX.md).

## Calls, references and recovery

Pass returned opaque references, never display names; they can go stale after
undo, close or engineer edits and are re-acquired through `nx_list_objects` or
`nx_find_geometry`. Give mutations an `operation_id`; after an uncertain outcome
call `nx_get_operation` before retrying, and a repeat with the same ID and
arguments replays the recorded result. Every model mutation runs under an undo
mark and rolls back on failure.

## Evidence

A tool is available on an NX build only when the acceptance runner recorded
machine-checked evidence for it: scenarios in `acceptance/<domain>/`, replayed
through a real sidecar by `python -m nx_mcp.acceptance run`, folded into
`src/nx_mcp/evidence/*.json` by `python -m nx_mcp.acceptance fold`. Untested tools
are hidden and refused in production. Per-tool scope:
[capability matrix](docs/capability-matrix.md). Evidence covers the recorded
scenarios, not every option of an NX builder.

## Quality gates

```powershell
python -m pre_commit run --all-files
python -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
python -m mypy src/nx_mcp
```

Pre-commit runs ruff, ruff-format, and checks that `docs/capability-matrix.md` and
`docs/tools.md` match the catalogue (regenerate with
`scripts/generate_capability_matrix.py` and `scripts/generate_tool_reference.py`).
Fake-NX tests check the NXOpen seam only; the `real_nx` acceptance runs on the
dedicated NX runner are the release gate. Older documents and evidence logs are in
[docs/history/](docs/history/README.md).

## License

MIT; the fork retains upstream history. Private CAD and machine provisioning are excluded.
