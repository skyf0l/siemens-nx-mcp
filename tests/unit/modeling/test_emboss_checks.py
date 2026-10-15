"""Emboss and offset emboss refuse, before NX, sections and paths NX would reject with
raw messages: an open section, a section beside the faces, a path off the sheet."""

from tests.kit import run
from tests.unit.modeling.test_emboss import emboss_nx, sheet_nx
from tests.unit.modeling.test_features import _lines

ABOVE = [((10, 10, 30), (30, 10, 30)), ((30, 10, 30), (30, 30, 30)),
         ((30, 30, 30), (10, 30, 30)), ((10, 30, 30), (10, 10, 30))]  # fmt: skip


def test_an_open_section_is_refused_before_nx():
    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    sk = nx.sketch(origin=(0, 0, 30))
    sk.geometry = _lines(nx, ABOVE[:3])
    bad = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[body.face((0, 0, 1)).ref], height=5)
    assert bad.error["field"] == "/sketch" and "no closed loop" in bad.error["message"], bad
    assert bad.mutation_outcome == "not_started" and not nx.builders


def test_a_sketch_point_on_a_corner_does_not_open_the_section():
    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    sk = nx.sketch(origin=(0, 0, 30))
    corner = (10, 10, 30)  # a point NX evaluates, like a path sketch's origin on a corner
    sk.geometry = _lines(nx, [*ABOVE, (corner, corner)])
    got = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[body.face((0, 0, 1)).ref], height=5)
    assert got.ok or "no closed loop" not in got.error["message"], got


def test_a_section_beside_the_faces_is_refused_before_nx():
    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    sk = nx.sketch(origin=(0, 0, 30))
    sk.geometry = _lines(nx, [((a[0] + 200, a[1], a[2]), (b[0] + 200, b[1], b[2]))
                              for a, b in ABOVE])  # fmt: skip
    top = body.face((0, 0, 1))
    top.box = [0, 0, 10, 80, 80, 10]
    nx.remember(top)
    bad = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[top.ref], height=5)
    assert bad.error["field"] == "/sketch" and "does not lie over" in bad.error["message"], bad
    assert not nx.builders


def test_a_path_off_the_sheet_is_refused_before_nx():
    nx, sheet, _path = sheet_nx()
    (line,) = _lines(nx, [((10, 20, 15), (50, 20, 15))])
    line.OwningPart, line.IsOccurrence = nx.part, False
    ref = nx.ref_of(line, "curve")
    nx.uf.Modeling.AskMinimumDist = lambda a, b, c, p, d, q: (15.0, list(q), list(q))
    bad = run(nx, "nx_create_offset_emboss", sheet=sheet.ref, path=[ref], height=2,
              left_width=2)  # fmt: skip
    assert bad.error["field"] == "/path/0" and "off the sheet" in bad.error["message"], bad
    assert "nx_create_projected_curve" in bad.error["hint"] and not nx.builders


def test_a_closed_section_over_the_faces_is_built():
    from tests.unit.modeling.test_emboss import embosses

    nx = emboss_nx()
    body = nx.block(80, 80, 10)
    sk = nx.sketch(origin=(0, 0, 30))
    sk.geometry = _lines(nx, ABOVE)
    top = body.face((0, 0, 1))
    top.box = [0, 0, 10, 80, 80, 10]
    nx.remember(top)
    embosses(nx, body, 2000.0)
    ok = run(nx, "nx_create_emboss", sketch=sk.ref, faces=[top.ref], height=5)
    assert ok.ok, ok
