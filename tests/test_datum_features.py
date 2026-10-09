"""Legacy DatumFeaturesMixin helpers: local contract tests.

The datum, circular-pattern and feature tools moved to the modeling domain
(``tests/unit/modeling``) and sketches on a face or datum plane to the sketch
domain (``nx_create_sketch``, ``tests/unit/sketch``).
"""

import pytest

from nx_mcp.runtime import NXToolError


def test_datum_axis_or_csys_is_not_a_plane_reference():
    # Live NX 2406 (verify_a): a datum axis passed as sketch target reached
    # AskFaceData and failed with raw NX_API_ERROR 820022.
    from types import SimpleNamespace

    from nx_mcp.datum_features import DatumFeaturesMixin

    point = SimpleNamespace(X=0.0, Y=0.0, Z=0.0)
    axis = SimpleNamespace(Origin=point, Direction=point, Tag=1)
    csys = SimpleNamespace(Origin=point, Orientation=None, Tag=2)
    for obj in (axis, csys):
        with pytest.raises(NXToolError) as error:
            DatumFeaturesMixin()._df_plane_of(obj)
        assert error.value.code == "NX_OBJECT_TYPE_MISMATCH"
