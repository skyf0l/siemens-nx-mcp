# NX MCP Server

NX MCP lets an MCP client inspect and edit Siemens NX through an NX-owned bridge.
The sidecar validates requests and manages transport; NXOpen calls run serially
on the NX thread. The sidecar imports without NX installed.

```text
MCP client → Python sidecar → authenticated loopback bridge → NXOpen / NX
```

This fork targets **NX 2606 on Windows**. Upstream's NX 2506 batch evidence is
historical and does not establish cross-version compatibility for these additions.
See the [capability matrix](docs/capability-matrix.md) for per-tool evidence and
limits. “Tested” applies to the recorded fixtures, not every option of a builder.

## Choose a tool surface

All 189 tools are always available. `NX_MCP_SURFACE` only chooses how they are
presented to the MCP client:

| Surface | What the client sees | Configuration |
| --- | --- | --- |
| Full (default) | All 189 tools listed directly | nothing, or `NX_MCP_SURFACE=full` |
| Agent | 13 entry points; discover/invoke the 189 tools on demand | `NX_MCP_SURFACE=agent` |

`NX_MCP_WORKSPACE` is required. Per-tool native test status is reported by
`nx_capabilities`. There is no arbitrary journal execution tool; every operation
goes through a typed tool.

## Start graphical NX

Install Python 3.10+ and the package in the external sidecar environment:

```powershell
python -m pip install -e ".[dev]"
```

1. Set `NX_MCP_WORKSPACE` in the NX environment to a dedicated CAD directory,
   such as `D:\NX_MCP_WORKSPACE`.
2. Start the NX side of the bridge:
   - **NX 2406:** point NX at this checkout's `nx_app` folder once
     (`UGII_USER_DIR=<checkout>\nx_app`, or a `custom_dirs.dat` entry) and start
     NX normally. NX loads `nx_app/startup/nx_mcp_startup.py` at launch and
     keeps it loaded; no journal is played. Do not use the resident
     `start_nx_interactive_2406.py` journal for real work: it can freeze NX.
   - **NX 2606:** in graphical NX, play `examples/start_nx_interactive_2606.py`.
     The journal returns; a retained Win32 timer dispatches queued calls on the
     NX UI thread.
   See [graphical lifecycle](INTERACTIVE-NX.md).
3. Start the sidecar using the same workspace and the graphical descriptor:

```powershell
$env:NX_MCP_WORKSPACE = 'D:\NX_MCP_WORKSPACE'
$env:NX_MCP_BRIDGE_DESCRIPTOR = Join-Path $env:LOCALAPPDATA 'nx-mcp\interactive-bridge.json'
$env:NX_MCP_SURFACE = 'agent'
python -m nx_mcp.server
```

Configure the MCP client with that executable, arguments and environment. The NX
journal loads this checkout's `src` directory; the NX-side process does not need
`mcp` or `pydantic`. Do not attach batch and graphical hosts to the same workspace.
See [graphical lifecycle](INTERACTIVE-NX.md) and [setup details](docs/tools.md).

The optional `python -m nx_mcp.http_surface` entrypoint serves `/mcp` and
`/agent/mcp`. It requires separate network access controls; loopback bridge
authentication does not authenticate the HTTP endpoint. Stdio avoids network exposure.

## Workflows

| Area | Features and contracts |
| --- | --- |
| Files and artifacts | [Nested folders, open/save paths](docs/tools.md), uploads/downloads, checksums, assembly dependency packages and inline PNGs |
| Inspection | [Assembly bounds, distance and interference](docs/tools.md), topology selection, validity and measured properties |
| Sketches and solids | [Curve editing, expressions and previews](docs/tools.md); [dimensions, relations and native patterns](docs/tools.md) |
| Display | [Visibility, color, transparency, collision highlights and sections](docs/tools.md); camera and render controls |
| Assemblies and drawings | [Exploded views](docs/tools.md), trace lines, BOMs, balloons and [editable annotations](docs/tools.md) |
| Sheet metal | [Native operations, flat patterns and bend tables](docs/tools.md); per-operation schemas and tested option scope |
| Freeform and direct editing | [Splines, meshes, bridge/trim/sew/thicken, face edits and sampled analysis](docs/tools.md) |
| Manufacturing | [Native threads, PMI/GD&T and annotation refresh](docs/tools.md) |
| Agent use | [Discovery, compact results, pagination, snapshots and retention](docs/agent-surface.md) |

## References, recovery and paths

Use returned opaque IDs rather than display names. References include owner and
session context and can become stale after close, rollback or manual handoff.
Reacquire them through inspection tools when that happens.

Assign a unique `operation_id` to mutations. After uncertain delivery, query
`nx_operation_status` before retrying. Reusing the same ID and arguments can
return the committed receipt without applying the mutation twice. Checkpoints
are session-bound; NX save can expire native undo marks. Recovery does not undo
arbitrary external file writes or survive process restart as a model checkpoint.

Paths refer to the NX host. Use explicit project subfolders; relative paths are
resolved from `NX_MCP_WORKSPACE`, and absolute paths must remain inside it.
Traversal, resolved links outside it and internal `.nx-mcp` files are rejected.
See [path semantics](docs/tools.md).

## Validation and limitations

Run the ordinary quality gates without NX:

```powershell
python -m pre_commit run --all-files
python -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
```

Hosted CI checks supported OS/Python combinations, sidecar types and branch
coverage. Fake NX tests cover API boundaries; they do not establish geometry
correctness. Native runners under `acceptance/validate_*.py` use disposable fixtures
and document their environment variables. See [native validation](docs/real-nx-validation.md)
and [release acceptance](docs/real-nx-validation.md).

The [validation guide](docs/real-nx-validation.md) records 918 automated passes
and scoped dev20–dev22 live checks. Historical receipts identify their runtime commits and are
not current-version blanket certification. Current experimental gaps are tracked
in [capability closeout](docs/capability-matrix.md).

NXOpen mutations are serialized. Long native calls can block graphical NX;
cancellation is cooperative between batch children. Sampled surface, thickness
and draft analysis does not establish global extrema or standards compliance.
Individual sheet-metal options retain narrower evidence than their tool family.

## Upstream contribution

[Draft PR #5](https://github.com/DreamEnding/NX_MCP/pull/5) proposes this integration.
The [review outline](docs/tools.md) describes possible extraction
boundaries. The fork retains upstream history and its MIT license; private CAD
and machine provisioning are excluded.
