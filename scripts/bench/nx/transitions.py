# 2. The host's control("manual") / control("agent") as implemented, inside NX.
# Runs inside execute() in agent mode: the host has released its LockAccess for
# this call. We re-take it first (between-call agent state, lock count 1), then
# cycle manual <-> agent and end in agent mode with the lock held, so execute()'s
# finally sees Lock and the host is exactly as before.
import json
import time

import nx_mcp.interactive as I

h = I._host
ui = h.ui
S = h.nx.UI.Status
x = h.executor
N = BENCH_ARGS.get("n", 50)
populate = BENCH_ARGS.get("populate", True)
pc = time.perf_counter
out = {}


def rec(name, value):
    out.setdefault(name, []).append(value)


assert h.mode == "agent" and h.owns_lock, (h.mode, h.owns_lock)
ui.LockAccess()
work = session.Parts.Work
parts = list(session.Parts)
refs = []
orig_release, orig_forget = h._release_to_engineer, h._forget_agent_session


def timed(name, fn):
    def wrapper(*a, **k):
        t = pc()
        try:
            return fn(*a, **k)
        finally:
            rec(name, (pc() - t) * 1000)

    return wrapper


h._release_to_engineer = timed("  manual: _release_to_engineer (EnableWindow+Unlock)", orig_release)
h._forget_agent_session = timed("  manual: _forget_agent_session (bookkeeping)", orig_forget)
try:
    for _i in range(N):
        if populate and work is not None:
            t = pc()
            snap = x._snapshot(work)  # registers a reference per model object
            rec("(untimed setup) register refs via executor._snapshot", (pc() - t) * 1000)
            refs.append(len(x.objects._objects))
        t0 = pc()
        h.control("manual")
        t1 = pc()
        h._refresh_panel()
        t2 = pc()
        h.control("agent")
        t3 = pc()
        h._refresh_panel()
        t4 = pc()
        rec("control('manual')", (t1 - t0) * 1000)
        rec("  panel refresh after manual", (t2 - t1) * 1000)
        rec("control('agent')", (t3 - t2) * 1000)
        rec("  panel refresh after agent", (t4 - t3) * 1000)
        rec("full agent->manual->agent cycle incl. panel", (t4 - t0) * 1000)
    # publish() = status() + panel + ui-state.json write (twice per agent call today)
    for _i in range(N):
        t = pc()
        h.publish()
        rec("host.publish() (status+panel+json write)", (pc() - t) * 1000)
        t = pc()
        h.status()
        rec("host.status()", (pc() - t) * 1000)
finally:
    del h._release_to_engineer, h._forget_agent_session
    if h.mode != "agent":
        h.control("agent")
state = {
    "mode": h.mode,
    "owns_lock": h.owns_lock,
    "lock": str(ui.AskLockStatus()),
    "window_enabled": bool(h.user.IsWindowEnabled(h.main_hwnd)),
    "parts": len(parts),
    "work": work.Leaf if work else None,
    "features": len(list(work.Features)) if work else 0,
    "bodies": len(list(work.Bodies)) if work else 0,
    "refs_before_manual": (min(refs), max(refs)) if refs else 0,
}
print("JSON:" + json.dumps({"state": state, "ms": {k: summarize(v) for k, v in out.items()}}))
