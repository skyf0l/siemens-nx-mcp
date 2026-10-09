# Installs a RUNTIME-ONLY benchmark harness on the live host (nothing on disk
# changes): queue-wait stamps on the dispatcher and an "auto mode" tick that,
# while the engineer has control, takes control for one queued call, runs it and
# hands control straight back. Remove with auto_restore.py (or any dev reload).
#
# variant "full":  control('agent') + host.execute + control('manual') as implemented.
# variant "light": EnableWindow(False) + execute without LockAccess/UnlockAccess +
#                  EnableWindow(True); references invalidated only when the change
#                  fingerprint (newest undo mark id + mark count + parts) moved.
import contextlib
import time

import NXOpen.UF as UF

import nx_mcp.interactive as I

h = I._host
pc = time.perf_counter
uf = UF.UFSession.GetUFSession()
V = NXOpen.Session.MarkVisibility
B = h.__dict__.setdefault("_bench", {})
B.update(
    variant=BENCH_ARGS.get("variant", "full"),
    cycles=[],
    waits=[],
    enq={},
    busy_until=None,
    busy_events=[],
    restore=False,
    fingerprint=None,
    invalidations=0,
)
disp = h.dispatcher
cls_tick = type(h).tick


def fingerprint():
    s = session.Parts
    return (
        session.GetNewestUndoMark(V.AnyVisibility),
        uf.Undo.AskNumberOfMarks(UF.Undo.UserVisibility.ANY_VIS),
        tuple(int(p.Tag) for p in s),
        int(s.Work.Tag) if s.Work else 0,
        int(s.Display.Tag) if s.Display else 0,
    )


# --- queue-wait stamps (bridge worker thread -> UI thread start) -------------
orig_call = type(disp).call
orig_exec = disp._executor


def stamped_call(method, params):
    B["enq"][id(params)] = pc()
    try:
        return orig_call(disp, method, params)
    finally:
        B["enq"].pop(id(params), None)


def stamped_exec(method, params):
    t = B["enq"].get(id(params))
    rec = {"method": method, "mode": h.mode, "wait_ms": (pc() - t) * 1000 if t else None}
    t1 = pc()
    try:
        return orig_exec(method, params)
    finally:
        rec["exec_ms"] = (pc() - t1) * 1000
        B["waits"].append(rec)


disp.call = stamped_call
disp._executor = stamped_exec


# --- light execute: host.execute without the LockAccess/UnlockAccess pair ------
def light_execute(method, params):
    h.last_method = h.running_method = method
    h.operation_started = time.monotonic()
    h.current_summary = h._summarize(method, params)
    h.current_intent = params.get("intent")
    h.current_started_at = time.time()
    result = error = None
    try:
        h.publish()
        result = h.executor.execute(method, params)
        h.completed += 1
        part = session.Parts.Display
        if part and not I._read_only(method):
            part.ModelingViews.WorkView.UpdateDisplay()
        return result
    except BaseException as exc:
        error = exc
        raise
    finally:
        h.last_duration_seconds = time.monotonic() - h.operation_started
        h.activity.record(
            method,
            h.current_summary,
            started=h.current_started_at,
            duration=h.last_duration_seconds,
            intent=h.current_intent,
            read_only=I._model_unchanged(method),
            result=result,
            error=error,
        )
        h.running_method = h.operation_started = None
        h.current_summary = h.current_intent = h.current_started_at = None
        h.idle_since = time.time()
        h.publish()


def held(p):
    return not I.runs_while_engineer_has_control(p)


def ui_busy():
    return h.ui.AskLockStatus() == h.nx.UI.Status.Lock or not h.ui.CanOpenPart()


def auto_tick(*a):
    if h.busy or h.stopped or h.mode != "manual" or not B.get("active"):
        return cls_tick(h, *a)
    h.busy = True
    try:
        h.ticks += 1
        now = pc()
        # Simulated engineer activity: NX locked (as during a dialog) until busy_until.
        if B["busy_until"] is not None and now >= B["busy_until"]:
            h.ui.UnlockAccess()
            B["busy_events"].append({"free_at": B["busy_until"], "seen_free": now})
            B["busy_until"] = None
        if h.dispatcher.drain_where(I.runs_while_engineer_has_control):
            return
        waiting = [p for p in h.dispatcher.waiting() if held(p)]
        if not waiting:
            return
        t0 = pc()
        if ui_busy():
            B.setdefault("busy_checks", []).append((pc() - t0) * 1000)
            return  # engineer busy: the call waits for a later tick
        if B["busy_events"] and "start" not in B["busy_events"][-1]:
            B["busy_events"][-1]["start"] = t0
        c = {"method": waiting[0].method, "variant": B["variant"]}
        if B["variant"] == "full":
            h.control("agent")
            c["take_ms"] = (pc() - t0) * 1000
            t1 = pc()
            h.dispatcher.drain(timeout=0, limit=1)
            c["run_ms"] = (pc() - t1) * 1000
            t2 = pc()
            if B["restore"]:
                c["release_ms"] = 0.0
                B["cycles"].append(c)
                return
            h.control("manual")
            h._refresh_panel()
            c["release_ms"] = (pc() - t2) * 1000
        else:
            fp = fingerprint()
            changed = B["fingerprint"] is not None and fp != B["fingerprint"]
            if changed:  # the engineer changed something since the agent's last call
                h._forget_agent_session()
                B["invalidations"] += 1
            h.user.EnableWindow(h.main_hwnd, False)
            h.window_disabled = True
            h.mode, h.owns_lock = "agent", True  # owns_lock: execute() gate only
            h._refresh_panel()
            c["take_ms"] = (pc() - t0) * 1000
            c["changed"] = changed
            t1 = pc()
            h.dispatcher.drain(timeout=0, limit=1)
            c["run_ms"] = (pc() - t1) * 1000
            t2 = pc()
            if B["restore"]:
                h.owns_lock = False
                h.control("agent")  # real agent mode again: LockAccess + disabled window
                c["release_ms"] = 0.0
                B["cycles"].append(c)
                return
            B["fingerprint"] = fingerprint()
            h.owns_lock = False
            h.mode = "manual"
            h.user.EnableWindow(h.main_hwnd, True)
            h.window_disabled = False
            h._refresh_panel()
            c["release_ms"] = (pc() - t2) * 1000
        c["cycle_ms"] = (pc() - t0) * 1000
        B["cycles"].append(c)
    except BaseException as exc:
        h.last_error = "bench auto tick: " + repr(exc)
        with contextlib.suppress(BaseException):
            h.control("manual")
    finally:
        h.busy = False


def _light_entry(method, params):
    t = B["enq"].get(id(params))
    rec = {"method": method, "mode": h.mode, "wait_ms": (pc() - t) * 1000 if t else None}
    t1 = pc()
    try:
        if method in ("nx_ui_control", "nx_status") or h.mode != "agent" or B["restore"]:
            return orig_exec(method, params)
        return light_execute(method, params)
    finally:
        rec["exec_ms"] = (pc() - t1) * 1000
        B["waits"].append(rec)


if B["variant"] == "light":
    disp._executor = _light_entry
B["orig_exec"] = orig_exec
h.tick = auto_tick
B["active"] = BENCH_ARGS.get("activate", False)
if B["active"]:
    h.requested_mode = "manual"  # next tick: the engineer takes control (by_engineer=True)
print('JSON:{{"installed": true, "variant": "{}"}}'.format(B["variant"]))
