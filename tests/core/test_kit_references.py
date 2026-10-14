"""The shared fake's references behave like ``core.references`` (tests/kit/kernel.py)."""

from tests.kit import kernel as kit
from tests.kit.fake import FakeNX


def test_kit_rows_carry_anchors_and_find_anchor_matches_them():
    nx = FakeNX()
    body = nx.block(10, 10, 10)
    body.JournalIdentifier = "BLOCK(1)"
    services = kit.services(nx)
    row = services.references.row(body, "body")
    assert row["anchor"] == "@work.prt:BLOCK(1)"
    assert services.references.find_anchor(row["anchor"], "body")["id"] == row["id"]
    assert services.references.resolve(row["anchor"], ("body",)) is body
    face = body.faces[0]
    face.JournalIdentifier = "FACE 1"
    assert "anchor" not in services.references.row(face, "face")
    assert services.references.row(face, "face", anchor=True)["anchor"] == "@work.prt:FACE 1"


def test_kit_topology_change_keeps_faces_of_bodies_and_retires_the_rest():
    nx = FakeNX()
    body = nx.block(10, 10, 10)
    services = kit.services(nx)
    kept = nx.ref_of(body.faces[0], "face")
    loose = nx.ref_of(nx.face(), "face")  # a face no body has
    retired = services.invalidate({}, {}, True)
    assert retired == {loose} and kept in nx.refs


def test_kit_display_records_keep_a_body_in_one_record():
    """Like the host: a body's record carries its faces' common transparency and only
    the faces that differ; faces get records of their own only when given."""
    nx = FakeNX()
    body = nx.block(10, 10, 10)
    for obj in (body, *body.faces):
        obj.IsBlanked, obj.Color, obj.transparency = False, 1, 0
    body.faces[0].Color = 5
    [(obj, record)] = kit.services(nx).display_records([body], True)
    assert obj is body and record["transparency"] == 0 and record["color_index"] == 1
    assert [f["color_index"] for f in record["faces"]] == [5]
    face = body.faces[1]
    [(_, alone)] = kit.services(nx).display_records([face], True)
    assert alone == {"blanked": False, "color_index": 1, "transparency": 0}
