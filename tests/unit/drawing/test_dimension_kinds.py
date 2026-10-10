"""Chamfer, thickness, arc-length and baseline/chain dimensions; ISO 286 fits."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import dimension_kinds as kinds
from nx_mcp.domains.drawing import fits
from tests.kit import run
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view

# ---- pure rules ---------------------------------------------------------------------------


def test_chamfer_size_is_the_leg_along_the_reference():
    assert kinds.chamfer_values([10, -10], [1, 0]) == pytest.approx((10, 45))
    assert kinds.chamfer_values([-10, 10], [0, 5]) == pytest.approx((10, 45))
    assert kinds.chamfer_values([8, -4], [1, 0]) == pytest.approx((8, 26.565051))
    assert kinds.chamfer_values([0, 0], [1, 0]) == (None, None)


def test_line_gap_and_arc_sweep():
    assert kinds.line_gap([0, 0], [10, 0], [3, 7]) == pytest.approx(7)
    quarter = {"center": [0, 0, 0], "start": [6, 0, 0], "samples": [[6 * math.cos(math.pi / 6),
               6 * math.sin(math.pi / 6), 0]], "closed": False}  # fmt: skip
    assert kinds.arc_sweep(quarter) == pytest.approx(math.pi / 2)
    assert kinds.arc_sweep({"closed": True}) == pytest.approx(2 * math.pi)


def test_baseline_and_chain_values():
    points = [[0, 0, 0], [25, 3, 0], [60, 9, 0], [80, 1, 0]]
    x = [1, 0, 0]
    assert kinds.set_values(points, x, "baseline") == [25, 60, 80]
    assert kinds.set_values(points, x, "chain") == [25, 35, 20]
    assert kinds.set_values(points, [0, 1, 0], "chain") == [3, 6, 8]


@pytest.mark.parametrize(
    ("role", "text", "size", "upper", "lower"),
    [
        ("hole", "H7", 16, 18, 0),
        ("shaft", "g6", 16, -6, -17),
        ("hole", "H7", 18, 18, 0),  # 18 belongs to 10-18
        ("hole", "H7", 18.001, 21, 0),
        ("hole", "K7", 10, 5, -10),  # delta rule
        # up to 3 mm there is no delta (ISO 286-2 tables)
        ("hole", "M7", 3, -2, -12),
        ("hole", "M7", 1, -2, -12),
        ("hole", "M7", 3.01, 0, -12),  # 3-6: -4 + delta 4
        ("hole", "K7", 3, 0, -10),
        ("hole", "N7", 3, -4, -14),
        ("hole", "P7", 3, -6, -16),
        ("hole", "M6", 3, -2, -8),
        ("hole", "N8", 3.01, -2, -20),
        ("hole", "K6", 3.01, 2, -6),
        ("hole", "K7", 25, 6, -15),
        ("hole", "M7", 15, 0, -18),
        ("hole", "N7", 50, -8, -33),
        ("hole", "P7", 15, -11, -29),
        ("hole", "F8", 10, 35, 13),
        ("hole", "D10", 70, 220, 100),
        ("hole", "JS7", 10, 7.5, -7.5),
        ("shaft", "js6", 10, 4.5, -4.5),
        ("shaft", "k6", 25, 15, 2),
        ("shaft", "k8", 70, 46, 0),  # k beyond IT7 starts at 0
        ("shaft", "m6", 50, 25, 9),
        ("shaft", "n6", 50, 33, 17),
        ("shaft", "p6", 70, 51, 32),
        ("shaft", "e8", 50, -50, -89),
        ("shaft", "d9", 50, -80, -142),
        ("shaft", "h11", 500, 0, -400),
    ],
)
def test_iso_286_deviations(role, text, size, upper, lower):
    assert fits.deviations(role, text, size) == pytest.approx((upper, lower))


@pytest.mark.parametrize(
    ("role", "text", "size", "says"),
    [
        ("hole", "Z7", 16, "not supported"),
        ("hole", "K9", 16, "up to grade 8"),
        ("hole", "P8", 16, "up to grade 7"),
        ("hole", "H12", 16, "IT5-IT11"),
        ("hole", "h7", 16, "upper-case"),
        ("shaft", "G6", 16, "lower-case"),
        ("shaft", "r6", 16, "not supported"),
        ("hole", "H7", 501, "up to 500"),
        ("hole", "H7", 0, "up to 500"),
        ("hole", "7H", 16, "not a tolerance class"),
    ],
)
def test_unsupported_fit_classes_explain_why(role, text, size, says):
    found = fits.deviations(role, text, size)
    assert isinstance(found, str) and says in found


# ---- tools on the fake --------------------------------------------------------------------


def kinds_nx():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    nx.top = make_view(nx, sheet, pos=(100, 100))
    nx.side = make_view(
        nx, sheet, "Front@2", pos=(100, 40), axes=((1, 0, 0), (0, 0, 1), (0, -1, 0))
    )
    nx.back = line_edge(nx, (6, 50, 20), (70, 50, 20))
    nx.front = line_edge(nx, (80, 0, 20), (8, 0, 20))
    nx.left = line_edge(nx, (0, 4, 20), (0, 44, 20))
    nx.right = line_edge(nx, (80, 40, 20), (80, 0, 20))
    nx.ch = line_edge(nx, (80, 40, 20), (70, 50, 20))
    nx.ach = line_edge(nx, (0, 4, 20), (8, 0, 20))
    nx.hole = arc_edge(nx, (25, 25, 20), 8)
    nx.ring = arc_edge(nx, (25, 25, 20), 5)
    nx.fillet = arc_edge(nx, (6, 44, 20), 6, full=False)
    edges = [nx.back, nx.front, nx.left, nx.right, nx.ch, nx.ach, nx.hole, nx.ring, nx.fillet]
    install_eval(nx, edges)
    form = nx.nx.UF.Drf.ChamferDimensionForm.CHAMFER_FORM_SIZEANGLE
    nx.uf.Drf = NS(AskChamferDimensionData=lambda tag: NS(Form=form))
    return nx


def dimension_hook(nx, value):
    def commit(b):
        p = b.Origin.OriginPoint
        d = NS(Tag=7000 + len(nx.part.Dimensions), Name="DIM", ComputedSize=value,
               AnnotationOrigin=p, OwningPart=nx.part, IsOccurrence=False)  # fmt: skip
        nx.part.Dimensions.append(d)
        return d

    return commit


def create(nx, **args):
    return run(nx, "nx_create_drawing_dimension", view=nx.top.ref, **args)


def test_chamfer_is_checked_and_uses_the_size_x_angle_form():
    nx = kinds_nx()
    nx.on_commit("ChamferDimensionBuilder", dimension_hook(nx, 10.0))
    ok = create(nx, kind="chamfer", first=nx.ch.ref, second=nx.back.ref, origin=[200, 170])
    assert ok.ok, ok
    row = ok.data["dimension"]
    assert row["value"] == 10 and row["expected"] == pytest.approx(10)
    assert row["angle"] == pytest.approx(45)
    assert ok.data["origin"] == pytest.approx([200, 170])
    b = nx.last("ChamferDimensionBuilder")
    assert b.Style.DimensionStyle.ChamferForm is nx.nx.Annotations.ChamferForm.SizeAngle
    nx.on_commit("ChamferDimensionBuilder", dimension_hook(nx, 14.142))
    bad = create(nx, kind="chamfer", first=nx.ch.ref, second=nx.right.ref)
    assert bad.error["category"] == "verification_failed" and nx.live_builders == 0


def test_chamfer_refusals_happen_before_nx():
    nx = kinds_nx()
    uneven = create(nx, kind="chamfer", first=nx.ach.ref, second=nx.front.ref)
    assert uneven.error["code"] == "NX_UNSUPPORTED_GEOMETRY" and uneven.error["field"] == "/first"
    arc = create(nx, kind="chamfer", first=nx.hole.ref, second=nx.back.ref)
    assert arc.error["code"] == "NX_WRONG_GEOMETRY" and arc.error["field"] == "/first"
    alone = create(nx, kind="chamfer", first=nx.ch.ref)
    assert alone.error["field"] == "/second"
    same = create(nx, kind="chamfer", first=nx.ch.ref, second=nx.ch.ref)
    assert same.error["field"] == "/second"
    for result in (uneven, arc, alone, same):
        assert result.mutation_outcome == "not_started"
    assert not nx.builders


def test_thickness_of_parallel_edges_and_concentric_arcs():
    nx = kinds_nx()
    nx.on_commit("ThicknessDimensionBuilder", dimension_hook(nx, 50.0))
    ok = create(nx, kind="thickness", first=nx.back.ref, second=nx.front.ref)
    assert ok.ok and ok.data["dimension"]["expected"] == pytest.approx(50)
    nx.on_commit("ThicknessDimensionBuilder", dimension_hook(nx, 3.0))
    ring = create(nx, kind="thickness", first=nx.hole.ref, second=nx.ring.ref)
    assert ring.ok and ring.data["dimension"]["expected"] == pytest.approx(3)
    skew = create(nx, kind="thickness", first=nx.front.ref, second=nx.left.ref)
    mixed = create(nx, kind="thickness", first=nx.hole.ref, second=nx.back.ref)
    edge_on = run(nx, "nx_create_drawing_dimension", view=nx.side.ref, kind="thickness",
                  first=nx.hole.ref, second=nx.ring.ref)  # fmt: skip
    for result in (skew, mixed, edge_on):
        assert result.error["code"] == "NX_UNSUPPORTED_GEOMETRY", result
        assert result.mutation_outcome == "not_started"


def test_arc_length_is_radius_times_sweep():
    nx = kinds_nx()
    nx.on_commit("CurveLengthDimensionBuilder", dimension_hook(nx, 3 * math.pi))
    ok = create(nx, kind="arc_length", first=nx.fillet.ref)
    assert ok.ok, ok
    assert ok.data["dimension"]["expected"] == pytest.approx(3 * math.pi)
    assert ok.data["dimension"]["sweep"] == pytest.approx(90)
    nx.on_commit("CurveLengthDimensionBuilder", dimension_hook(nx, 16 * math.pi))
    full = create(nx, kind="arc_length", first=nx.hole.ref)
    assert full.ok and full.data["dimension"]["sweep"] == pytest.approx(360)
    line = create(nx, kind="arc_length", first=nx.back.ref)
    assert line.error["code"] == "NX_WRONG_GEOMETRY"
    two = create(nx, kind="arc_length", first=nx.hole.ref, second=nx.back.ref)
    assert two.error["field"] == "/second"


class DimensionSets:
    """``part.Annotations.DimensionSets``: members measure along x or y between picks."""

    def __init__(self, nx, values=None):
        self.nx, self.values, self.made, self.condemned, self.redrawn = nx, values, [], False, []

    def __getattr__(self, name):
        horizontal = "Horizontal" in name
        kind = "baseline" if "Baseline" in name else "chain"

        def create(data, origin):
            picks = [a.PickPoint for a in data.items]
            along = [p.X if horizontal else p.Y for p in picks]
            values = self.values or kinds.set_values([[v, 0, 0] for v in along], [1, 0, 0], kind)
            first = 9100 + 10 * len(self.made)
            members = [NS(Tag=first + i, Name="DIM", ComputedSize=v, OwningPart=self.nx.part,
                          IsOccurrence=False) for i, v in enumerate(values)]  # fmt: skip
            for m in members:
                m.RedisplayObject = lambda m=m: self.redrawn.append(m.Tag)
            native = NS(Tag=9000 + len(self.made), Name="SET", OwningPart=self.nx.part, IsOccurrence=False,
                        GetAllSubDimensions=lambda: members, name=name, origin=origin)  # fmt: skip
            # NX 2406: a member it never makes alive is not in part.Dimensions (not drawn)
            self.nx.part.Dimensions.extend(members[: -1 if self.condemned else None])
            self.made.append(native)
            return native

        return create


def dimension_data():
    data = NS(items=[], Dispose=lambda: None)
    data.SetAssociativity = lambda i, items: data.items.extend(items)
    return data


def with_sets(nx, values=None):
    nx.part.Annotations.NewDimensionData = dimension_data
    nx.part.Annotations.NewAssociativity = lambda: NS(Dispose=lambda: None)
    nx.part.Annotations.DimensionSets = DimensionSets(nx, values)
    return nx.part.Annotations.DimensionSets


def test_baseline_and_chain_sets_are_checked_per_member():
    nx = kinds_nx()
    sets = with_sets(nx)
    objects = [nx.left.ref, nx.hole.ref, nx.right.ref]
    base = create(nx, kind="baseline", objects=objects)
    assert base.ok, base
    assert [r["value"] for r in base.data["dimensions"]] == [25, 80]
    assert sets.made[-1].name == "CreateHorizontalBaselineDimension"
    chain = create(nx, kind="chain", objects=objects, direction="horizontal", origin=[90, 160])
    assert [r["expected"] for r in chain.data["dimensions"]] == [25, 55]
    assert chain.data["origin"] == [90, 160]
    vertical = create(nx, kind="chain", objects=[nx.front.ref, nx.hole.ref, nx.back.ref],
                      direction="vertical")  # fmt: skip
    assert sets.made[-1].name == "CreateVerticalChainDimension"
    assert [r["value"] for r in vertical.data["dimensions"]] == [25, 25]
    sets.values = [25, 81]
    bad = create(nx, kind="baseline", objects=objects)
    assert bad.error["category"] == "verification_failed"
    assert bad.mutation_outcome == "rolled_back"


def test_set_members_are_checked_drawn_and_redisplayed():
    nx = kinds_nx()
    sets = with_sets(nx)
    objects = [nx.left.ref, nx.hole.ref, nx.right.ref]
    ok = create(nx, kind="chain", objects=objects)
    assert ok.ok, ok
    # NX draws a new set only at the next display update: every member is redisplayed
    assert sets.redrawn == [m.Tag for m in sets.made[-1].GetAllSubDimensions()]
    sets.condemned = True
    hidden = create(nx, kind="chain", objects=objects)
    assert hidden.error["category"] == "verification_failed"
    assert hidden.error["details"]["check"] == "shown[1]"
    assert hidden.mutation_outcome == "rolled_back"


def test_two_point_sets_are_refused_before_nx():
    nx = kinds_nx()
    sets = with_sets(nx)
    for kind in ("chain", "baseline"):
        two = create(nx, kind=kind, objects=[nx.left.ref, nx.right.ref], direction="horizontal")
        with_datum = create(nx, kind=kind, datum=nx.left.ref, objects=[nx.right.ref])
        for result in (two, with_datum):
            assert result.error["code"] == "NX_INVALID_ARGUMENT"
            assert result.error["field"] == "/objects"
            assert "kind linear" in result.error["hint"] and "got 2" in result.error["message"]
            assert result.mutation_outcome == "not_started"
    assert not sets.made


def test_set_refusals_happen_before_nx():
    nx = kinds_nx()
    sets = with_sets(nx)
    one = create(nx, kind="chain", objects=[nx.left.ref])
    assert one.error["field"] == "/objects"
    # Two points make one (invisible on NX 2406) set member: a linear dimension instead.
    two = create(nx, kind="chain", objects=[nx.left.ref, nx.hole.ref])
    assert two.error["field"] == "/objects" and "kind linear" in two.error["hint"]
    assert "got 2" in two.error["message"]
    dup = create(nx, kind="chain", objects=[nx.left.ref, nx.left.ref, nx.right.ref])
    assert dup.error["field"] == "/objects/1"
    level = create(nx, kind="chain", objects=[nx.front.ref, nx.hole.ref, nx.ring.ref],
                   direction="vertical")  # fmt: skip
    assert level.error["field"] == "/objects/2" and level.error["category"] == "invalid_argument"
    assert "objects/2 is at the same vertical position as objects/1" in level.error["message"]
    snap = create(nx, kind="baseline", objects=[nx.hole.ref, nx.left.ref, nx.right.ref],
                  object_point="end")  # fmt: skip
    assert snap.error["code"] == "NX_WRONG_GEOMETRY" and snap.error["field"] == "/objects/0"
    on_datum = create(nx, kind="baseline", datum=nx.left.ref, objects=[nx.right.ref, nx.left.ref])
    assert on_datum.error["field"] == "/objects"
    for result in (one, two, dup, level, snap, on_datum):
        assert result.mutation_outcome == "not_started"
    assert not sets.made


def test_a_set_datum_is_its_first_point():
    nx = kinds_nx()
    sets = with_sets(nx)
    base = create(nx, kind="baseline", datum=nx.left.ref, objects=[nx.hole.ref, nx.right.ref])
    assert base.ok, base
    assert [r["value"] for r in base.data["dimensions"]] == [25, 80]
    assert sets.made[-1].name == "CreateHorizontalBaselineDimension"
    # the datum point snaps like an object: a circle at its center
    centre = create(nx, kind="chain", datum=nx.hole.ref, objects=[nx.left.ref, nx.right.ref])
    assert [r["value"] for r in centre.data["dimensions"]] == [25, 80]
    # an object level with the datum is named against the datum
    level = create(nx, kind="baseline", datum=nx.left.ref, objects=[nx.right.ref, nx.ach.ref],
                   direction="horizontal")  # fmt: skip
    assert level.error["field"] == "/objects/1" and "as datum" in level.error["message"]


# ---- fits ---------------------------------------------------------------------------------


class Prefs:
    def __init__(self, d):
        self.d = d

    def GetUnitsFormatPreferences(self):
        return NS(DisplayTrailingZeros=False, PrimaryDimensionUnit=self.d.unit,
                  DecimalPointCharacter=self.d.point, Dispose=lambda: None)  # fmt: skip

    def Dispose(self):
        pass


class FitStyle:
    """NX: deviations are recomputed on commit only when the classes change; in Fit mode
    only the shaft's."""

    def __init__(self, nx, d):
        self.nx, self.d, self.values = nx, d, {}

    def __setattr__(self, name, value):
        if name in ("nx", "d", "values"):
            object.__setattr__(self, name, value)
        else:
            self.values[name] = value

    def commit(self):
        d, v, A = self.d, self.values, self.nx.nx.Annotations
        d.ToleranceType = v.get("ToleranceType", d.ToleranceType)
        d.LimitFitDisplayStyle = v.get("LimitFitDisplayStyle", d.LimitFitDisplayStyle)
        mode = v.get("LimitFitAnsiHoleType", d.LimitFitEnhancedDisplayType)
        changed = mode is not d.LimitFitEnhancedDisplayType
        d.LimitFitEnhancedDisplayType = mode
        for key in ("LimitFitDeviation", "LimitFitGrade", "LimitFitShaftDeviation",
                    "LimitFitShaftGrade"):  # fmt: skip
            if key in v and v[key] != getattr(d, key):
                setattr(d, key, v[key])
                changed = True
        if not changed:
            return
        size = d.ComputedSize
        if mode is not A.FitAnsiHoleType.Shaft:
            hole = fits.deviations("hole", f"{d.LimitFitDeviation}{d.LimitFitGrade}", size)
            if mode is A.FitAnsiHoleType.Hole:
                d.UpperMetricToleranceValue, d.LowerMetricToleranceValue = (x / 1000 for x in hole)
        if mode is not A.FitAnsiHoleType.Hole:
            shaft = fits.deviations(
                "shaft", f"{d.LimitFitShaftDeviation}{d.LimitFitShaftGrade}", size
            )
            d.ShaftUpperToleranceValue, d.ShaftLowerToleranceValue = (x / 1000 for x in shaft)


def fit_nx(kind="DiameterDimension", size=16.0):
    nx = kinds_nx()
    A = nx.nx.Annotations
    cls = type(kind, (NS,), {})
    d = cls(Tag=8100, Name="D", ComputedSize=size, NumberOfAssociativities=1, IsRetained=False,
            OwningPart=nx.part, IsOccurrence=False, NominalDecimalPlaces=1,
            ToleranceType=A.ToleranceType.NotSet, ToleranceDecimalPlaces=3,
            UpperMetricToleranceValue=0.1, LowerMetricToleranceValue=-0.1,
            UpperToleranceValue=0.005, LowerToleranceValue=-0.005,
            ShaftUpperToleranceValue=0.0, ShaftLowerToleranceValue=0.0,
            LimitFitDeviation="H", LimitFitGrade=7, LimitFitShaftDeviation="g",
            LimitFitShaftGrade=6, LimitFitEnhancedDisplayType=A.FitAnsiHoleType.Hole,
            LimitFitDisplayStyle=A.FitDisplayStyle.FitSymbols,
            unit=A.DimensionUnit.Millimeters, point=A.DecimalPointCharacter.Period,
            RedisplayObject=lambda: None, AnnotationOrigin=None)  # fmt: skip
    d.GetDimensionPreferences = lambda: Prefs(d)
    d.SetDimensionPreferences = lambda prefs: None
    nx.part.Dimensions.append(d)
    nx.commits = []

    def settings(objects):
        style = FitStyle(nx, objects[0])
        builder = NS(AnnotationStyle=NS(DimensionStyle=style), Destroy=lambda: None)
        builder.Commit = lambda: (nx.commits.append(dict(style.values)), style.commit())[0]
        return builder

    nx.part.SettingsManager.CreateAnnotationEditSettingsBuilder = settings
    return nx, d, nx.ref_of(d, "dimension")


def test_fit_limits_are_computed_by_nx_and_checked_against_iso_286():
    nx, d, ref = fit_nx()
    ok = run(nx, "nx_edit_dimension", dimension=ref,
             tolerance={"type": "fit", "hole": "H7", "shaft": "g6", "display": "symbol_and_limits"})  # fmt: skip
    assert ok.ok, ok
    tol = ok.data["tolerance"]
    assert tol["type"] == "fit" and tol["hole"] == "H7" and tol["shaft"] == "g6"
    assert tol["display"] == "symbol_and_limits"
    assert tol["upper"] == pytest.approx(0.018) and tol["lower"] == pytest.approx(0)
    assert tol["shaft_upper"] == pytest.approx(-0.006)
    assert tol["limits"]["shaft"] == pytest.approx([15.983, 15.994])
    # H7 and g6 were already set: each class was primed with another grade first
    assert [c.get("LimitFitGrade", c.get("LimitFitShaftGrade")) for c in nx.commits] == [8, 7, 7, 6]
    shaft = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"type": "fit", "shaft": "f7"})
    assert shaft.ok and "hole" not in shaft.data["tolerance"]
    assert shaft.data["tolerance"]["limits"] == {"shaft": pytest.approx([15.966, 15.984])}


def test_fit_request_refusals_happen_before_nx():
    nx, d, ref = fit_nx()
    cases = [
        ({"type": "fit"}, "invalid_argument", "/tolerance/hole"),
        ({"type": "fit", "hole": "H7", "upper": 0.1}, "invalid_argument", "/tolerance/upper"),
        ({"type": "bilateral", "shaft": "g6"}, "invalid_argument", "/tolerance/shaft"),
        ({"type": "fit", "hole": "Z7"}, "unsupported", "/tolerance/hole"),
        ({"type": "fit", "hole": "7h"}, "invalid_argument", "/tolerance/hole"),
        ({"type": "fit", "shaft": "r6"}, "unsupported", "/tolerance/shaft"),
    ]
    for tolerance, category, field in cases:
        result = run(nx, "nx_edit_dimension", dimension=ref, tolerance=tolerance)
        assert result.error["category"] == category, (tolerance, result.error)
        assert result.error["field"] == field and result.mutation_outcome == "not_started"
    inch = run(nx, "nx_edit_dimension", dimension=ref, format={"units": "in"},
               tolerance={"type": "fit", "hole": "H7"})  # fmt: skip
    assert inch.error["field"] == "/tolerance/type" and inch.mutation_outcome == "not_started"
    assert not nx.commits


def test_fits_are_refused_on_angles_and_oversized_dimensions():
    for kind, size, field in (("AngularDimension", 45.0, "/tolerance/type"),
                              ("DiameterDimension", 600.0, "/tolerance/hole")):  # fmt: skip
        nx, d, ref = fit_nx(kind, size)
        result = run(
            nx, "nx_edit_dimension", dimension=ref, tolerance={"type": "fit", "hole": "H7"}
        )
        assert result.error["code"] == "NX_UNSUPPORTED_OPTION" and result.error["field"] == field
        assert result.mutation_outcome == "not_started" and not nx.commits


def test_fit_classes_are_taken_in_any_case():
    nx, d, ref = fit_nx()
    ok = run(nx, "nx_edit_dimension", dimension=ref,
             tolerance={"type": "fit", "hole": "h7", "shaft": "G6"})  # fmt: skip
    assert ok.ok, ok
    assert ok.data["tolerance"]["hole"] == "H7" and ok.data["tolerance"]["shaft"] == "g6"
    js = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"type": "fit", "hole": "js7"})
    assert js.ok and js.data["tolerance"]["hole"] == "JS7"


def test_deviations_follow_the_tolerance_type():
    nx, d, ref = fit_nx()
    fresh = run(nx, "nx_get_dimension", dimension=ref)
    assert fresh.data["tolerance"]["type"] == "none"
    assert "upper" not in fresh.data["tolerance"] and "lower" not in fresh.data["tolerance"]
    hidden = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"upper": 0.2})
    assert hidden.error["field"] == "/tolerance/upper" and hidden.mutation_outcome == "not_started"
    assert "bilateral, symmetric or limits" in hidden.error["message"]
    sym = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"type": "symmetric", "upper": 0.2})
    assert sym.ok, sym
    assert sym.data["tolerance"]["upper"] == pytest.approx(0.2)
    assert sym.data["tolerance"]["lower"] == pytest.approx(-0.2)
    from_lower = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"lower": -0.05})
    assert from_lower.ok and from_lower.data["tolerance"]["upper"] == pytest.approx(0.05)
    clash = run(nx, "nx_edit_dimension", dimension=ref,
                tolerance={"type": "symmetric", "upper": 0.1, "lower": -0.2})  # fmt: skip
    assert clash.error["field"] == "/tolerance/lower" and clash.mutation_outcome == "not_started"
    d.UpperMetricToleranceValue = 0.3
    keep = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"type": "symmetric"})
    assert keep.data["tolerance"]["lower"] == pytest.approx(-0.3)
    none = run(nx, "nx_edit_dimension", dimension=ref, tolerance={"type": "none"})
    assert none.ok and set(none.data["tolerance"]) == {"type", "decimals"}


def rebind_nx(kind="HorizontalDimension"):
    """A linear dimension on ``front`` whose builder rebinds it to the edges it is given."""
    nx, d, ref = fit_nx(kind, 72.0)
    bound = [nx.front, nx.front]
    d.NumberOfAssociativities, d.AnnotationOrigin = 2, nx.nx.Point3d(140.0, 120.0, 0.0)
    d.GetAssociativity = lambda i: NS(ObjectView=nx.top, FirstObject=bound[i - 1])

    def commit(b):
        if nx.rebinds:
            bound[:] = [b.FirstAssociativity.SetValue.calls[-1][0][1],
                        b.SecondAssociativity.SetValue.calls[-1][0][1]]  # fmt: skip
        d.ComputedSize = nx.size
        return d

    nx.rebinds, nx.size = True, 64.0
    nx.on_commit("LinearDimensionBuilder", commit)
    return nx, d, ref


def test_rebinding_reads_back_the_bound_edges():
    nx, d, ref = rebind_nx()
    ok = run(nx, "nx_edit_dimension", dimension=ref, edges=[nx.back.ref])
    assert ok.ok, ok
    assert ok.data["value"] == 64 and [o["id"] for o in ok.data["objects"]] == [nx.back.ref] * 2
    nx2, _, ref2 = rebind_nx()
    nx2.rebinds = False  # NX kept the old edges (and showed the expected value)
    kept = run(nx2, "nx_edit_dimension", dimension=ref2, edges=[nx2.back.ref])
    assert kept.error["category"] == "verification_failed", kept
    assert kept.mutation_outcome == "rolled_back"


def test_rebind_errors_name_the_edges_field():
    nx, d, ref = rebind_nx()
    end_on = run(nx, "nx_edit_dimension", dimension=ref, edges=[nx.left.ref])
    assert end_on.error["field"] == "/edges/0" and end_on.mutation_outcome == "not_started"
    assert "horizontal distance" in end_on.error["message"]
    assert "own length" in end_on.error["hint"]
    level = run(nx, "nx_edit_dimension", dimension=ref, edges=[nx.left.ref, nx.ach.ref])
    assert level.error["field"] == "/edges/1"
