"""Invariants every feature group must satisfy (see nx_mcp/feature_groups.py)."""

import inspect

import pytest

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.feature_groups import server_modules, tool_names
from nx_mcp.hardened import HardenedExecutor

GROUPS = server_modules()


@pytest.mark.parametrize("module", GROUPS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_group_tools_are_complete(module):
    names = tool_names(module)
    for name in names:
        schema = getattr(module, name)
        assert inspect.getdoc(schema), f"{name} needs a docstring (its MCP description)"
        handler = getattr(HardenedExecutor, "_" + name[3:], None)
        assert handler is not None, f"{name} has no HardenedExecutor handler"
        wanted = list(inspect.signature(schema).parameters)
        accepted = list(inspect.signature(handler).parameters)[1:]
        assert wanted == accepted, f"{name}: schema {wanted} != handler {accepted}"
        assert name in MANIFEST["tools"], f"{name} needs a capability manifest entry"
    assert set(names) >= module.READ_ONLY and set(names) >= module.NON_MODEL
    assert set(module.SUMMARIES) <= set(names)


def test_tool_names_are_unique_across_groups():
    seen = {}
    for module in GROUPS:
        for name in tool_names(module):
            assert name not in seen, f"{name} defined in {seen.get(name)} and {module.__name__}"
            seen[name] = module.__name__
