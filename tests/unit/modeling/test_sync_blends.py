"""nx_edit_blend_order (synchronous Reorder Blends) on the shared fake through the real kernel."""

from types import SimpleNamespace as NS

from tests.kit import run
from tests.kit.objects import NXException
from tests.unit.modeling.fakes import feature, modeling_nx

BUILDER = "ReorderBlendsBuilder"


def corner(partner="convex"):
    """A stepped block with a concave fillet and a convex blend running into it. The
    builder's ``GetBlendface2Candidate`` answers ``partner``: "convex" (the convex blend
    face), "none" (refuses: no overflow) or "several" (None)."""
    nx = modeling_nx()
    a = nx.block(60, 40, 20)
    concave = nx.remember(nx.face(normal=(0, -1, 1), origin=(30, 18.5, 11.5), kind="cylinder",
                                  body=a))  # fmt: skip
    convex = nx.remember(nx.face(normal=(1, 0, 1), origin=(58.5, 19, 11), kind="cylinder",
                                 body=a))  # fmt: skip
    a.faces += [concave, convex]
    make = nx.builder
    asked = []

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == BUILDER:

            def candidate(face):
                asked.append(face)
                if partner == "none":
                    raise NXException(3190182, "Blend 1 does not overflow a blend of opposite "
                                      "convexity.")  # fmt: skip
                return None if partner == "several" else convex

            object.__setattr__(b, "GetBlendface2Candidate", candidate)
        return b

    nx.builder = builder
    return nx, a, concave, convex, asked


def reorders(nx, body, volume):
    def hook(b):
        body.volume = volume
        return feature(nx, "Reorder Blends", [body])

    nx.on_commit(BUILDER, hook)


def test_finds_the_overflowing_blend_and_reads_the_corner_change_back():
    nx, a, concave, convex, asked = corner()
    before = a.volume
    reorders(nx, a, before - 11.7)
    ok = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert ok.ok, ok
    assert abs(ok.data["volume_change"] + 11.7) < 1e-9 and ok.data["face_count"] == len(a.faces)
    b = nx.last(BUILDER)
    assert asked == [concave] and b.destroyed and b.args == (None,)
    assert b.BlendFace1.ReplaceRules.calls[0][0][0] == [("FaceDumb", [concave])]
    assert b.BlendFace2.ReplaceRules.calls[0][0][0] == [("FaceDumb", [convex])]
    reorders(nx, a, before)  # the other way round: given, so not looked up
    back = run(nx, "nx_edit_blend_order", face=nx.ref_of(convex, "face"),
               other=nx.ref_of(concave, "face"))  # fmt: skip
    assert back.ok and abs(back.data["volume_change"] - 11.7) < 1e-9, back
    assert asked == [concave]
    assert nx.last(BUILDER).BlendFace2.ReplaceRules.calls[0][0][0] == [("FaceDumb", [concave])]


def test_a_reorder_on_a_body_too_large_to_examine_says_the_check_was_skipped(monkeypatch):
    from nx_mcp.kit import bodies

    nx, a, concave, _convex, _asked = corner()
    reorders(nx, a, a.volume - 11.7)
    monkeypatch.setattr(bodies, "CLASH_FACES", 1)
    ok = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert ok.ok, ok
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert notes and notes[0].startswith("Self-overlap check skipped"), ok.warnings


def test_a_reorder_that_changes_nothing_rolls_back():
    nx, a, concave, _convex, _asked = corner()
    reorders(nx, a, a.volume)
    same = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert same.error["details"]["check"] == "volume" and same.mutation_outcome == "rolled_back"
    nx.faults[a.Tag] = 3
    reorders(nx, a, a.volume - 5.0)
    broken = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert broken.error["details"]["check"] == "consistency", broken
    assert broken.mutation_outcome == "rolled_back"


def test_refusals_before_any_change():
    nx, _a, concave, convex, _asked = corner("none")
    none = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert none.error["code"] == "NX_WRONG_GEOMETRY" and none.error["field"] == "/face", none
    assert none.mutation_outcome == "not_started" and nx.last(BUILDER).destroyed
    nx, _a, concave, convex, _asked = corner("several")
    several = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert several.error["field"] == "/other" and several.mutation_outcome == "not_started"
    itself = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"),
                 other=nx.ref_of(concave, "face"))  # fmt: skip
    assert itself.error["field"] == "/other" and itself.mutation_outcome == "not_started"
    b = nx.block(10, 10, 10)
    elsewhere = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"),
                    other=nx.ref_of(b.face((0, 0, 1)), "face"))  # fmt: skip
    assert elsewhere.error["field"] == "/other", elsewhere


def test_nx_refusing_the_pair_is_wrong_geometry():
    nx, _a, concave, convex, _asked = corner()
    nx.refuse(BUILDER, 3190185, "The blends cannot be reordered.")
    no = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"),
             other=nx.ref_of(convex, "face"))  # fmt: skip
    assert no.error["code"] == "NX_WRONG_GEOMETRY" and no.error["nx_code"] == 3190185, no


def test_licences_are_asked_before_any_builder():
    nx, _a, concave, _convex, _asked = corner()
    asked = []

    def presence(name):
        asked.append(name)
        return name != "nx_synchronous"

    nx.session.LicenseManager = NS(CheckPresence=presence)
    no = run(nx, "nx_edit_blend_order", face=nx.ref_of(concave, "face"))
    assert no.error["code"] == "NX_LICENSE_UNAVAILABLE" and no.mutation_outcome == "not_started"
    assert asked == ["nx_synchronous"] and not nx.builders
