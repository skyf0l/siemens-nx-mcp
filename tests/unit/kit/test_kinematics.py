"""kit.kinematics: poses, moves, driven constraints, gaps, carried points and angles."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import geom, kinematics
from nx_mcp.kit import pose as kpose
from tests.kit import fake_nx, matrix, point

SOLVED, BROKEN = 0, 1


def rz(deg):
    a = math.radians(deg)
    return [[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]]


def flat(r):
    return [v for row in r for v in row]


class Occurrence:
    def __init__(self, t, r):
        self.t, self.r = list(t), [list(row) for row in r]

    def GetPosition(self):
        columns = ([self.r[0][j], self.r[1][j], self.r[2][j]] for j in range(3))
        return point(*self.t), matrix(*columns)


def test_angle_about_is_signed_and_exact_about_the_axis():
    before = ([0.0, 0.0, 0.0], rz(10))
    after = ([5.0, 0.0, 0.0], rz(55))
    assert kinematics.angle_about(before, after, [0, 0, 1]) == pytest.approx(45)
    assert kinematics.angle_about(before, after, [0, 0, -1]) == pytest.approx(-45)
    assert kinematics.angle_about(after, after, [0, 0, 1]) == pytest.approx(0)


def test_unwrap_keeps_angle_histories_continuous():
    assert kinematics.unwrap(None, 170) == 170
    assert kinematics.unwrap(170, -170) == pytest.approx(190)
    assert kinematics.unwrap(-170, 170) == pytest.approx(-190)
    assert kinematics.unwrap(350, 5) == pytest.approx(365)


def test_carry_moves_a_point_with_its_component():
    before = ([10.0, 0.0, 0.0], rz(0))
    after = kpose.rotate_about(before, [0, 0, 0], [0, 0, 1], 90)
    assert kinematics.carry([20, 0, 5], before, after) == pytest.approx([0, 20, 5])


def test_place_reaches_an_absolute_pose_through_the_origin_pivot():
    nx = fake_nx()
    c = Occurrence([10, 0, 0], rz(0))

    def move(component, shift, m):  # NX 2406: rotate about the assembly origin, then shift
        delta = geom.rows(m)
        component.t = geom.add(geom.matvec(delta, component.t), geom.xyz(shift))
        component.r = geom.matmul(delta, component.r)

    nx.part.ComponentAssembly = NS(MoveComponent=move)
    target = kpose.rotate_about(kinematics.pose(c), [10, 5, 0], [0, 0, 1], 30)
    kinematics.place(nx.tx, c, target)
    t, r = kinematics.pose(c)
    assert t == pytest.approx(target[0]) and flat(r) == pytest.approx(flat(target[1]))


def _driven(nx, statuses, error=None):
    calls = []

    class Net:
        MoveObjectsState = False

        def AddConstraint(self, c):
            calls.append(("add", c))

        def Solve(self):
            if error is not None:
                raise error
            calls.append("solve")

        def ApplyToModel(self):
            calls.append("apply")

    others = [NS(Suppressed=False, status=s, GetConstraintStatus=None) for s in statuses]
    for o in others:
        o.GetConstraintStatus = lambda o=o: o.status
    suppressed = NS(Suppressed=True, GetConstraintStatus=lambda: BROKEN)
    positioner = NS(
        BeginAssemblyConstraints=lambda: calls.append("begin"),
        EndAssemblyConstraints=lambda: calls.append("end"),
        EstablishNetwork=Net,
        ClearNetwork=lambda: calls.append("clear"),
        Constraints=[*others, suppressed],
    )
    nx.part.ComponentAssembly = NS(Positioner=positioner)
    nx.nx.Positioning = NS(Constraint=NS(SolverStatus=NS(Solved=SOLVED)))
    nx.session.UpdateManager = NS(DoUpdate=lambda mark: 0)
    nx.tx.mark = "mark"
    driven = NS(value=None)
    driven.SetExpression = lambda text: setattr(driven, "value", text)
    return driven, others, calls


def test_drive_sets_the_value_solves_and_lists_unsolved_constraints():
    nx = fake_nx()
    driven, others, calls = _driven(nx, [SOLVED, BROKEN])
    assert kinematics.drive(nx.tx, driven, 42.5) == [others[1]]  # suppressed ones skipped
    assert driven.value == "42.5"
    assert calls[0] == "begin" and calls[-2:] == ["clear", "end"]
    driven, _others, _calls = _driven(nx, [SOLVED])
    assert kinematics.drive(nx.tx, driven, 10) == []


def test_drive_reports_an_nx_refusal_as_the_driven_constraint():
    nx = fake_nx()
    driven, _others, calls = _driven(nx, [SOLVED], error=RuntimeError("cannot solve"))
    assert kinematics.drive(nx.tx, driven, 10) == [driven]
    assert calls[-2:] == ["clear", "end"]  # the network is always closed


def test_gap_is_the_minimum_over_body_pairs():
    nx = fake_nx()
    table = {("a1", "b1"): 7.0, ("a1", "b2"): 3.0, ("a2", "b1"): -1e-9, ("a2", "b2"): 9.0}
    nx.session.Measurement = NS(
        GetMinimumDistance=lambda a, b: (table[(a, b)], point(1, 2, 3), point(4, 5, 6), 0.0)
    )
    assert kinematics.gap(nx.tx, ["a1"], ["b1", "b2"]) == (3.0, [1, 2, 3], [4, 5, 6])
    assert kinematics.gap(nx.tx, ["a1", "a2"], ["b1", "b2"])[0] == 0.0  # clamped
    with pytest.raises(NXError) as raised:
        kinematics.gap(nx.tx, [], ["b1"])
    assert raised.value.code == "NX_WRONG_GEOMETRY"
