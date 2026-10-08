"""Feature groups added after 0.2.

Each group has <group>_server.py (typed schemas, READ_ONLY, NON_MODEL,
SUMMARIES) and a mixin on HardenedExecutor. Registering a group here exposes its
tools in the sidecar, routes them on the NX side and adds its panel summaries.
"""

from __future__ import annotations

import importlib

SERVER_MODULES = (
    "modeling_server",
    "sketch_curves_server",
    "sketch_constraints_server",
    "holes_server",
    "feature_tree_server",
    "body_ops_server",
    "drafting_dims_server",
    "drafting_notes_server",
)


def server_modules():
    return [importlib.import_module("nx_mcp." + name) for name in SERVER_MODULES]


def tool_names(module):
    return [
        name for name, value in vars(module).items() if name.startswith("nx_") and callable(value)
    ]
