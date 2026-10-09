"""NX 2406 experiment 2: like experiment 1, but GetUnloadOption returns the enum
(as in Siemens' Python samples) and there is no background thread.

Experiment 1 hung NX right after the journal returned (no timer tick ran); it
returned int(AtTermination), which NX probably ignored, then unloaded the
journal under a live timer and thread.

Play once in a FRESH NX with no part open (Alt+F8). It returns immediately.
Everything is logged to <workspace>/.nx-mcp/exp2.log; nothing else is changed
except one new scratch part, smoke/experiments/exp2_<time>.prt.

Questions answered by the log:
1. With GetUnloadOption = AtTermination, does the journal's code stay alive
   after the journal returns (timer ticks keep coming)?
2. In which Python interpreter does the Win32 timer callback run: the
   journal's (a marker module is visible) or NX's main one?
3. Does a background thread (like the bridge listener) survive the return?
4. Can the callback create the FIRST part of a fresh session (the call that
   froze NX under the resident launcher) and return promptly?
The experiment stops itself after about 40 seconds.
"""

import ctypes
import os
import sys
import time
from pathlib import Path

import NXOpen

WORKSPACE = Path(os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE"))
LOG = WORKSPACE / ".nx-mcp" / "exp2.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
START = time.time()


def log(message):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{time.time() - START:7.2f}s  {message}\n")


def GetUnloadOption(dummy):  # noqa: N802 - NX looks this name up after the journal returns.
    return NXOpen.Session.LibraryUnloadOption.AtTermination


# A module only the journal's interpreter knows about.
sys.modules["nx_exp2_marker"] = type(sys)("nx_exp2_marker")
LOG.write_text("", encoding="utf-8")
log(f"journal start: python {sys.version.split()[0]}, nx {NXOpen.Session.GetSession().GetEnvironmentVariableValue('NX_VERSION') if hasattr(NXOpen.Session.GetSession(), 'GetEnvironmentVariableValue') else '?'}")
log(f"journal: parts loaded = {sum(1 for _ in NXOpen.Session.GetSession().Parts)}, application = {NXOpen.Session.GetSession().ApplicationName}")

state = {"ticks": 0, "created": False, "stop": False}
user32 = ctypes.WinDLL("user32", use_last_error=True)
TIMERPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_uint32)
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, TIMERPROC]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def tick(*_):
    try:
        state["ticks"] += 1
        n = state["ticks"]
        if n in (1, 2, 5) or n % 20 == 0:
            import sys as s  # Resolved by whichever interpreter runs this callback.

            session = NXOpen.Session.GetSession()
            log(
                f"tick {n}: marker visible={'nx_exp2_marker' in s.modules} "
                f"parts={sum(1 for _ in session.Parts)} app={session.ApplicationName}"
            )
        if n == 10 and not state["created"]:
            state["created"] = True
            session = NXOpen.Session.GetSession()
            path = WORKSPACE / "smoke" / "experiments" / f"exp2_{time.strftime('%H%M%S')}.prt"
            path.parent.mkdir(parents=True, exist_ok=True)
            log(f"tick 10: creating first part {path.name} (app={session.ApplicationName})")
            t0 = time.time()
            builder = session.Parts.FileNew()
            try:
                # Same settings as nx_create_part (nx_bridge._create_part).
                builder.TemplateFileName = "model-plain-1-mm-template.prt"
                builder.Units = NXOpen.Part.Units.Millimeters
                builder.NewFileName = str(path)
                builder.DisplayPartOption = NXOpen.DisplayPartOption.AllowAdditional
                builder.Commit()
            finally:
                builder.Destroy()
            log(f"tick 10: part created in {time.time() - t0:.2f}s, app now {session.ApplicationName}")
        if n >= 80 or state["stop"]:
            user32.KillTimer(None, state["timer"])
            log(f"tick {n}: timer stopped; experiment finished")
    except BaseException as error:  # Log everything; never let an exception reach NX.
        import traceback

        log(f"tick {state['ticks']}: ERROR {type(error).__name__}: {error}\n{traceback.format_exc()}")


callback = TIMERPROC(tick)
state["callback"] = callback  # Keep the delegate alive.
state["timer"] = user32.SetTimer(None, 0, 500, callback)
log(f"journal: timer id {state['timer']}")


log("journal: returning now")
