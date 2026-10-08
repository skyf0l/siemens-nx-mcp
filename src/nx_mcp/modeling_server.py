"""Typed schemas for datum features, sketches on faces/datums and circular patterns.

Handlers live in datum_features.DatumFeaturesMixin (NX side). Lengths use
work-part units; angles are degrees; vectors are [x, y, z] in the work part.
"""

from __future__ import annotations

from typing import Literal

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()
SUMMARIES: dict = {}


def nx_datum_plane(
    method: Literal["offset", "point_normal", "three_points"],
    reference: str | None = None,
    distance: float | None = None,
    origin: list[float] | None = None,
    normal: list[float] | None = None,
    points: list[list[float]] | None = None,
    flip: bool = False,
    name: str | None = None,
):
    """Create an associative native datum plane feature. offset: parallel to a planar face or datum plane ID (reference) at distance along its normal (flip reverses the side); the distance stays an editable expression. point_normal: through origin with normal. three_points: through three non-collinear points. Returns the feature, the datum plane ID (usable by nx_create_sketch_on and as a pattern/mirror reference) and NX's actual origin and normal, verified against the request."""


def nx_datum_axis(
    method: Literal["point_direction", "two_points"],
    origin: list[float] | None = None,
    direction: list[float] | None = None,
    start: list[float] | None = None,
    end: list[float] | None = None,
    name: str | None = None,
):
    """Create a native datum axis feature through origin along direction, or from start to end. Returns the feature, the datum axis ID (usable as nx_circular_pattern axis) and NX's actual origin and unit direction, verified against the request."""


def nx_datum_csys(
    origin: list[float],
    x_axis: list[float],
    y_axis: list[float],
    name: str | None = None,
):
    """Create a native datum coordinate system feature (CSYS with its three datum planes and axes) at origin with orthonormal x_axis and y_axis; z follows the right-hand rule. Returns the feature and the actual frame, verified against the request."""


def nx_datum_point(position: list[float], name: str | None = None):
    """Create a native point feature at position [x, y, z] in the work part. Returns the feature and NX's actual coordinates."""


def nx_create_sketch_on(
    target: str,
    name: str | None = None,
    x_direction: list[float] | None = None,
):
    """Create and activate a native sketch on a planar face or datum plane ID (associative: it follows the face/plane when the model updates). x_direction optionally sets the sketch's horizontal direction; it must lie in the plane. Returns the sketch ID and its actual origin, x/y axes and normal; add curves in that local frame, then finish the sketch. Finish another active sketch first."""


def nx_circular_pattern(
    features: list[str],
    count: int,
    angle: float,
    axis: str | None = None,
    axis_origin: list[float] | None = None,
    axis_direction: list[float] | None = None,
):
    """Create a native circular feature pattern of owned features: count total instances including the seed (2-360), angle = pitch in degrees between instances. Rotate about a datum axis ID (axis) or a fixed axis through axis_origin along axis_direction; give one or the other. count*angle may not exceed 360. Returns the pattern feature and instance count."""
