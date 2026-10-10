"""The feature timeline as the Part Navigator shows it, and each feature's state.

NX 2406 lessons:

- ``part.Features`` is not in timeline order and holds internal features (a sketch's
  own datum CSYS, ``SKETCH(1:1B)``); ``Timestamp`` orders them, ``IsInternal`` marks the
  internal ones. Timestamps are not unique: the stable sort keeps NX's order for ties.
- ``Feature.Suppressed`` is True both for user-suppressed features and for features
  after the current feature (rolled back by ``nx_set_current_feature``); UF
  ``AskSuppressFeature`` tells them apart (mask 1 = suppressed by the user,
  16 = inactive).
"""

from __future__ import annotations

USER_SUPPRESSED = 1  # AskSuppressFeature mask: suppressed by the user
INACTIVE = 16  # AskSuppressFeature mask: after the current feature


def internal(feature) -> bool:
    """Whether NX hides the feature from the Part Navigator (``IsInternal``)."""
    return bool(getattr(feature, "IsInternal", False))


def timeline(part) -> list:
    """User-visible features of ``part`` in timeline order (internal features omitted)."""
    features = list(part.Features)
    if features and all(hasattr(f, "Timestamp") for f in features):
        features.sort(key=lambda f: int(f.Timestamp))
    return [f for f in features if not internal(f)]


def states(tx, part) -> dict:
    """``{tag: (feature, suppressed, active)}`` in timeline order: ``suppressed`` only by
    the user, ``active`` False for features after the current feature."""
    order = timeline(part)
    current = getattr(part, "CurrentFeature", None)
    tags = [int(f.Tag) for f in order]
    cut = tags.index(int(current.Tag)) if current is not None and int(current.Tag) in tags else None
    ask = getattr(getattr(tx.uf, "Modeling", None), "AskSuppressFeature", None)
    out = {}
    for i, f in enumerate(order):
        active = cut is None or i <= cut
        if ask is not None:
            code = int(ask(f.Tag))
            suppressed = bool(code & USER_SUPPRESSED)
            active = active and not code & INACTIVE
        else:
            suppressed = bool(getattr(f, "Suppressed", False)) and active
        out[int(f.Tag)] = (f, suppressed, active)
    return out
