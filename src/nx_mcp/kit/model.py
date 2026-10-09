"""The NX model update, inside the call's undo mark.

model.update(tx)              # DoUpdate(tx.mark); errors -> NX_UPDATE_FAILED
model.update(tx, mark)        # a mark the caller holds (inside tx.scratch())
"""

from __future__ import annotations

from nx_mcp.core.errors import fail, native


def update(tx, mark=None):
    """Run ``UpdateManager.DoUpdate`` under ``mark`` (default: the call's mark)."""
    mark = tx.mark if mark is None else mark
    with native(default="NX_UPDATE_FAILED"):
        count = tx.session.UpdateManager.DoUpdate(mark)
    if count:
        fail("NX_UPDATE_FAILED", f"The model update reported {count} error(s)")
