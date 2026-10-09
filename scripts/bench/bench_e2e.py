"""3 + 5. End-to-end latency as the agent sees it, agent mode vs simulated auto mode.

Phases (each restores agent control at its end):
  agent       agent has control (today's dev default)
  auto_full   engineer has control; each call: control('agent') -> run -> control('manual')
  auto_light  engineer has control; each call: EnableWindow only, no LockAccess, references
              invalidated only when the undo-mark/part fingerprint moved; plus the busy test

The harness is installed at runtime through nx_dev_python (scripts/bench/nx/auto_install.py);
nothing under src/ changes. Work part must be a bench part under smoke/bench_control/.

    python scripts/bench/bench_e2e.py --phase agent --n 50
    python scripts/bench/bench_e2e.py --phase auto_light --n 50 --busy 50
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DESCRIPTOR, client, nx_json, save, snippet, stats, table  # noqa: E402

pc = time.perf_counter
Z = [400.0]


def datum_args():
    Z[0] += 1.0
    return {"method": "point_normal", "origin": [0, 0, Z[0]], "normal": [0, 0, 1]}


async def bridge_series(n):
    c = client()
    out = {"bridge nx_status": [], "bridge nx_list_parts": [], "bridge nx_create_datum_plane": []}
    for _ in range(n):
        for tool, args in (
            ("nx_status", {}),
            ("nx_list_parts", {}),
            (
                "nx_create_datum_plane",
                {**datum_args(), "operation_id": "bench-" + uuid.uuid4().hex[:10]},
            ),
        ):
            t = pc()
            r = await c.call(tool, args)
            out[f"bridge {tool}"].append((pc() - t) * 1000)
            if tool != "nx_status" and not r.get("ok", True):
                raise RuntimeError(json.dumps(r)[:600])
    return out


async def sidecar_series(n):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ, NX_MCP_BRIDGE_DESCRIPTOR=DESCRIPTOR, NX_MCP_SURFACE="agent")
    params = StdioServerParameters(command=sys.executable, args=["-m", "nx_mcp.server"], env=env)
    out = {"sidecar nx_invoke nx_list_parts": [], "sidecar nx_invoke nx_create_datum_plane": []}
    with open(os.devnull, "w") as quiet:
        async with stdio_client(params, errlog=quiet) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            await s.call_tool("nx_invoke", {"tool": "nx_list_parts", "arguments": {}})  # warm
            for _ in range(n):
                for tool, args in (("nx_list_parts", {}), ("nx_create_datum_plane", datum_args())):
                    if tool != "nx_list_parts":
                        args = {**args, "operation_id": "bench-" + uuid.uuid4().hex[:10]}
                    t = pc()
                    res = await s.call_tool("nx_invoke", {"tool": tool, "arguments": args})
                    out[f"sidecar nx_invoke {tool}"].append((pc() - t) * 1000)
                    if res.isError:
                        raise RuntimeError(str(res.content)[:600])
    return out


BUSY = """
import time, random
import nx_mcp.interactive as I
B = I._host._bench
I._host.ui.LockAccess()          # simulated engineer: NX UI locked (as in a command/dialog)
B["busy_until"] = time.perf_counter() + BENCH_ARGS["hold"]
print("JSON:{}")
"""


async def busy_series(n):
    c = client()
    out = {
        "busy: client latency of call sent while NX busy (incl. hold)": [],
        "busy: client latency minus hold": [],
    }
    for _ in range(n):
        hold = random.uniform(0.15, 0.4)
        code = f"BENCH_ARGS = {{'hold': {hold}}}\n" + BUSY
        await c.call("nx_dev_python", {"code": code})
        t = pc()
        await c.call("nx_list_parts", {})
        dt = (pc() - t) * 1000
        out["busy: client latency of call sent while NX busy (incl. hold)"].append(dt)
        out["busy: client latency minus hold"].append(dt - hold * 1000)
    return out


def wait_mode(want, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = asyncio.run(client().call("nx_status", {}))
        if r.get("ui", {}).get("mode") == want:
            return
        time.sleep(0.1)
    raise RuntimeError(f"host did not reach mode {want}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--phase", choices=["agent", "auto_full", "auto_light"], required=True)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--busy", type=int, default=0)
    parser.add_argument("--no-sidecar", action="store_true")
    args = parser.parse_args()
    variant = "light" if args.phase == "auto_light" else "full"
    print(nx_json(snippet("auto_install.py"), variant=variant, activate=args.phase != "agent"))
    client_ms = {}
    try:
        if args.phase != "agent":
            wait_mode("manual")
        asyncio.run(bridge_series(3))  # warm-up, discarded
        nx_json(snippet("auto_collect.py"), reset=True)
        client_ms.update(asyncio.run(bridge_series(args.n)))
        if not args.no_sidecar:
            client_ms.update(asyncio.run(sidecar_series(args.n)))
        if args.busy:
            client_ms.update(asyncio.run(busy_series(args.busy)))
    finally:
        inner = nx_json(snippet("auto_collect.py"), restore=True)
        state = asyncio.run(client().call("nx_status", {}))["ui"]
    rows = [(k, stats(v)) for k, v in client_ms.items()]
    table(rows, f"Client-side latency ({args.phase})")
    print("\nNX side:", json.dumps(inner, indent=1))
    keep = (
        "mode",
        "ui_locked_by_bridge",
        "actual_ui_lock",
        "nx_window_input_enabled",
        "can_open_part",
    )
    print("final host state:", {k: state.get(k) for k in keep})
    save(
        f"e2e_{args.phase}",
        {"client": dict(rows), "nx": inner, "final": {k: state.get(k) for k in keep}},
    )


if __name__ == "__main__":
    main()
