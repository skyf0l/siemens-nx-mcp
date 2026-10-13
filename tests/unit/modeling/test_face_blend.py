"""Face blend on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling import face_blend
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def _blends(nx, body, volume, radius=3.0):
    """Commit returns a feature owning ``body`` whose own face is a cylinder of ``radius``."""

    def hook(_b):
        body.volume = volume
        f = feature(nx, "FACE_BLEND", [body])
        face = nx.face(normal=(0, 1, 0), origin=(10, 0, 10), kind="cylinder", body=body)
        face.radius = radius
        nx.remember(face)
        f.own_faces = [face]
        return f

    nx.on_commit("FaceBlendBuilder", hook)
    nx.part.MeasureManager.NewFaceProperties = lambda *_a: NS(Area=94.25, Dispose=lambda: None)


def test_face_blend_configures_walls_and_reads_the_radius():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    top, side = block.face((0, 0, 1)), block.face((-1, 0, 0))
    _blends(nx, block, 8038.6)
    ok = run(nx, "nx_create_face_blend", faces1=[top.ref], faces2=[side.ref], radius=3,
             flip2=True)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("FaceBlendBuilder")
    assert b.FirstFaceCollector.GetRules()[0][1] == [top]
    assert b.SecondFaceCollector.GetRules()[0][1] == [side]
    assert b.ReverseFirstFaceNormal is False and b.ReverseSecondFaceNormal is True
    assert b.TrimInputFacesToBlendFaces is True and b.SewAllFaces is True
    assert b.CircularCrossSection.Radius.RightHandSide == "3.0"
    assert ok.data["radius"] == pytest.approx(3.0) and len(ok.data["blend_faces"]) == 1


def test_a_wrong_radius_rolls_back():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    _blends(nx, block, 8038.6, radius=2.0)
    bad = run(nx, "nx_create_face_blend", faces1=[block.face((0, 0, 1)).ref],
              faces2=[block.face((-1, 0, 0)).ref], radius=3)  # fmt: skip
    assert bad.error["details"]["check"] == "radius" and bad.mutation_outcome == "rolled_back"


def test_face_blend_arguments_are_checked_first():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    top = block.face((0, 0, 1))
    zero = run(nx, "nx_create_face_blend", faces1=[top.ref], faces2=[top.ref], radius=0)
    assert zero.error["field"] == "/radius" and zero.mutation_outcome == "not_started"
    both = run(nx, "nx_create_face_blend", faces1=[top.ref], faces2=[top.ref], radius=2)
    assert both.error["field"] == "/faces2/0" and both.mutation_outcome == "not_started"


def test_principal_curvatures_of_a_cylinder_shape_operator():
    k = 1 / 4.0  # a cylinder of radius 4 along z, normal along x
    shape = [[k, 0, 0], [0, 0, 0], [0, 0, 0]]
    assert sorted(face_blend._principal(shape)) == pytest.approx([0.0, 0.25])
