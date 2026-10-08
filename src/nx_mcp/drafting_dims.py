"""NX-side handlers: Drawing dimensions beyond linear (radial, diameter, angular, ordinate) and drawing standard (projection angle, templates).

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.
"""

from __future__ import annotations

from nx_mcp.runtime import NXToolError  # noqa: F401


class DraftingDimsMixin:
    pass
