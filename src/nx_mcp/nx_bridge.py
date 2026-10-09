"""Batch bridge lifecycle (``examples/start_nx_bridge.py`` under ``run_journal.exe``).

The journal thread pumps bridge calls (:func:`pump_bridge`); each call runs through
the same ``host.dispatch`` and catalogue kernel as the interactive host.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nx_mcp.bridge import (
    BridgeDescriptor,
    BridgeServer,
    MainThreadDispatcher,
    default_descriptor_path,
)
from nx_mcp.workspace import Workspace


@dataclass
class BridgeRuntime:
    server: BridgeServer
    dispatcher: MainThreadDispatcher
    descriptor: BridgeDescriptor
    descriptor_path: Path

    def stop(self) -> None:
        self.dispatcher.stop()
        self.server.stop()
        try:
            current = BridgeDescriptor.read(self.descriptor_path)
            if current.token == self.descriptor.token:
                self.descriptor_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


_runtime: BridgeRuntime | None = None


def detect_nx_version(session: Any) -> str:
    try:
        value = session.GetEnvironmentVariableValue("UGII_VERSION")
        if value:
            return str(value)
    except Exception:
        pass
    return "NX build unknown"


def start_bridge(
    workspace_root: str | Path,
    *,
    descriptor_path: str | Path | None = None,
    allow_unverified_threading: bool = False,
) -> BridgeDescriptor:
    """Start the batch bridge from inside an NX journal.

    NXOpen threading must be validated on the target build before this gate is
    enabled for a pilot.
    """
    global _runtime
    if _runtime is not None:
        return _runtime.descriptor
    if (
        not allow_unverified_threading
        and os.environ.get("NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE") != "1"
    ):
        raise RuntimeError(
            "Python bridge execution has not been verified on this NX build. "
            "Set NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE=1 only for the real-NX feasibility test."
        )

    import NXOpen

    from nx_mcp.host import dispatch
    from nx_mcp.host.services import NXServices

    session = NXOpen.Session.GetSession()
    services = NXServices(session, NXOpen, detect_nx_version(session), Workspace(workspace_root))
    token = secrets.token_hex(32)
    dispatcher = MainThreadDispatcher(
        lambda method, params: dispatch.execute(services, method, params)
    )
    server = BridgeServer(
        dispatcher.call,
        token=token,
        result_directory=Path(workspace_root) / ".nx-mcp" / "bridge-results",
    )
    server.start()
    descriptor = BridgeDescriptor.create(server.port, services.build, token=token)
    destination = Path(descriptor_path) if descriptor_path else default_descriptor_path()
    descriptor.write(destination)
    _runtime = BridgeRuntime(server, dispatcher, descriptor, destination)
    return descriptor


def pump_bridge(timeout: float = 0.1) -> int:
    """Run pending bridge calls on the NX journal's main thread."""
    if _runtime is None:
        return 0
    return _runtime.dispatcher.drain(timeout=timeout)


def stop_bridge() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.stop()
        _runtime = None
