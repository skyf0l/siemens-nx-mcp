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


def test_kit_display_records_report_transparency_of_faces_only():
    nx = FakeNX()
    body = nx.block(10, 10, 10)
    for obj in (body, *body.faces):
        obj.IsBlanked, obj.Color, obj.transparency = False, 1, 0
    shown = kit.services(nx).display_records([body], True)
    records = {id(obj): record for obj, record in shown}
    assert "transparency" not in records[id(body)]
    assert records[id(body.faces[0])]["transparency"] == 0
