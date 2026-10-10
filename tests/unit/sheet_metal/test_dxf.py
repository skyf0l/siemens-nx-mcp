"""sheet_metal.dxf: entity counts and extents of ASCII DXF and Trumpf GEO files."""

import math

import pytest

from nx_mcp.domains.sheet_metal import dxf


def ascii_dxf(*entities, version="AC1032"):
    head = f"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\n{version}\n  0\nENDSEC\n"
    body = "".join(entities)
    return (head + "  0\nSECTION\n  2\nENTITIES\n" + body + "  0\nENDSEC\n  0\nEOF\n").encode()


def line(a, b, layer="0"):
    return f"  0\nLINE\n  8\n{layer}\n 10\n{a[0]}\n 20\n{a[1]}\n 11\n{b[0]}\n 21\n{b[1]}\n"


def arc(center, radius, start, end, layer="0"):
    return (
        f"  0\nARC\n  8\n{layer}\n 10\n{center[0]}\n 20\n{center[1]}\n 40\n{radius}\n"
        f" 50\n{start}\n 51\n{end}\n"
    )


def test_lines_give_counts_layers_and_extents():
    facts = dxf.dxf_facts(
        ascii_dxf(
            line((0, 0), (100, 0), "OUT"),
            line((100, 0), (100, 97.749115), "OUT"),
            line((0, 77.87), (100, 77.87), "BEND"),
        )  # fmt: skip
    )
    assert facts["version"] == "AC1032" and facts["entities"] == {"LINE": 3}
    assert facts["layers"] == ["BEND", "OUT"]
    assert facts["min"] == [0, 0] and facts["extents"] == [100, 97.749115]


def test_arcs_are_bounded_by_their_swept_extremes_and_crlf_is_read():
    data = ascii_dxf(arc((10, 10), 5, 0, 90), line((0, 0), (12, 0)))
    facts = dxf.dxf_facts(data.replace(b"\n", b"\r\n"))
    assert facts["entities"] == {"ARC": 1, "LINE": 1}
    assert facts["max"] == pytest.approx([15, 15]) and facts["min"] == pytest.approx([0, 0])
    full = dxf.dxf_facts(ascii_dxf("  0\nCIRCLE\n 10\n0\n 20\n0\n 40\n2\n"))
    assert full["extents"] == pytest.approx([4, 4])


def test_arc_points_cover_quadrants_inside_the_sweep_only():
    points = dxf.arc_points((0, 0), 1, 350, 10)
    xs = [p[0] for p in points]
    assert max(xs) == pytest.approx(1) and min(xs) == pytest.approx(math.cos(math.radians(10)))


def test_polylines_and_empty_or_binary_files():
    poly = "  0\nLWPOLYLINE\n 90\n2\n 10\n1\n 20\n2\n 10\n4\n 20\n6\n"
    assert dxf.dxf_facts(ascii_dxf(poly))["extents"] == [3, 4]
    empty = dxf.dxf_facts(ascii_dxf())
    assert empty["entities"] == {} and empty["extents"] is None
    binary = dxf.dxf_facts(dxf.BINARY_DXF + b"\x1a\x00rest")
    assert binary["version"] == "binary" and binary["extents"] is None


GEO = """#~1
1.03
0
9.10.2026
0.0000 -0.0000 0.0
100.0000 97.7491 0.0
9774.9115
1
##~~
#~11


2.0000
##~~
#~31
P
1
0.0000 0.0000 0.0
|~
P
2
100.0000 97.7491 0.0
|~
##~~
#~33
LIN
1 0
1 2
|~
##~~
#~END
"""


def test_geo_points_bound_the_outline_and_the_header_gives_area_and_thickness():
    facts = dxf.geo_facts(GEO.encode())
    assert facts["points"] == 2 and facts["entities"] == {"LIN": 1}
    assert facts["extents"] == [100.0, 97.7491]
    assert facts["area"] == 9774.9115 and facts["thickness"] == 2.0
    bare = dxf.geo_facts(b"#~END\n")
    assert bare["extents"] is None and bare["area"] is None and bare["thickness"] is None
