"""Typed schemas for datum features, sketches on faces/datums and circular patterns.

Handlers live in datum_features.DatumFeaturesMixin (NX side). Lengths use
work-part units; angles are degrees; vectors are [x, y, z] in the work part.
"""

from __future__ import annotations

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()
SUMMARIES: dict = {}
