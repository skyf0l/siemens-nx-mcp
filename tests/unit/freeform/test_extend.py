"""nx_create_sheet_extension and nx_create_law_extension on the shared fake."""

import math

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import edge, face, flat, made, sheet
from tests.unit.freeform.fakes import freeform_nx as base_nx


def freeform_nx(units="mm"):
    """The freeform fake with NX's trim feature collection (its builders are the part's)."""
    nx = base_nx(units)
    nx.part.Features.TrimFeatureCollection = nx.part.Features
    return nx


def plate(nx, x1=20.0, z=0.0):
    """A 20 x 10 sheet at height z with its edge at x = x1 (returns body, face, edge)."""
    f = face(nx, flat(), area=x1 * 10.0)
    e = edge(nx, [x1, 0, z], [x1, 10, z], faces=[f])
    body = sheet(nx, (0, 0, z, x1, 10, z), [f], [e])
    f.box = list(body.box)
    return body, f, e


def grow(nx, kind, body, f, x1, new=False):
    """The commit grows ``body`` to x1 (or adds a separate sheet from 20 to x1)."""

    def commit(_b):
        if new:
            g = face(nx, flat(), area=(x1 - 20.0) * 10.0)
            return made(nx, "EXTEND_SHEET", [sheet(nx, (20, 0, 0, x1, 10, 0), [g])])
        f.area = x1 * 10.0
        body.box = [0, 0, 0, x1, 10, 0]
        f.box = list(body.box)
        return made(nx, "EXTEND_SHEET", [body])

    nx.on_commit(kind, commit)


def test_extend_by_distance_reports_the_area_change():
    nx = freeform_nx()
    body, f, e = plate(nx)
    grow(nx, "ExtendSheetBuilder", body, f, 25.0)
    ok = run(nx, "nx_create_sheet_extension", edges=[e.ref], distance=5)
    assert ok.ok, ok
    b = nx.last("ExtendSheetBuilder")
    E = nx.nx.Features.ExtendSheetBuilder
    assert b.TargetBoundaryEdges.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [e])]
    assert b.Offset.RightHandSide == "5.0" and b.LimitMethod is E.Limit.Offset
    assert b.BodyOutputOption is E.BodyOutput.ExtendOriginalSheet
    assert b.SurfaceShapeOption is E.SurfaceShape.NaturalCurvature
    assert b.DistanceTolerance == pytest.approx(0.001)
    assert nx.license_queries == ["nx_freeform_1"]
    data = ok.data
    assert data["area_before"] == pytest.approx(200) and data["area"] == pytest.approx(250)
    assert data["added_area"] == pytest.approx(50) and data["method"] == "distance"
    assert data["bounds"]["max"] == [25, 10, 0] and nx.live_builders == 0


def test_extend_trims_back_with_a_negative_distance_and_checks_the_sign():
    nx = freeform_nx()
    body, f, e = plate(nx)
    grow(nx, "ExtendSheetBuilder", body, f, 17.0)
    ok = run(nx, "nx_create_sheet_extension", edges=[e.ref], distance=-3)
    assert ok.ok and ok.data["added_area"] == pytest.approx(-30), ok

    nx = freeform_nx()
    body, f, e = plate(nx)
    grow(nx, "ExtendSheetBuilder", body, f, 25.0)  # NX grew it instead
    bad = run(nx, "nx_create_sheet_extension", edges=[e.ref], distance=-3)
    assert bad.error["details"]["check"] == "area", bad
    assert bad.mutation_outcome == "rolled_back"


def test_extend_as_a_new_sheet_counts_the_new_sheet():
    nx = freeform_nx()
    body, f, e = plate(nx)
    grow(nx, "ExtendSheetBuilder", body, f, 25.0, new=True)
    ok = run(nx, "nx_create_sheet_extension", edges=[e.ref], distance=5, output="new_sheet",
             shape="natural_tangent")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("ExtendSheetBuilder")
    E = nx.nx.Features.ExtendSheetBuilder
    assert b.BodyOutputOption is E.BodyOutput.ExtendasNewSheet
    assert b.SurfaceShapeOption is E.SurfaceShape.NaturalTangent
    assert ok.data["area"] == pytest.approx(50) and ok.data["added_area"] == pytest.approx(50)


def test_extend_until_a_face_measures_the_reach():
    nx = freeform_nx()
    body, f, e = plate(nx)
    limit = face(nx, flat(), area=600.0, normal=(-1, 0, 0))
    limit.box = [30, -10, -10, 30, 20, 10]
    grow(nx, "ExtendSheetBuilder", body, f, 30.0)
    ok = run(nx, "nx_create_sheet_extension", edges=[e.ref], method="until",
             until=[limit.ref])  # fmt: skip
    assert ok.ok, ok
    b = nx.last("ExtendSheetBuilder")
    assert b.LimitMethod is nx.nx.Features.ExtendSheetBuilder.Limit.UntilSelected
    assert b.LimitTools.ReplaceRules.calls[0][0][0] == [("FaceDumb", [limit])]
    assert ok.data["gaps"] == [pytest.approx(0)] and ok.data["area"] == pytest.approx(300)

    nx = freeform_nx()
    body, f, e = plate(nx)
    limit = face(nx, flat(), area=600.0, normal=(-1, 0, 0))
    limit.box = [30, -10, -10, 30, 20, 10]
    grow(nx, "ExtendSheetBuilder", body, f, 25.0)  # stopped short of the wall
    bad = run(nx, "nx_create_sheet_extension", edges=[e.ref], method="until",
              until=[limit.ref])  # fmt: skip
    assert bad.error["details"]["check"] == "reach", bad


def test_extend_until_a_datum_plane_uses_a_datum_rule():
    nx = freeform_nx()
    body, f, e = plate(nx)
    plane = nx.datum_plane(origin=(30, 0, 0), normal=(1, 0, 0))
    plane.box = [30, -100, -100, 30, 100, 100]
    nx.remember(plane)
    grow(nx, "ExtendSheetBuilder", body, f, 30.0)
    ok = run(nx, "nx_create_sheet_extension", edges=[e.ref], method="until",
             until=[nx.ref_of(plane, "datum_plane")])  # fmt: skip
    assert ok.ok, ok
    rules = nx.last("ExtendSheetBuilder").LimitTools.ReplaceRules.calls[0][0][0]
    assert rules == [("FaceDatum", [plane])]


def test_corner_joins_two_sheets():
    nx = freeform_nx()
    body, f, e = plate(nx)
    g = face(nx, flat(), area=100.0)
    other_edge = edge(nx, [30, 0, 5], [30, 10, 5], faces=[g])
    other = sheet(nx, (30, 0, 5, 30, 10, 15), [g], [other_edge])

    def commit(_b):
        f.area, g.area = 300.0, 150.0
        body.faces = [f, g]
        body.box = [0, 0, 0, 30, 10, 15]
        nx.part.Bodies.remove(other)
        return made(nx, "TRIM_AND_EXTEND", [body])

    nx.on_commit("TrimAndExtendBuilder", commit)
    ok = run(nx, "nx_create_sheet_extension", edges=[e.ref], method="corner",
             to_edges=[other_edge.ref])  # fmt: skip
    assert ok.ok, ok
    b = nx.last("TrimAndExtendBuilder")
    assert b.Operation is nx.nx.Features.TrimAndExtendBuilder.OperationType.MakeCorner
    assert b.ToolSelection.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [other_edge])]
    assert b.CombineOption is True
    assert ok.data["area_before"] == pytest.approx(300)
    assert ok.data["area"] == pytest.approx(450) and ok.data["added_area"] == pytest.approx(150)
    assert len(ok.data["bodies"]) == 1


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"edges": ["@e"]}, "/distance"),
        ({"edges": ["@e"], "distance": 0}, "/distance"),
        ({"edges": ["@e"], "method": "until"}, "/until"),
        ({"edges": ["@e"], "method": "corner"}, "/to_edges"),
        ({"edges": ["@e"], "distance": 5, "until": ["@f"]}, "/until"),
        ({"edges": ["@e"], "method": "corner", "to_edges": ["@e2"], "output": "new_sheet"},
         "/output"),
        ({"edges": ["@solid"], "distance": 5}, "/edges/0"),
        ({"edges": ["@e"], "method": "corner", "to_edges": ["@e"]}, "/to_edges"),
    ],
)  # fmt: skip
def test_extension_rejections_before_nx(args, field):
    nx = freeform_nx()
    _body, f, e = plate(nx)
    _b2, _f2, e2 = plate(nx, z=50)
    block = nx.block(10, 10, 10)
    solid_edge = block.edges[0]
    solid_edge.GetFaces = lambda: list(block.faces[:2])
    names = {"@e": e.ref, "@e2": e2.ref, "@f": f.ref, "@solid": nx.ref_of(solid_edge, "edge")}

    def put(value):
        if isinstance(value, list):
            return [names.get(v, v) for v in value]
        return value

    bad = run(nx, "nx_create_sheet_extension", **{k: put(v) for k, v in args.items()})
    assert bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_extension_without_license_refuses_before_the_builder():
    nx = freeform_nx()
    _body, _f, e = plate(nx)
    nx.licenses["nx_freeform_1"] = False
    bad = run(nx, "nx_create_sheet_extension", edges=[e.ref], distance=5)
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and not nx.builders


# ---------------------------------------------------------------------------
# Law extension


def law_commit(nx, area, box):
    def commit(_b):
        g = face(nx, flat(), area=area)
        return made(nx, "LAW_EXTENSION", [sheet(nx, box, [g])])

    nx.on_commit("LawExtensionBuilder", commit)


def test_law_extension_from_faces_checks_the_straight_base_area():
    nx = freeform_nx()
    nx.licenses["solid_modeling"] = True
    _body, f, e = plate(nx)
    e.GetLength = lambda: 10.0
    law_commit(nx, 50.0, (20, 0, 0, 20, 10, 5))
    ok = run(nx, "nx_create_law_extension", base=[e.ref], length=5, angle=90)
    assert ok.ok, ok
    b = nx.last("LawExtensionBuilder")
    L = nx.nx.Features.LawExtensionBuilder
    law = nx.nx.GeometricUtilities.LawBuilder.Type
    assert b.Type is L.Types.Faces
    assert b.Faces.ReplaceRules.calls[0][0][0] == [("FaceDumb", [f])]
    assert b.LengthLaw.LawType is law.Constant and b.LengthLaw.Value.RightHandSide == "5.0"
    assert b.AngleLaw.Value.RightHandSide == "90.0"
    assert nx.license_queries == ["solid_modeling"]
    assert ok.data["expected_area"] == pytest.approx(50) and ok.data["gap"] == pytest.approx(0)
    assert ok.data["base_length"] == pytest.approx(10)


def test_law_extension_along_a_vector_with_a_linear_length():
    nx = freeform_nx()
    nx.licenses["solid_modeling"] = True
    _body, _f, e = plate(nx)
    e.GetLength = lambda: 10.0
    law_commit(nx, 60.0, (20, 0, 0, 20, 10, 8))
    ok = run(nx, "nx_create_law_extension", base=[e.ref], reference="vector",
             direction=[0, 0, 2], length=4, end_length=8)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("LawExtensionBuilder")
    assert b.Type is nx.nx.Features.LawExtensionBuilder.Types.Vector
    assert (b.Vector.Vector.X, b.Vector.Vector.Y, b.Vector.Vector.Z) == (0, 0, 1)
    law = b.LengthLaw
    assert law.LawType is nx.nx.GeometricUtilities.LawBuilder.Type.Linear
    assert (law.StartValue.RightHandSide, law.EndValue.RightHandSide) == ("4.0", "8.0")
    assert ok.data["expected_area"] == pytest.approx(60)

    law_commit(nx, 45.0, (20, 0, 0, 20, 10, 8))  # not the trapezoid's area
    bad = run(nx, "nx_create_law_extension", base=[e.ref], reference="vector",
              direction=[0, 0, 1], length=4, end_length=8)  # fmt: skip
    assert bad.error["details"]["check"] == "area", bad


def test_law_extension_detached_from_the_base_rolls_back():
    nx = freeform_nx()
    nx.licenses["solid_modeling"] = True
    _body, _f, e = plate(nx)
    e.GetLength = lambda: 10.0
    law_commit(nx, 50.0, (25, 0, 0, 25, 10, 5))
    bad = run(nx, "nx_create_law_extension", base=[e.ref], length=5)
    assert bad.error["details"]["check"] == "attached", bad
    assert bad.mutation_outcome == "rolled_back"
    assert math.isclose(bad.error["details"]["actual"]["gap"], 5.0)


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"length": 5, "reference": "vector"}, "/direction"),
        ({"length": 5, "direction": [0, 0, 1]}, "/direction"),
        ({"length": 5, "reference": "vector", "direction": [0, 0, 1], "faces": ["@f"]},
         "/faces"),
        ({"length": 0}, "/length"),
        ({"length": 5, "end_length": -1}, "/end_length"),
        ({"length": 5, "reference": "vector", "direction": [0, 0, 0]}, "/direction"),
    ],
)  # fmt: skip
def test_law_extension_rejections_before_nx(args, field):
    nx = freeform_nx()
    _body, f, e = plate(nx)
    args = {k: ([f.ref] if v == ["@f"] else v) for k, v in args.items()}
    bad = run(nx, "nx_create_law_extension", base=[e.ref], **args)
    assert bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started" and not nx.builders
