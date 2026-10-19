"""Read-back of a captured contour PNG: how much of it is the coloured result body.

NX 2406 (live, 100x20x10 mm cantilever, graphics window 1460x1794, white background):
outside the Topology Optimization task a plotted contour draws only its legend (the
colour bar, every band labelled 0, all blue); inside the task the result body is drawn
in the band colours. Pixels whose channel spread (max - min) exceeds ``CHROMA`` are
contour colours (greys, text, edges and the white background stay below). Columns
saturated over ``LEGEND_RUN`` of the height are the legend's colour bar and are left
out. Measured share of the picture: legend only 0.00026 (the view triad), result body
at the task's own zoom 0.0079, fitted 0.157.

``kit.capture.read_png`` decodes the 8-bit RGB/RGBA non-interlaced PNG NX writes with the
standard library only (NX's Python has no imaging package); every second row and column
is counted.
"""

from __future__ import annotations

from nx_mcp.kit.capture import read_png

CHROMA = 80
LEGEND_RUN = 0.3
STEP = 2  # count every second row and column (the share is a ratio)


def contour_share(path) -> float:
    """Share of the PNG covered by contour colours outside the legend's colour bar."""
    width, height, bpp, lines = read_png(path)
    columns = [0] * len(range(0, width, STEP))
    counted = 0
    for line in lines[::STEP]:
        counted += 1
        step = bpp * STEP
        for x, (r, g, b) in enumerate(
            zip(line[0::step], line[1::step], line[2::step], strict=True)
        ):
            if max(r, g, b) - min(r, g, b) > CHROMA:
                columns[x] += 1
    if not counted or not columns:
        return 0.0
    body = sum(c for c in columns if c < LEGEND_RUN * counted)
    return body / (counted * len(columns))
