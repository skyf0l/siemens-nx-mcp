"""Shared helpers for the control-handover benchmarks (development loop only).

Hold scripts/dev_lock.py while running any of these; they run code inside NX
through nx_dev_python (NX_MCP_DEV=1) and never change files under src/.
"""

import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

from nx_mcp.bridge import DescriptorBridgeClient

HERE = Path(__file__).resolve().parent
DESCRIPTOR = os.environ.get(
    "NX_MCP_BRIDGE_DESCRIPTOR",
    str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
)
WORKSPACE = Path(os.environ.get("NX_MCP_WORKSPACE", r"C:\Users\testuser\NX_MCP_WORKSPACE"))
SMOKE = "smoke/bench_control"
OUT = WORKSPACE / SMOKE / "results"


def client():
    return DescriptorBridgeClient(DESCRIPTOR, timeout=300)


def nx(code, **values):
    """Run code inside NX; `values` are injected as a JSON global BENCH_ARGS."""
    if values:
        code = f"BENCH_ARGS = __import__('json').loads({json.dumps(json.dumps(values))})\n" + code
    result = asyncio.run(client().call("nx_dev_python", {"code": code}))
    if result.get("status") == "error":
        raise RuntimeError(json.dumps(result, indent=1)[:4000])
    return result


def nx_json(code, **values):
    """Run code that prints exactly one JSON line prefixed with 'JSON:'."""
    out = nx(code, **values)["stdout"]
    for line in out.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    raise RuntimeError("no JSON in output:\n" + out[-4000:])


def snippet(name):
    prelude = (HERE / "nx" / "_prelude.py").read_text(encoding="utf-8")
    return prelude + "\n" + (HERE / "nx" / name).read_text(encoding="utf-8")


def stats(values):
    """p50 / p95 / max / mean in milliseconds of a list of seconds (or ms with ms=True)."""
    xs = sorted(values)
    if not xs:
        return {"n": 0}

    def pct(p):
        k = (len(xs) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

    return {
        "n": len(xs),
        "p50": round(pct(0.50), 3),
        "p95": round(pct(0.95), 3),
        "max": round(xs[-1], 3),
        "mean": round(statistics.fmean(xs), 3),
    }


def table(rows, title=None):
    if title:
        print(f"\n== {title}")
    print(f"{'measurement':58} {'n':>4} {'p50':>9} {'p95':>9} {'max':>9}  (ms)")
    for name, s in rows:
        if s.get("n"):
            print(f"{name:58} {s['n']:>4} {s['p50']:>9.3f} {s['p95']:>9.3f} {s['max']:>9.3f}")
        else:
            print(f"{name:58}  n/a")


def save(name, payload):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(f"saved {path}", file=sys.stderr)
