"""Native manufacturing detail and bounded, explicitly sampled analysis contracts."""

from __future__ import annotations

READ_ONLY = {"nx_face_analysis", "nx_wall_thickness"}
NON_MODEL: set[str] = set()


def nx_face_analysis(
    faces: list[str],
    samples_per_axis: int = 3,
    pull_direction: list[float] | None = None,
    minimum_draft: float = 1.0,
):
    """Sample native face normals and principal curvatures on a trimmed UV grid. Optional work-part pull_direction enables signed draft angles: asin(normal dot pull), with positive/negative/below-minimum classifications. minimum_draft is degrees. 1..20 samples per UV axis, at most 10000 total; skips points outside trimmed boundaries. Reports sampled values, not global curvature extrema, mold-release feasibility, or an undercut certification."""


def nx_wall_thickness(
    body: str,
    faces: list[str] | None = None,
    samples_per_axis: int = 3,
    tolerance: float = 0.001,
):
    """Measure a solid's sampled wall thickness using native inward-normal ray intersections. Select an owned work-part solid and optionally its faces. Sample a trimmed UV grid (1..20 per axis, max 10000); start each ray tolerance part-units inside the solid. Returns source/opposite faces and points, sampled min/max, unresolved counts and units. This is first-exit normal thickness, not global minimum or rolling-ball thickness. Thin regions smaller than tolerance require a smaller tolerance."""
