"""nx_edit_face_relation (Make Coplanar/Coaxial/Parallel/Perpendicular/Tangent/Symmetric) and
the NX Synchronous licence guard of nx_edit_faces, on the shared fake through the real
kernel."""

import math
from types import SimpleNamespace as NS

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx

C10, S10 = math.cos(math.radians(10)), math.sin(math.radians(10))


def setup():
    """Block A 40x20x10 at the origin and block B 20x20x15 at x=100."""
    nx = modeling_nx()
    a = nx.block(40, 20, 10)
    b = nx.block(20, 20, 15, origin=(100.0, 0.0, 0.0))
    return nx, a, b


def extra(nx, body, kind, origin, direction, radius=0.0):
    """A face of ``kind`` added to ``body`` (a hole, a boss, a sphere)."""
    face = nx.remember(nx.face(normal=direction, origin=origin, kind=kind, body=body))
    face.radius = radius
    body.faces.append(face)
    return face


def moves(nx, kind, body, face, origin, direction, volume=None):
    """Commits of ``kind`` move ``face`` to ``origin``/``direction`` (and set the volume)."""

    def hook(b):
        nx.face_data[face.Tag] = (nx.face_data[face.Tag][0], origin, direction)
        if volume is not None:
            body.volume = volume
        return feature(nx, kind, [body])

    nx.on_commit(kind, hook)


def relate(nx, face, relation, to, **args):
    return run(nx, "nx_edit_face_relation", face=nx.ref_of(face, "face"), relation=relation,
               to=nx.ref_of(to, "face"), **args)  # fmt: skip


def test_coplanar_fills_the_constraint_and_rigid_faces_and_reads_the_gap_back():
    nx, a, b = setup()
    top, btop = a.face((0, 0, 1)), b.face((0, 0, 1))
    moves(nx, "CoplanarBuilder", a, top, (20, 10, 15), (0, 0, 1), 12000.0)
    ok = relate(nx, top, "coplanar", btop)
    assert ok.ok and ok.data["gap"] == 0.0 and ok.data["angle"] == 0.0, ok
    assert ok.data["volume_change"] == 4000.0
    builder = nx.last("CoplanarBuilder")
    assert builder.ConstraintFace.Value is top and builder.StationaryFace.Value is btop
    rules = builder.RigidFace.FaceCollector.ReplaceRules.calls[0][0][0]
    assert rules == [("FaceDumb", [top])] and builder.destroyed
    nx.on_commit("CoplanarBuilder", lambda b: feature(nx, "CoplanarBuilder", [a]))  # no move
    nx.face_data[top.Tag] = ("plane", (20, 10, 10), (0, 0, 1))
    left = relate(nx, top, "coplanar", btop)
    assert left.error["details"]["check"] == "gap" and left.mutation_outcome == "rolled_back"
    side = a.face((1, 0, 0))
    cyl = extra(nx, b, "cylinder", (110, 10, 0), (0, 0, 1), 3.0)
    wrong = relate(nx, side, "coplanar", cyl)
    assert wrong.error["code"] == "NX_WRONG_GEOMETRY" and wrong.error["field"] == "/to"
    assert wrong.mutation_outcome == "not_started"


def test_coaxial_moves_the_hole_and_its_group():
    nx, a, b = setup()
    hole = extra(nx, a, "cylinder", (30, 12, 0), (0, 0, -1), 3.0)
    boss = extra(nx, b, "cylinder", (30, 10, 20), (0, 0, 1), 5.0)
    top = a.face((0, 0, 1))
    moves(nx, "CoaxialBuilder", a, hole, (30, 10, 5), (0, 0, -1))
    ok = relate(nx, hole, "coaxial", boss, with_faces=[nx.ref_of(top, "face")])
    assert ok.ok and ok.data["gap"] == 0.0 and ok.data["angle"] == 0.0, ok
    builder = nx.last("CoaxialBuilder")
    assert builder.MotionFace.Value is hole and builder.StationaryFace.Value is boss
    rules = builder.MoveAlongFace.FaceCollector.ReplaceRules.calls[0][0][0]
    assert rules == [("FaceDumb", [hole, top])]
    planar = relate(nx, top, "coaxial", boss)
    assert planar.error["code"] == "NX_WRONG_GEOMETRY" and planar.error["field"] == "/face"
    other = relate(nx, hole, "coaxial", boss, with_faces=[nx.ref_of(b.face((0, 0, 1)), "face")])
    assert other.error["code"] == "NX_WRONG_OWNER" and other.error["field"] == "/with_faces/0"
    assert other.mutation_outcome == "not_started"


def test_parallel_turns_about_the_face_centre_and_perpendicular_through_a_point():
    nx, a, _ = setup()
    d = nx.block(30, 20, 10, origin=(0.0, 50.0, 0.0))
    nx.face_data[d.face((1, 0, 0)).Tag] = ("plane", (27.8, 65.0, 5.0), (C10, S10, 0.0))
    nx.face_data[d.face((0, 1, 0)).Tag] = ("plane", (11.3, 72.3, 5.0), (-S10, C10, 0.0))
    side = a.face((1, 0, 0))
    side.box = [40.0, 0.0, 0.0, 40.0, 20.0, 10.0]
    moves(nx, "ParallelBuilder", a, side, (40, 10, 5), (C10, S10, 0.0))
    ok = relate(nx, side, "parallel", d.face((1, 0, 0)))
    assert ok.ok and abs(ok.data["angle"]) < 1e-6, ok
    through = nx.last("ParallelBuilder").ThroughPoint.Coordinates
    assert (through.X, through.Y, through.Z) == (40.0, 10.0, 5.0)
    moves(nx, "PerpendicularBuilder", a, side, (40, 20, 0), (C10, S10, 0.0))
    ok = relate(nx, side, "perpendicular", d.face((0, 1, 0)), through=[40, 20, 0])
    assert ok.ok and abs(ok.data["angle"] - 90.0) < 1e-6, ok
    through = nx.last("PerpendicularBuilder").ThroughPoint.Coordinates
    assert (through.X, through.Y, through.Z) == (40.0, 20.0, 0.0)
    moves(nx, "PerpendicularBuilder", a, side, (40, 10, 5), (C10, S10, 0.0))  # misses the point
    off = relate(nx, side, "perpendicular", d.face((0, 1, 0)), through=[40, 20, 3])
    assert off.error["details"]["check"] == "through" and off.mutation_outcome == "rolled_back"


def test_symmetric_mirrors_to_about_a_new_plane():
    nx, a, _ = setup()
    side, back = a.face((1, 0, 0)), a.face((-1, 0, 0))
    moves(nx, "SymmetricBuilder", a, side, (50, 10, 5), (1, 0, 0), 10000.0)
    plane = {"origin": [25, 0, 0], "normal": [1, 0, 0]}
    ok = relate(nx, side, "symmetric", back, plane=plane)
    assert ok.ok and ok.data["gap"] == 0.0 and ok.data["angle"] == 0.0, ok
    builder = nx.last("SymmetricBuilder")
    assert builder.SymmetryPlaneOption is nx.nx.Features.SymmetricBuilder.PlaneOptions.New
    assert builder.NewSymmetryPlane is not None
    missing = relate(nx, side, "symmetric", back)
    assert missing.error["field"] == "/plane" and missing.mutation_outcome == "not_started"
    cyl = extra(nx, a, "cylinder", (30, 12, 0), (0, 0, 1), 3.0)
    mixed = relate(nx, side, "symmetric", cyl, plane="yz")
    assert mixed.error["code"] == "NX_WRONG_GEOMETRY" and mixed.error["field"] == "/to"


def test_tangent_plane_to_sphere_and_argument_refusals():
    nx, a, b = setup()
    ball = extra(nx, b, "sphere", (10, 10, 30), (0, 0, 0), 5.0)
    top = a.face((0, 0, 1))
    moves(nx, "TangentBuilder", a, top, (20, 10, 25), (0, 0, 1), 20000.0)
    ok = relate(nx, top, "tangent", ball)
    assert ok.ok and ok.data["gap"] == 0.0 and ok.data["angle"] is None, ok
    planes = relate(nx, top, "tangent", b.face((0, 0, 1)))
    assert planes.error["code"] == "NX_WRONG_GEOMETRY" and "coplanar" in planes.error["message"]
    through = relate(nx, top, "tangent", ball, through=[0, 0, 0])
    assert through.error["field"] == "/through" and through.mutation_outcome == "not_started"
    plane = relate(nx, top, "coplanar", b.face((0, 0, 1)), plane="xy")
    assert plane.error["field"] == "/plane"
    same = relate(nx, top, "coplanar", top)
    assert same.error["field"] == "/to"


def test_the_synchronous_licence_is_asked_before_any_builder():
    nx, a, b = setup()
    asked = []

    def presence(name):
        asked.append(name)
        return False

    nx.session.LicenseManager = NS(CheckPresence=presence)
    no = relate(nx, a.face((0, 0, 1)), "coplanar", b.face((0, 0, 1)))
    assert no.error["code"] == "NX_LICENSE_UNAVAILABLE" and no.mutation_outcome == "not_started"
    assert asked == ["nx_synchronous"] and not nx.builders
    top = nx.ref_of(a.face((0, 0, 1)), "face")
    move = run(nx, "nx_edit_faces", faces=[top], action="move", distance=2, direction="z")
    assert move.error["code"] == "NX_LICENSE_UNAVAILABLE", move
    assert move.error["details"]["argument_license"] is True
    nx.on_commit("OffsetFaceBuilder", lambda b_: (setattr(a, "volume", 8800.0),
                                                   feature(nx, "OFFSET", [a]))[1])  # fmt: skip
    offset = run(nx, "nx_edit_faces", faces=[top], action="offset", distance=1)
    assert offset.ok, offset  # Offset Face needs Modeling only
