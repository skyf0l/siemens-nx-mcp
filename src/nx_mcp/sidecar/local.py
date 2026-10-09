"""Catalogue tools that run in the sidecar (``runs="sidecar"``): workspace files.

They never reach NX, so the engineer's control hold never delays them. The same
kernel runs them (gates 1-4, 6-7, 10, 14, 15) over :class:`SidecarServices`, a
``Services`` without NX: no work part, no undo, receipts in the shared operation
store. Gate 2 runs here as well as in the proxy, on the sidecar's workspace.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from types import SimpleNamespace

from nx_mcp.core import kernel
from nx_mcp.core.tx import Services


class SidecarServices(Services):
    runs = "sidecar"

    def __init__(self, workspace, *, build=None, dev=None) -> None:
        from nx_mcp.core.receipts import OperationStore

        self.workspace = workspace
        self.store = OperationStore(workspace.root)
        self.build = build
        self.dev = os.environ.get("NX_MCP_DEV") == "1" if dev is None else bool(dev)
        self.session_id = "sidecar-" + uuid.uuid4().hex

    # No NX in this process: no parts, sketches or undo.
    def work_part(self):
        return None

    def parts(self) -> list:
        return []

    def part_key(self, part) -> str:
        return ""

    def state(self) -> dict:
        return {}

    def owns(self, part) -> bool:
        return True

    def modified_flags(self) -> dict:
        return {}

    @contextlib.contextmanager
    def observe(self):
        yield SimpleNamespace(touched=False)

    def after_commit(self, spec) -> None:
        pass

    def after_failure(self, spec) -> None:
        pass


def run(spec, args: dict, services: SidecarServices, *, operation_id=None) -> dict:
    """The envelope of one sidecar tool call (never raises for a tool failure)."""
    return kernel.run(spec, args, kernel.Context(services, operation_id=operation_id))
