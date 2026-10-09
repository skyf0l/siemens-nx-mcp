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
    """Create a native datum plane feature. offset (associative): parallel to a planar face or datum plane ID (reference; datum axes/CSYS are rejected with NX_OBJECT_TYPE_MISMATCH) at signed distance along the reference normal (a face's outward normal); negative distance or flip=true puts it on the other side (flip is ignored by the other methods); the result normal equals the reference normal and the distance stays an editable expression. point_normal: fixed plane through origin with that normal. three_points: fixed plane through three non-collinear points, normal = (p1-p0) x (p2-p0). Returns the feature, the datum plane ID (usable by nx_create_sketch_on and as an offset reference) and NX's actual origin and normal, verified against the request (position and normal sense). Refused with NX_SKETCH_ACTIVE while a sketch is active (finish it first)."""


def nx_datum_axis(
    method: Literal["point_direction", "two_points"],
    origin: list[float] | None = None,
    direction: list[float] | None = None,
    start: list[float] | None = None,
    end: list[float] | None = None,
    name: str | None = None,
):
    """Create a fixed (non-associative) native datum axis feature through origin along direction, or from start toward end. Returns the feature, the datum axis ID (usable as nx_circular_pattern axis) and NX's actual origin and unit direction, verified against the request (same line and sense). The returned origin is a point on the axis, not necessarily start: NX extends the displayed axis beyond the points. Refused with NX_SKETCH_ACTIVE while a sketch is active (finish it first)."""


def nx_datum_csys(
    origin: list[float],
    x_axis: list[float],
    y_axis: list[float],
    name: str | None = None,
):
    """Create a native datum coordinate system feature (CSYS with its three datum planes and axes) at origin [x,y,z] with unit, mutually orthogonal x_axis and y_axis (tolerance 1e-8); z = x cross y. Returns the feature, the coordinate system ID and NX's actual origin and axes, verified against the request. Refused with NX_SKETCH_ACTIVE while a sketch is active (finish it first)."""


def nx_datum_point(position: list[float], name: str | None = None):
    """Create a native point feature at position [x, y, z] in the work part. Returns the feature and NX's actual coordinates. Refused with NX_SKETCH_ACTIVE while a sketch is active (finish it first)."""


def nx_create_sketch_on(
    target: str,
    name: str | None = None,
    x_direction: list[float] | None = None,
):
    """Create and activate a native sketch on a planar face or datum plane ID owned by the work part (associative: it follows the face/plane when the model updates). The sketch normal is the face's outward normal or the datum plane normal. Without x_direction NX chooses the horizontal axis and the origin is the part origin projected onto the plane; with x_direction (a vector lying in the plane) the x axis is exactly that direction and the origin is the reference point of the face/plane (returned). Returns the sketch ID and its actual frame (origin, x_axis, y_axis, normal); add curves in that local frame ([x,y] maps to origin + x*x_axis + y*y_axis), then nx_finish_sketch. Face IDs go stale after any model change; re-acquire them (nx_find_geometry). Finish another active sketch first."""


def nx_circular_pattern(
    features: list[str],
    count: int,
    angle: float,
    axis: str | None = None,
    axis_origin: list[float] | None = None,
    axis_direction: list[float] | None = None,
):
    """Create a native circular feature pattern (Count and Pitch) of owned feature IDs, e.g. an extrude boss or cut: count total instances including the seed (2-360), angle = pitch in degrees between neighbours. Rotate about a datum axis ID (axis, associative) or a fixed axis through axis_origin along axis_direction; give one or the other. (count-1)*angle must be < 360 so no instance lands on the seed. Returns the pattern feature, count, pitch and the axis used. Refused with NX_SKETCH_ACTIVE while a sketch is active (finish it first)."""
