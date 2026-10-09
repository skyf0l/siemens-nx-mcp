"""Sketch lister for ``nx_list_objects``."""

from __future__ import annotations

from nx_mcp.core.catalogue import lister
from nx_mcp.core.errors import fail


@lister("sketch")
def sketches(tx, owner, owner_kind):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "sketches are listed for the whole part", field="/owner")
    active = tx.session.ActiveSketch
    return [
        (s, "sketch", {"details": {"active": active is not None and s.Tag == active.Tag}})
        for s in tx.part.Sketches
    ]
