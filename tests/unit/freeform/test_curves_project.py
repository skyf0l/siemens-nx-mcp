"""nx_create_projected_curve and nx_create_combined_projection on the shared fake."""

import pytest

from tests.kit import run
from tests.unit.freeform.curves_fakes import curves_nx, made
from tests.unit.freeform.fakes import curve

P = "ProjectCurveBuilder"
C = "CombinedProjectionBuilder"


def test_project_onto_faces_along_their_normal_adds_a_face_collector():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    top = body.face((0, 0, 1))
    line = curve(nx, [10, 10, 50], [90, 40, 50])
    nx.on_commit(P, lambda b: made(nx, "CPROJ", [[[10, 10, 20], [90, 40, 20]]]))
    ok = run(nx, "nx_create_projected_curve", curves=[line.ref], faces=[top.ref])
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = nx.last(P)
    added = b.FaceToProjectTo.Add.calls[0][0][0]
    assert added.GetRules() == [("FaceDumb", [top])]  # a collector, never bare faces
    assert (
        b.ProjectionDirectionMethod
        is nx.nx.Features.ProjectCurveBuilder.DirectionType.AlongFaceNormal
    )
    assert b.SectionToProject.AddToSection.calls[0][0][0] == [("CurveDumb", [line])]
    assert ok.data["length"] == pytest.approx(85.440037) and ok.data["gap"] == 0
    assert ok.data["ray_gap"] is None and nx.live_builders == 0


def test_project_along_a_vector_onto_a_plane_checks_the_rays():
    nx = curves_nx()
    line = curve(nx, [10, 10, 50], [90, 40, 50])
    nx.on_commit(P, lambda b: made(nx, "CPROJ", [[[60, 10, 0], [140, 40, 0]]]))
    ok = run(nx, "nx_create_projected_curve", curves=[line.ref], plane="xy", along="vector",
             direction=[1, 0, -1], both_sides=True)  # fmt: skip
    assert ok.ok and ok.data["ray_gap"] == pytest.approx(0, abs=1e-6), ok
    b = nx.last(P)
    F = nx.nx.Features.ProjectCurveBuilder
    assert b.ProjectionDirectionMethod is F.DirectionType.AlongVector
    assert b.ProjectionOption is F.ProjectionOptionType.ProjectBothSides
    assert b.PlaneToProjectTo.normal == [0, 0, 1] and not b.FaceToProjectTo.Add.calls
    # The same curve claimed along -z is not a projection of the source.
    nx.on_commit(P, lambda b: made(nx, "CPROJ", [[[60, 10, 0], [140, 40, 0]]]))
    bad = run(nx, "nx_create_projected_curve", curves=[line.ref], plane="xy", along="vector",
              direction="-z")  # fmt: skip
    assert bad.error["details"]["check"] == "direction" and bad.mutation_outcome == "rolled_back"


def test_project_argument_checks():
    nx = curves_nx()
    line = curve(nx, [10, 10, 50], [90, 40, 50])
    none = run(nx, "nx_create_projected_curve", curves=[line.ref])
    assert none.error["category"] == "invalid_argument" and none.mutation_outcome == "not_started"
    vec = run(nx, "nx_create_projected_curve", curves=[line.ref], plane="xy", along="vector")
    assert vec.error["category"] == "invalid_argument"
    twice = run(nx, "nx_create_projected_curve", curves=[line.ref, line.ref], plane="xy")
    assert twice.error["category"] == "invalid_argument" and not nx.builders


def test_projected_curve_off_the_target_rolls_back():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    line = curve(nx, [10, 10, 50], [90, 40, 50])
    nx.on_commit(P, lambda b: made(nx, "CPROJ", [[[10, 10, 25], [90, 40, 25]]]))
    bad = run(nx, "nx_create_projected_curve", curves=[line.ref], faces=[body.ref])
    assert bad.error["details"]["check"] == "on_target" and nx.live_builders == 0


# ---------------------------------------------------------------------------
# Combined projection


def test_combined_projection_with_vectors_reads_back_both_views():
    nx = curves_nx()
    top = curve(nx, [0, 0, 0], [100, 50, 0])
    side = curve(nx, [0, -10, 0], [100, -10, 30])
    nx.on_commit(C, lambda b: made(nx, "CPROJ_CMB", [[[0, 0, 0], [100, 50, 30]]]))
    ok = run(nx, "nx_create_combined_projection", first=[top.ref], second=[side.ref],
             first_direction="z", second_direction="y")  # fmt: skip
    assert ok.ok and ok.data["length"] == pytest.approx(115.758369), ok
    b = nx.last(C)
    T = nx.nx.GeometricUtilities.ProjectionOptions.DirectionType
    assert b.Direction1.ProjectDirectionMethod is T.Vector
    v = b.Direction2.ProjectVector.Vector
    assert [v.X, v.Y, v.Z] == [0, 1, 0]
    assert ok.data["first_direction"] == [0, 0, 1] and ok.data["gap"] == pytest.approx(0, abs=1e-6)
    nx.on_commit(C, lambda b: made(nx, "CPROJ_CMB", [[[0, 0, 0], [100, 50, 40]]]))
    bad = run(nx, "nx_create_combined_projection", first=[top.ref], second=[side.ref],
              first_direction="z", second_direction="y")  # fmt: skip
    assert bad.error["details"]["check"] == "views" and bad.mutation_outcome == "rolled_back"


def test_combined_projection_defaults_to_the_curve_plane_normals():
    nx = curves_nx()
    plan = curve(nx, [0, 0, 0], [50, 20, 0], [100, 0, 0])
    prof = curve(nx, [0, -10, 0], [50, -10, 10], [100, -10, 0])
    nx.on_commit(C, lambda b: made(nx, "CPROJ_CMB", [[[0, 0, 0], [50, 20, 10], [100, 0, 0]]]))
    ok = run(nx, "nx_create_combined_projection", first=[plan.ref], second=[prof.ref])
    assert ok.ok, ok
    T = nx.nx.GeometricUtilities.ProjectionOptions.DirectionType
    assert nx.last(C).Direction1.ProjectDirectionMethod is T.CrvPlaneNormal
    assert [abs(v) for v in ok.data["first_direction"]] == [0, 0, 1]
    assert [abs(v) for v in ok.data["second_direction"]] == [0, 1, 0]


def test_combined_projection_needs_a_direction_for_a_line_or_a_3d_curve():
    nx = curves_nx()
    line = curve(nx, [0, 0, 0], [100, 50, 0])
    bent = curve(nx, [0, 0, 0], [50, 20, 5], [100, 0, 0], [120, 30, 30])
    prof = curve(nx, [0, -10, 0], [50, -10, 10], [100, -10, 0])
    straight = run(nx, "nx_create_combined_projection", first=[line.ref], second=[prof.ref])
    assert straight.error["field"] == "/first_direction"
    assert straight.mutation_outcome == "not_started"
    spatial = run(nx, "nx_create_combined_projection", first=[prof.ref], second=[bent.ref])
    assert spatial.error["field"] == "/second_direction"
    same = run(nx, "nx_create_combined_projection", first=[prof.ref], second=[prof.ref])
    assert same.error["field"] == "/second" and not nx.builders
