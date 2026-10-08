"""Typed schemas: Feature tree operations: suppress/unsuppress, make current, reorder.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are FeatureTreeMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
"""

from __future__ import annotations

from typing import Literal

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()
SUMMARIES: dict = {
    "nx_suppress_features": lambda p, c: (
        ("Suppress " if p.get("suppress", True) else "Unsuppress ")
        + c.count(p.get("features"), "feature")
    ),
    "nx_make_current_feature": lambda p, c: (
        f"Make {c.obj(p.get('feature'))} the current feature"
        if p.get("feature")
        else "Make the last feature current"
    ),
    "nx_reorder_feature": lambda p, c: (
        f"Move {c.obj(p.get('feature'))} {p.get('position', 'after')} {c.obj(p.get('target'))}"
    ),
}


def nx_suppress_features(features: list[str], suppress: bool):
    """Suppress (suppress=true) or unsuppress (suppress=false) 1-100 feature IDs owned by the work part, then update the model. NX also suppresses every dependent (child) feature of a suppressed feature. Unsuppress restores only the listed features: dependents NX suppressed with them stay suppressed and are listed in still_suppressed_dependents; include them in features to restore the full model. Returns changed: every feature whose suppression state actually changed (requested and implied), each with before/after {suppressed}; unchanged: requested features already in the requested state; timeline: all features in order with suppressed/active flags; bodies: fresh IDs of the bodies now in the part. Bodies that disappear are invalidated and every face/edge ID is invalidated: reacquire topology afterward. Feature IDs stay valid after success; a rejected (rolled-back) call invalidates every ID of the part. A feature that NX leaves in the wrong state rolls the operation back (NX_VERIFICATION_FAILED)."""


def nx_make_current_feature(feature: str | None = None):
    """Make a work-part feature ID current: the model is rolled back to the state right after that feature; every later feature becomes inactive (kept in the timeline, not evaluated) and new features are inserted after the current one. With feature omitted, make the last feature in the timeline current again (all features active). Returns current (the current feature), inactive (features after it, in timeline order), timeline (all features with suppressed/active flags) and bodies (fresh IDs of the bodies now in the part). Face/edge IDs are invalidated and bodies that disappear are invalidated: reacquire topology afterward. Feature IDs stay valid after success; a rejected (rolled-back) call invalidates every ID of the part."""


def nx_reorder_feature(feature: str, target: str, position: Literal["before", "after"]):
    """Move a work-part feature ID immediately before or after another feature ID (target) in the timeline, then update the model. A feature may not move before any of its parents or after any of its children: such moves are rejected with NX_REORDER_DEPENDENCY listing the conflicting features, and any move NX refuses or does not place exactly as requested is rolled back. Returns the moved feature, its new index, timeline_before and timeline (after) with suppressed/active flags, and bodies (fresh IDs). Face/edge IDs are invalidated: reacquire topology afterward."""
