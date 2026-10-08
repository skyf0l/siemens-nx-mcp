"""Typed schemas: Body operations: asymmetric chamfer, variable-radius blend, mirror about a datum or face, split/trim/scale body.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are BodyOpsMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
"""

from __future__ import annotations

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()
SUMMARIES: dict = {}
