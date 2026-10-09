"""3b. Where the non-NX part of a call's latency goes: socket, worker thread, tick.

Measures client latency of the worker-thread-only snapshot read
(nx_ui_control status: never touches the UI thread) and of nx_status (one tick),
and records inside NX when the bridge worker thread enqueues each request relative
to the UI-thread ticks (does the worker only get the GIL while a tick runs?).

    python scripts/bench/bench_transport.py --n 50
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DESCRIPTOR, client, nx_json, save, stats, table  # noqa: E402

pc = time.perf_counter

INSTALL = """
import time, threading
import nx_mcp.interactive as I
h = I._host
pc = time.perf_counter
T = h.__dict__.setdefault("_transport", {"ticks": [], "enq": [], "conn": []})
cls_tick = type(h).tick
def tick(*a):
    t = pc()
    try:
        return cls_tick(h, *a)
    finally:
        T["ticks"].append((t, pc()))
        del T["ticks"][:-4000]
disp = h.dispatcher
orig_call = type(disp).call
def call(method, params):
    T["enq"].append((pc(), method))
    return orig_call(disp, method, params)
orig_dispatch = h.server._server.executor if hasattr(h.server._server, "executor") else None
disp.call = call
h.tick = tick
print("JSON:{}")
"""

COLLECT = """
import json, bisect, sys
import nx_mcp.interactive as I
h = I._host
T = h.__dict__.pop("_transport")
h.__dict__.pop("tick", None)
h.dispatcher.__dict__.pop("call", None)
ticks = sorted(T["ticks"])
starts = [s for s, e in ticks]
inside, gap_after_tick_end, periods, durs = 0, [], [], []
for t, m in T["enq"]:
    i = bisect.bisect_right(starts, t) - 1
    if i >= 0:
        s, e = ticks[i]
        if t <= e:
            inside += 1
        else:
            gap_after_tick_end.append((t - e) * 1000)
for (s0, e0), (s1, e1) in zip(ticks, ticks[1:]):
    periods.append((s1 - s0) * 1000)
for s, e in ticks:
    durs.append((e - s) * 1000)
print("JSON:" + json.dumps({
    "requests": len(T["enq"]), "enqueued_while_a_tick_ran": inside,
    "enqueue_after_tick_end_ms": summarize(gap_after_tick_end),
    "tick period ms": summarize(periods), "tick duration ms": summarize(durs),
    "switchinterval_s": sys.getswitchinterval(),
}))
"""


async def series(n):
    desc = json.loads(Path(DESCRIPTOR).read_text())
    c = client()
    out = {
        "tcp connect only (127.0.0.1)": [],
        "nx_ui_control status (worker thread only)": [],
        "nx_status (one UI tick)": [],
    }
    for _ in range(n):
        t = pc()
        r, w = await asyncio.open_connection(desc["host"], desc["port"])
        out["tcp connect only (127.0.0.1)"].append((pc() - t) * 1000)
        w.close()
        t = pc()
        await c.call("nx_ui_control", {"mode": "status"})
        out["nx_ui_control status (worker thread only)"].append((pc() - t) * 1000)
        t = pc()
        await c.call("nx_status", {})
        out["nx_status (one UI tick)"].append((pc() - t) * 1000)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    args = parser.parse_args()

    from common import HERE

    prelude = (HERE / "nx" / "_prelude.py").read_text()
    nx_json(INSTALL)
    try:
        data = asyncio.run(series(args.n))
    finally:
        inner = nx_json(prelude + COLLECT)
    rows = [(k, stats(v)) for k, v in data.items()]
    table(rows, "Transport")
    print(json.dumps(inner, indent=1))
    save("transport", {"client": dict(rows), "nx": inner})


if __name__ == "__main__":
    main()
