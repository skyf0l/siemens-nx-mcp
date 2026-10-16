"""NX's undo self-test on an untested NX build (``core.kernel._self_test``).

    result = selftest.run(tx)   # {"state": "passed" | "failed" | "skipped", "reason"}

Once per NX session, before the first changing call on a build NX MCP has no
acceptance evidence for, the kernel checks that the undo every call relies on works:
in a scratch part (never saved, under ``<workspace>/.nx-mcp``, never the engineer's)
it sets an undo mark, creates a block, undoes to the mark and checks the block is gone
and the part has the features and bodies it had. ``failed`` (the block could not be
made, the undo raised or left something) makes the build read-only for agents;
``skipped`` (no scratch part could be made or shown) restricts nothing.

The display comes back as it was: the scratch part is shown with ``parts.show`` (undo
marks kept), the engineer's tab is activated again with ``parts.give_back`` and the
scratch part closes with ``CloseRetainUndoMarks``. Cheap: one new part, one block.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

from nx_mcp.kit import builders, parts


class _Skip(Exception):
    """The self-test could not be set up (nothing about undo is known)."""


def _tags(collection) -> frozenset:
    """The NX tags of ``collection``'s objects (features or bodies of the scratch part)."""
    return frozenset(int(o.Tag) for o in collection)


def _scratch_part(tx):
    session_parts = tx.session.Parts
    name = f"nx_mcp_selftest_{uuid.uuid4().hex[:12]}.prt"
    builder = session_parts.FileNew()
    try:
        builder.TemplateFileName = parts.SCRATCH_TEMPLATE
        builder.Units = tx.nx.Part.Units.Millimeters
        builder.NewFileName = str(Path(tx.workspace.root) / ".nx-mcp" / name)
        builder.MakeDisplayedPart = False
        builder.DisplayPartOption = tx.nx.DisplayPartOption.AllowAdditional
        return builder.Commit()
    finally:
        with contextlib.suppress(Exception):
            builder.Destroy()


def _block(tx, part) -> None:
    features = tx.nx.Features
    with builders.build(tx, part.Features.CreateBlockFeatureBuilder, features.Feature.Null) as b:
        b.Type = features.BlockFeatureBuilder.Types.OriginAndEdgeLengths
        b.SetOriginAndLengths(tx.nx.Point3d(0.0, 0.0, 0.0), "10", "10", "10")
        b.BooleanOption.Type = tx.nx.GeometricUtilities.BooleanOperation.BooleanType.Create
        b.commit()


def check_undo(tx, part) -> str | None:
    """Mark, block, undo in ``part`` (the work part): None when the undo removed the block
    and left the features and bodies as they were, else what went wrong."""
    services = tx.services
    features, bodies = _tags(part.Features), _tags(part.Bodies)
    mark = services.set_mark("NX MCP: self-test", visible=False)
    try:
        try:
            _block(tx, part)
        except Exception as error:
            return f"a block could not be created: {getattr(error, 'message', error)}"
        if _tags(part.Bodies) == bodies:
            return "the block made no body"
        try:
            services.undo_to(mark)
        except Exception as error:
            return f"the undo failed: {error}"
    finally:
        with contextlib.suppress(Exception):
            services.delete_mark(mark)
    if _tags(part.Features) != features or _tags(part.Bodies) != bodies:
        return "the undo left the block or changed the part"
    return None


def run(tx) -> dict:
    """``{"state", "reason"}`` of the self-test (module notes); never raises."""
    session_parts = tx.session.Parts
    display, work = session_parts.Display, session_parts.Work
    scratch = None
    try:
        try:
            scratch = _scratch_part(tx)
            parts.show(tx.nx, session_parts, scratch)
            if not parts.same_part(session_parts.Work, scratch):
                raise _Skip("the scratch part did not become the work part")
        except _Skip:
            raise
        except Exception as error:
            raise _Skip(f"no scratch part: {error}") from error
        problem = check_undo(tx, scratch)
        if problem is not None:
            return {"state": "failed", "reason": problem}
        return {"state": "passed", "reason": None}
    except _Skip as skipped:
        return {"state": "skipped", "reason": str(skipped)}
    except Exception as error:
        return {"state": "failed", "reason": f"{type(error).__name__}: {error}"}
    finally:
        if display is not None:
            with contextlib.suppress(Exception):
                parts.give_back(tx, display, work)
        if scratch is not None:
            with contextlib.suppress(Exception):
                scratch.CloseRetainUndoMarks(
                    tx.nx.BasePart.CloseWholeTree.FalseValue,
                    tx.nx.BasePart.CloseModified.CloseModified,
                    None,
                )
