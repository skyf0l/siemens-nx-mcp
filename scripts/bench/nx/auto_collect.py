# Collects the harness measurements; with BENCH_ARGS["restore"] removes the
# harness and leaves the agent in control (LockAccess held, window disabled),
# exactly as the development loop starts.
import json

import nx_mcp.interactive as I

h = I._host
B = h.__dict__.get("_bench") or {}
cycles = [c for c in B.get("cycles", []) if c.get("method") != "nx_dev_python"]
out = {"variant": B.get("variant"), "invalidations": B.get("invalidations")}
for method in sorted({c["method"] for c in cycles}):
    cs = [c for c in cycles if c["method"] == method]
    out[f"cycles {method}"] = {
        k: summarize([c[k] for c in cs if k in c])
        for k in ("take_ms", "run_ms", "release_ms", "cycle_ms")
    }
    out[f"cycles {method}"]["changed"] = sum(1 for c in cs if c.get("changed"))
waits = [w for w in B.get("waits", []) if w["method"] != "nx_dev_python"]
for method in sorted({w["method"] for w in waits}):
    out[f"queue wait {method}"] = summarize(
        [w["wait_ms"] for w in waits if w["method"] == method and w["wait_ms"] is not None]
    )
    out[f"host.execute {method}"] = summarize(
        [w["exec_ms"] for w in waits if w["method"] == method]
    )
ev = [e for e in B.get("busy_events", []) if "start" in e]
out["busy: free -> start (ms)"] = summarize([(e["start"] - e["free_at"]) * 1000 for e in ev])
out["busy: free -> next tick sees it (ms)"] = summarize(
    [(e["seen_free"] - e["free_at"]) * 1000 for e in ev]
)
out["busy checks while held (ms)"] = summarize(B.get("busy_checks", []))
if BENCH_ARGS.get("reset"):
    B.update(cycles=[], waits=[], busy_events=[], busy_checks=[], invalidations=0)
if BENCH_ARGS.get("restore") and B:
    B["restore"] = True  # the auto tick keeps agent control after this call
    B["active"] = False
    d = h.dispatcher
    d.__dict__.pop("call", None)
    d._executor = B["orig_exec"]
    h.__dict__.pop("tick", None)
    if B.get("busy_until") is not None:
        h.ui.UnlockAccess()
        B["busy_until"] = None
    h.__dict__.pop("_bench", None)
out["mode_now"] = h.mode
print("JSON:" + json.dumps(out))
