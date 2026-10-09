"""Real-NX smoke: a short modeling workflow, repeated, plus transport recovery.

Every call goes through the acceptance runner's client (a fresh stdio
``python -m nx_mcp.server``, agent profile, catalogue tools through
``nx_invoke``/``nx_invoke_read``) and is checked with the runner's ``expect``
vocabulary. Each iteration builds a block in a new part under
``smoke/<run>/<n>/``, checks the exact retry of a committed call (replayed, no
second body) and a conflicting reuse of its operation id, undoes and recreates
the extrude, exports STEP and closes the part unsaved.

Transport recovery runs once: a mutation sent straight to the bridge whose
connection is dropped before the answer must still commit, report ``committed``
through ``nx_get_operation`` and replay (not run again) when retried.

The full per-tool acceptance is ``python -m nx_mcp.acceptance run``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from nx_mcp.acceptance import runner
from nx_mcp.bridge import BRIDGE_PROTOCOL_VERSION

PROFILE = "$profile.data.sketch.id"
EXTRUDE = {"sketch": PROFILE, "end": {"type": "distance", "value": 12.5}}

STEPS: list[dict[str, Any]] = [
    {
        "call": "nx_get_status",
        "args": {"include": ["bridge"]},
        "expect": {"data.bridge.state": "connected"},
    },
    {"call": "nx_create_part", "setup": True, "args": {"path": "$part", "units": "mm"}},
    {
        "call": "nx_create_sketch",
        "setup": True,
        "save": "profile",
        "args": {"placement": {"principal": "xy"}, "name": "PROFILE"},
    },
    {
        "call": "nx_create_sketch_curves",
        "setup": True,
        "args": {
            "sketch": PROFILE,
            "curves": [{"type": "rectangle", "corner1": [0, 0], "corner2": [20, 10]}],
        },
    },
    {"call": "nx_finish_sketch", "setup": True, "args": {"sketch": PROFILE}},
    {
        "call": "nx_create_extrude",
        "save": "extrude",
        "args": EXTRUDE,
        "expect": {"mutation_outcome": "committed", "data.bodies": {"count": 1}},
    },
    {
        "call": "nx_create_extrude",
        "args": {**EXTRUDE, "operation_id": "$extrude.operation_id"},
        "intent": "Smoke: retry a committed extrude with its operation id",
        "expect": {"replayed": True, "data.bodies": {"count": 1}},
    },
    {
        "call": "nx_create_extrude",
        "args": {
            **EXTRUDE,
            "end": {"type": "distance", "value": 3},
            "operation_id": "$extrude.operation_id",
        },  # fmt: skip
        "intent": "Smoke: reuse the operation id with other arguments",
        "expect": {"error.code": "NX_IDEMPOTENCY_CONFLICT", "mutation_outcome": "not_started"},
    },
    {"call": "nx_list_objects", "args": {"kind": "body"}, "expect": {"page.total": 1}},
    {
        "call": "nx_measure_mass",
        "args": {"scope": "part"},
        "expect": {"data.volume": {"close": 2500, "tol": 1e-6}},
    },
    {"call": "nx_set_camera", "args": {"fit": True}},
    {"call": "nx_undo_operation", "args": {}, "expect": {"mutation_outcome": "committed"}},
    {"call": "nx_list_objects", "args": {"kind": "body"}, "expect": {"page.total": 0}},
    {"call": "nx_list_objects", "save": "sketches", "args": {"kind": "sketch"}},
    {
        "call": "nx_create_extrude",
        "save": "again",
        "args": {"sketch": "$sketches.data.items.0.id", "end": {"type": "distance", "value": 12.5}},
        "expect": {"mutation_outcome": "committed", "data.bodies": {"count": 1}},
    },
    {
        "call": "nx_export_model",
        "args": {"path": "$run/block.stp", "format": "step"},
        "expect": {"mutation_outcome": "committed"},
    },
]


def _failures(results: list[runner.StepResult], expected: int) -> list[str]:
    problems = [f"[{r.index}] {r.call}: {f}" for r in results for f in r.failures]
    if len(results) < expected and not problems:
        problems.append(f"stopped after step {len(results)} of {expected}")
    return problems


async def run_iteration(session, run_dir: Path, index: int) -> dict[str, Any]:
    folder = run_dir / f"{index:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    special = {"$run": str(folder), "$part": str(folder / f"smoke_{run_dir.name}_{index:02d}.prt")}
    before = await runner.loaded_parts(session)
    try:
        results = await runner.run_steps(session, STEPS, special, label=f"smoke {index}")
    finally:
        cleanup = await runner.close_new_parts(session, before, "close")
    problems = _failures(results, len(STEPS)) + cleanup["failed"]
    if problems:
        raise RuntimeError(f"Smoke iteration {index} failed: " + "; ".join(problems))
    status, again = results[0].result, results[-2].result
    if not (folder / "block.stp").is_file():
        raise RuntimeError("The STEP export file was not created")
    return {
        "iteration": index,
        "nx_version": status["data"]["bridge"]["nx_version"],
        "feature_id": again["data"]["feature"]["id"],
        "body_id": again["data"]["bodies"][0]["id"],
    }


def abandon(method: str, params: dict[str, Any]) -> None:
    """Send one bridge request and drop the connection before the answer."""
    path = runner.descriptor_path()
    if path is None:
        raise RuntimeError("No bridge descriptor (set NX_MCP_BRIDGE_DESCRIPTOR)")
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    request = {
        "jsonrpc": "2.0",
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "id": uuid.uuid4().hex,
        "token": descriptor["token"],
        "method": method,
        "params": params,
    }
    with socket.create_connection((descriptor["host"], descriptor["port"]), timeout=10) as link:
        link.sendall(json.dumps(request).encode() + b"\n")


async def transport_recovery(session, run_dir: Path, *, wait: float = 120.0) -> dict[str, Any]:
    """An abandoned mutation commits once; its receipt answers the retry."""
    folder = run_dir / "transport"
    folder.mkdir(parents=True, exist_ok=True)
    part = str(folder / f"smoke_{run_dir.name}_transport.prt")
    operation = "op_" + uuid.uuid4().hex
    args = {"name": "smoke_abandoned", "formula": "7", "units": "mm"}
    before = await runner.loaded_parts(session)
    try:
        created = await runner.call_tool(
            session, "nx_create_part", {"path": part, "units": "mm"}, intent="Smoke: transport part"
        )
        if not created.get("ok"):
            raise RuntimeError(f"nx_create_part failed: {created.get('error')}")
        abandon("nx_set_expression", {**args, "operation_id": operation,
                                      "intent": "Smoke: a call whose client disconnects"})  # fmt: skip
        deadline = time.monotonic() + wait
        while True:
            state = await runner.call_tool(
                session, "nx_get_operation", {"operation": operation}, intent=None
            )
            if (state.get("data") or {}).get("state") == "committed":
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"The abandoned call never committed: {state}")
            await asyncio.sleep(1)
        steps = [
            {
                "call": "nx_set_expression",
                "args": {**args, "operation_id": operation},
                "expect": {"replayed": True, "data.value": {"close": 7, "tol": 1e-12}},
            },
            {
                "call": "nx_set_expression",
                "args": {**args, "formula": "8", "operation_id": operation},
                "expect": {
                    "error.code": "NX_IDEMPOTENCY_CONFLICT",
                    "mutation_outcome": "not_started",
                },  # fmt: skip
            },
        ]
        results = await runner.run_steps(session, steps, {}, label="smoke transport")
    finally:
        cleanup = await runner.close_new_parts(session, before, "close")
    problems = _failures(results, len(steps)) + cleanup["failed"]
    if problems:
        raise RuntimeError("Transport recovery failed: " + "; ".join(problems))
    return {"operation_id": operation, "committed": True, "replayed": True}


async def run(
    workspace: Path, iterations: int, prefix: str = "smoke", *, transport: bool = True
) -> list[dict[str, Any]]:
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    run_dir = workspace / "smoke" / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}"
    lock = runner.DevLock(workspace, f"real-smoke-{prefix}")
    lock.acquire()
    try:
        async with runner.sidecar(workspace) as session:
            results = []
            for index in range(1, iterations + 1):
                lock.refresh()
                results.append(await run_iteration(session, run_dir, index))
            if transport:
                results.append({"transport": await transport_recovery(session, run_dir)})
            return results
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--run-prefix", default="smoke")
    parser.add_argument("--no-transport", action="store_true", help="skip transport recovery")
    arguments = parser.parse_args()
    if arguments.iterations < 1:
        parser.error("--iterations must be at least 1")
    if not arguments.run_prefix.strip():
        parser.error("--run-prefix must not be empty")
    workspace = arguments.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    results = asyncio.run(
        run(
            workspace,
            arguments.iterations,
            arguments.run_prefix,
            transport=not arguments.no_transport,
        )
    )
    print(json.dumps({"status": "passed", "runs": results}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
