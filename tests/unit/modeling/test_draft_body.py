"""nx_create_draft_body on the shared fake, through the real kernel."""

import math

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def sides(body):
    return [f for f in body.faces if f.normal[2] == 0]


def drafts(nx, body, angle, added=1000.0, faces=8):
    """Commit a BODY_TAPER whose ``faces`` planar faces lie at ``angle`` to z."""
    s, c = math.sin(math.radians(angle)), math.cos(math.radians(angle))

    def commit(_b):
        body.volume += added
        f = feature(nx, "BODY_TAPER", [body])
        f.own_faces = [
            nx.remember(nx.face(normal=(c, 0.0, s if i % 2 else -s), body=body))
            for i in range(faces)
        ]
        return f

    nx.on_commit("DraftBodyBuilder", commit)


def test_draft_body_drafts_both_halves_and_reads_the_angles_back():
    nx = modeling_nx()
    body = nx.block(100, 60, 40)
    drafts(nx, body, 5)
    ok = run(
        nx,
        "nx_create_draft_body",
        faces=[f.ref for f in sides(body)],
        parting={"origin": [0, 0, 20], "normal": [0, 0, 1]},
        direction="z",
        angle=5,
        match="none",
        method="true_draft",
    )
    assert ok.ok, ok
    b = nx.last("DraftBodyBuilder")
    types = nx.nx.Features.DraftBodyBuilder
    assert b.Type is types.Types.Faces and b.DraftAngle.RightHandSide == "5.0"
    assert b.MatchType is types.MatchingType.NotSet
    assert b.DraftMethod is types.Method.TrueDraft
    assert b.PartingObject.Value.Normal.Z == 1 and b.PartingObject.Value.Origin.Z == 20
    assert b.FacesToDraft.ReplaceRules.calls and b.Tolerance == pytest.approx(0.01)
    assert b.DrawDirection.Vector.Z == 1.0
    assert len(ok.data["faces"]) == 8
    assert all(row["angle"] == pytest.approx(5) for row in ok.data["faces"])
    assert ok.data["volume"] == pytest.approx(241000) and nx.live_builders == 0


def test_draft_body_takes_a_datum_plane_parting_and_isocline_match():
    nx = modeling_nx()
    body = nx.block(80, 80, 10)
    plane = nx.datum_plane(origin=(0, 0, 5))
    drafts(nx, body, 3)
    ok = run(nx, "nx_create_draft_body", faces=[f.ref for f in sides(body)],
             parting=plane.ref, direction=[0, 0, -1], angle="3 deg")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("DraftBodyBuilder")
    assert b.PartingObject.Value is plane
    assert b.MatchType is nx.nx.Features.DraftBodyBuilder.MatchingType.ToIsocline
    assert b.DraftMethod is nx.nx.Features.DraftBodyBuilder.Method.Isocline


def test_faces_that_miss_the_angle_roll_back():
    nx = modeling_nx()
    body = nx.block(100, 60, 40)
    drafts(nx, body, 4)  # NX drafted at another angle
    bad = run(nx, "nx_create_draft_body", faces=[f.ref for f in sides(body)], parting="xy",
              direction="z", angle=5)  # fmt: skip
    assert bad.error["details"]["check"] == "angle" and bad.mutation_outcome == "rolled_back"


def test_an_unchanged_body_rolls_back():
    nx = modeling_nx()
    body = nx.block(100, 60, 40)
    drafts(nx, body, 5, added=0.0)
    bad = run(nx, "nx_create_draft_body", faces=[f.ref for f in sides(body)], parting="xy",
              direction="z", angle=5)  # fmt: skip
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize("angle", [0, -3, 89, 120])
def test_the_angle_is_checked_first(angle):
    nx = modeling_nx()
    body = nx.block(10, 10, 10)
    bad = run(nx, "nx_create_draft_body", faces=[f.ref for f in sides(body)], parting="xy",
              direction="z", angle=angle)  # fmt: skip
    assert bad.error["field"] == "/angle" and bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_faces_square_to_the_draw_direction_and_of_two_bodies_are_refused():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    top = a.face((0, 0, 1))
    bad = run(nx, "nx_create_draft_body", faces=[top.ref], parting="xy", direction="z", angle=3)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.mutation_outcome == "not_started"
    two = run(nx, "nx_create_draft_body", faces=[sides(a)[0].ref, sides(b)[0].ref],
              parting="xy", direction="z", angle=3)  # fmt: skip
    assert two.error["field"] == "/faces" and two.mutation_outcome == "not_started"
