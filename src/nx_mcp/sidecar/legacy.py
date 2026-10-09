"""Legacy tools as pseudo-specs, so discovery and the gateways cover them too.

During migration (architecture.md section 4) every legacy tool stays callable and
discoverable in both profiles. Its effect comes from today's lists
(``integration_server.READ_ONLY`` and the ``agent_guidance`` groups); its domain
from the legacy agent categories. Deleted in F4 with the legacy tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nx_mcp import agent_guidance, agent_surface, integration_server

# Legacy agent categories -> catalogue domains.
_DOMAINS = {"files": "parts_files", "manufacturing": "sheet_metal"}
_SESSION = {
    "nx_status",
    "nx_workspace_info",
    "nx_operation_status",
    "nx_cancel_operation",
    "nx_ui_control",
    "nx_capabilities",
    "nx_read_result",
    "nx_batch",
    "nx_checkpoint",
    "nx_checkpoint_state",
    "nx_rollback",
    "nx_undo",
    "nx_preview_change",
    "nx_finish_preview",
    *agent_surface.HELPERS,
}


@dataclass(frozen=True)
class LegacyTool:
    name: str
    description: str
    effect: str
    domain: str
    tool: Any  # the FastMCP Tool
    helper: bool = False

    @property
    def input_schema(self) -> dict:
        return self.tool.parameters

    @property
    def output_schema(self) -> dict | None:
        return self.tool.fn_metadata.output_schema


def effect_of(name: str) -> str:
    """read, view, file or model, from the legacy lists (never guessed as read)."""
    if name in agent_surface.HELPERS:
        return agent_surface.HELPERS[name]
    if name in integration_server.READ_ONLY and name != "nx_ui_control":
        return "read"
    if name in agent_guidance.GEOMETRY:
        return "model"
    if name in agent_guidance.VISIBILITY:
        return "view"
    if name in agent_guidance.FILES:
        return "file"
    if name in _SESSION:
        return "session"
    return "model"


def domain_of(name: str) -> str:
    if name in _SESSION:
        return "session"
    legacy = agent_surface.category(name)
    return _DOMAINS.get(legacy, legacy)


def registry(mcp, helpers) -> dict[str, LegacyTool]:
    """Every legacy tool registered on ``mcp``, plus the helper tools."""
    out = {}
    for name, tool in {**mcp._tool_manager._tools, **helpers.tools()}.items():
        out[name] = LegacyTool(
            name=name,
            description=" ".join((tool.description or name).split()),
            effect=effect_of(name),
            domain=domain_of(name),
            tool=tool,
            helper=name in agent_surface.HELPERS,
        )
    return out
