# Runs inside NX via nx_dev_python, on the UI thread, inside the host's execute():
# NX holds no LockAccess and the host disabled the main window for this call (Auto).
# Every path restores exactly that state before returning.
import json
import time

import nx_mcp.interactive as I

h = I._host
ui = h.ui
S = h.nx.UI.Status
user = h.user
hwnd = h.main_hwnd
N = BENCH_ARGS.get("n", 200)
pc = time.perf_counter
out = {}


def ms(t):
    return (pc() - t) * 1000.0


def record(name, xs):
    out.setdefault(name, []).extend(xs)


def unlock_fully():
    for _ in range(8):
        if ui.AskLockStatus() != S.Lock:
            return
        ui.UnlockAccess()


entry = {"lock": str(ui.AskLockStatus()), "enabled": bool(user.IsWindowEnabled(hwnd))}
try:
    # Lock nesting probe: is LockAccess counted?
    ui.LockAccess()
    ui.LockAccess()
    ui.UnlockAccess()
    nested_after_one_unlock = str(ui.AskLockStatus())
    unlock_fully()
    out_nesting = {
        "lock,lock,unlock ->": nested_after_one_unlock,
        "after full unlock": str(ui.AskLockStatus()),
    }

    a, b = [], []
    for _ in range(N):
        t = pc()
        ui.LockAccess()
        a.append(ms(t))
        t = pc()
        ui.UnlockAccess()
        b.append(ms(t))
    record("UI.LockAccess", a)
    record("UI.UnlockAccess", b)

    a, b = [], []
    for _ in range(N):
        t = pc()
        ui.AskLockStatus()
        a.append(ms(t))
        t = pc()
        ui.CanOpenPart()
        b.append(ms(t))
    record("UI.AskLockStatus", a)
    record("UI.CanOpenPart", b)

    # Window is disabled on entry (taken for the call): enable then disable again.
    a, b = [], []
    for _ in range(N):
        t = pc()
        user.EnableWindow(hwnd, True)
        a.append(ms(t))
        t = pc()
        user.EnableWindow(hwnd, False)
        b.append(ms(t))
    record("EnableWindow(main, True)", a)
    record("EnableWindow(main, False)", b)

    a = []
    for _ in range(N):
        t = pc()
        user.IsWindowEnabled(hwnd)
        a.append(ms(t))
    record("IsWindowEnabled(main)", a)

    # Full take + release with LockAccess (a lock-based handover), for comparison.
    take, release, cycle = [], [], []
    user.EnableWindow(hwnd, True)  # engineer state: enabled, unlocked
    for _ in range(N):
        t0 = pc()
        busy = ui.AskLockStatus() == S.Lock or not ui.CanOpenPart()
        assert not busy
        ui.LockAccess()
        user.EnableWindow(hwnd, False)
        t1 = pc()
        user.EnableWindow(hwnd, True)
        ui.AskLockStatus()
        ui.UnlockAccess()
        ui.AskLockStatus()
        ui.CanOpenPart()
        t2 = pc()
        take.append((t1 - t0) * 1000)
        release.append((t2 - t1) * 1000)
        cycle.append((t2 - t0) * 1000)
    record("take: check+LockAccess+EnableWindow(False)", take)
    record("release: EnableWindow(True)+UnlockAccess+checks", release)
    record("full take+release cycle (primitives)", cycle)
finally:
    unlock_fully()
    user.EnableWindow(hwnd, False)  # back to the call's entry state
exit_state = {"lock": str(ui.AskLockStatus()), "enabled": bool(user.IsWindowEnabled(hwnd))}
print(
    "JSON:"
    + json.dumps(
        {
            "entry": entry,
            "exit": exit_state,
            "nesting": out_nesting,
            "ms": {k: summarize(v) for k, v in out.items()},
        }
    )
)
