"""The explosion shown in the model work view, and the note that measured positions are
the assembled ones.

    ex = explode.shown(tx)            # Explosion shown in the work view of the work part, or None
    text = explode.assembled_note(tx) # the note to warn while one is shown, else None

NX 2406 findings encoded here:

- An explosion only moves the display of the views that show it (UF ``Assem``
  ``AskViewExplosion(view)``: 0 or the explosion's tag). Geometry queries on occurrence
  bodies (``AskBoundingBox``, ``AskMinimumDist``, ``Measurement.GetMinimumDistance``,
  face data, mass properties, interference) answer at the assembled (unexploded) pose.
- ``part.ModelingViews.WorkView`` raises while a drawing sheet is shown: no model view
  then, so nothing exploded is on screen in the model.
- A call on a part that is not displayed (gate 4s: a quiet work part, or a read bound
  to it) measures that part, never the exploded assembly on screen: no note.
"""

from __future__ import annotations

import contextlib

from nx_mcp.kit import drawing, parts

NOTE = (
    "Explosion {name} is displayed: positions here are the assembled (unexploded) ones, "
    "not the exploded positions on screen"
)


def shown(tx):
    """The explosion shown in the model work view of the call's part (``tx.part``: the
    work part, or the part a read is bound to) when that part has an NX window tab (the
    displayed assembly, or one in a background tab: each tab keeps its own view); None
    otherwise (no explosion, a drawing sheet shown, a part without a tab, or any NX
    refusal)."""
    with contextlib.suppress(Exception):
        part = next(
            (p for p in parts.tabs(tx.session.Parts) if int(p.Tag) == int(tx.part.Tag)), None
        )
        if part is None:
            return None
        if drawing.current(part) is not None or drawing.work_view_is_drawing(part):
            return None
        tag = int(tx.uf.Assem.AskViewExplosion(part.ModelingViews.WorkView.Tag) or 0)
        if not tag:
            return None
        for explosion in part.ComponentAssembly.Explosions:
            if int(explosion.Tag) == tag:
                return explosion
    return None


def assembled_note(tx) -> str | None:
    """The note that positions are assembled while an explosion is shown, else None."""
    explosion = shown(tx)
    if explosion is None:
        return None
    return NOTE.format(name=str(getattr(explosion, "Name", "") or "?"))
