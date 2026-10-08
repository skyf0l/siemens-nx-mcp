"""DEVELOPMENT ONLY: a resident NX journal that reloads the MCP host from disk.

Played once in graphical NX (examples/start_nx_interactive_dev.py), it hosts the
bridge like the 2406 launcher and additionally:

- gives the agent control as soon as NX allows it, without a click;
- on ``<workspace>/.nx-mcp/dev-restart`` stops the host, drops every ``nx_mcp``
  module from memory and starts a fresh host from the code on disk;
- keeps the engineer's choice: after "Take control back" a restart keeps the
  engineer in control;
- survives code that fails to import: the traceback goes to
  ``resident-error.txt`` and the loop waits for the next restart request;
- records each generation in ``dev-state.json`` for tooling to poll.

It ends on the panel's Stop bridge, the ``ui-stop`` file or ``dev-exit``.
Open parts stay open in NX across restarts; object IDs do not survive them.
This module keeps running across restarts, so changes to it need a new Play.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
import time
import traceback
from pathlib import Path


def purge_modules(prefix: str = "nx_mcp") -> None:
    """Forget every loaded nx_mcp module so the next import reads the disk."""
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]
    importlib.invalidate_caches()


def _default_pump():
    from nx_mcp.winpump import pump_messages

    return pump_messages


def run_dev(
    workspace,
    descriptor_path,
    *,
    interval: float = 0.02,
    pump=None,
    sleep=time.sleep,
    importer=importlib.import_module,
    purge=purge_modules,
):
    state_dir = Path(workspace) / ".nx-mcp"
    state_dir.mkdir(parents=True, exist_ok=True)
    restart_file = state_dir / "dev-restart"
    exit_file = state_dir / "dev-exit"
    error_file = state_dir / "resident-error.txt"
    state_file = state_dir / "dev-state.json"
    # Captured once: keeps NX responsive even while the code on disk is broken.
    pump = pump or _default_pump()
    retired = []  # Keep old hosts' native callback delegates alive until NX exits.
    agent_control = True
    generation = 0
    exit_file.unlink(missing_ok=True)

    def write_state(**fields):
        payload = {"generation": generation, "updated_at": time.time(), **fields}
        try:
            temp = state_file.with_suffix(".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            temp.replace(state_file)
        except OSError:
            pass

    while True:
        generation += 1
        restart_file.unlink(missing_ok=True)
        host = None
        try:
            purge()
            interactive = importer("nx_mcp.interactive")
            missing = interactive.import_nxopen_namespaces()
            interactive.start(
                workspace, descriptor_path, resident=True, agent_control=agent_control, dev=True
            )
            host = interactive._host
            retired.append(host)
            if missing:
                host.last_error = "NXOpen namespaces not importable: " + "; ".join(missing)
            error_file.unlink(missing_ok=True)
            write_state(running=True, ok=True, agent_control=agent_control, error=None)
        except Exception as exc:
            with contextlib.suppress(OSError):
                error_file.write_text(traceback.format_exc(), encoding="utf-8")
            write_state(running=True, ok=False, agent_control=agent_control, error=str(exc))

        while True:
            if exit_file.exists():
                exit_file.unlink(missing_ok=True)
                if host is not None and not host.stopped:
                    host.stop()
                write_state(running=False, ok=True, error=None)
                return host
            if restart_file.exists():
                break
            if host is not None and host.stopped:
                write_state(running=False, ok=True, error=None)
                return host  # The engineer chose Stop bridge.
            pump()
            if host is not None:
                host.tick()
            sleep(interval)

        if host is not None:
            # Keep the engineer's last choice; a host that never got control
            # (an NX dialog was open) still wants it.
            agent_control = host.mode == "agent" or bool(getattr(host, "auto_start", False))
            if not host.stopped:
                host.stop()
