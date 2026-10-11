"""Batch bridge lifecycle (``nx_app/journals/start_batch.py`` under ``run_journal.exe``).

The journal thread pumps bridge calls (:func:`pump_bridge`); each call runs through
the same ``host.dispatch`` and catalogue kernel as the interactive host.
"""

from __future__ import annotations

import contextlib
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
    run_timeout,
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
    """NX's own build text, e.g. ``"2406.9160"`` (``Session.FullReleaseNumber`` or
    ``ReleaseNumber``), else ``UGII_VERSION`` (``"v2406"``).

    The release number is used only when its build key (``evidence.build_key``) is
    the one ``UGII_VERSION`` gives, so the key that gates evidence never changes.
    """
    from nx_mcp.core.evidence import build_key

    variable = None
    with contextlib.suppress(Exception):
        variable = session.GetEnvironmentVariableValue("UGII_VERSION") or None
    for name in ("FullReleaseNumber", "ReleaseNumber"):
        try:
            release = str(getattr(session, name) or "").strip()
        except Exception:
            continue
        key = build_key(release)
        if key and (variable is None or key == build_key(variable)):
            return release
    return str(variable) if variable else "NX build unknown"


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
    build = detect_nx_version(session)
    services = NXServices(session, NXOpen, build, Workspace(workspace_root))
    token = secrets.token_hex(32)
    dispatcher = MainThreadDispatcher(
        lambda method, params: dispatch.execute(services, method, params),
        run_timeout=run_timeout,
    )
    server = BridgeServer(
        dispatcher.call,
        token=token,
        result_directory=Path(workspace_root) / ".nx-mcp" / "bridge-results",
    )
    server.start()
    descriptor = BridgeDescriptor.create(server.port, build, token=token)
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
