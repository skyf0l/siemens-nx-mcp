"""Call helpers: catalogue tools through the real kernel (NX side or sidecar)."""

from __future__ import annotations

from nx_mcp.core import kernel
from nx_mcp.sidecar import local
from nx_mcp.workspace import Workspace
from tests.kit.kernel import Result, services


def call(nx, fn, *, operation_id=None, **args) -> Result:
    """Run a catalogue tool's spec on ``nx`` through ``core.kernel.run``."""
    context = kernel.Context(services(nx), operation_id=operation_id)
    return Result(kernel.run(fn.spec, args, context))


def side(root, fn, **args) -> Result:
    """Run a ``runs="sidecar"`` tool through ``sidecar/local.py`` (development mode)."""
    svc = local.SidecarServices(Workspace(root), build="v2406", dev=True)
    return Result(local.run(fn.spec, args, svc))
