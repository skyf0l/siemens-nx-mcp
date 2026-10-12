"""nx_measure_assembly_mass and nx_export_mass_report on the fake kit: rows, totals,
assumed masses, NX assertions, groups, the reference frame, what-if changes, suppressed
components and the CSV."""

import csv
import io
from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Component, point
from tests.unit.inspection.fakes import assembly, inspection_nx

HULL = [0, 0, 0, 10, 10, 10]  # 1000 mm^3, CoG (5, 5, 5)
ENGINE = [50, 0, 0, 60, 10, 20]  # 2000 mm^3, CoG (55, 5, 10)


def _boat(nx):
    hull, engine = assembly(nx, ("HULL", HULL), ("ENGINE", ENGINE))
    root = nx.part.ComponentAssembly.RootComponent
    battery = Component("BATTERY", [], parent=root)
    battery.position = point(100, 0, 0)
    nx.remember(battery)
    return hull, engine, battery


def _ref(nx, c):
    return nx.ref_of(c, "component")


def _rows(data):
    return {r["name"]: r for r in data["rows"]}


def test_rows_totals_reference_and_inertia():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    d = nx.density
    ok = run(nx, "nx_measure_assembly_mass", reference={"origin": [0, 5, 0]}, inertia=True)
    assert ok.ok, ok
    data, rows = ok.data, _rows(ok.data)
    assert set(rows) == {"HULL", "ENGINE", "BATTERY"}
    assert rows["HULL"]["mass"] == pytest.approx(1000 * d)
    assert rows["ENGINE"]["center_of_gravity"] == pytest.approx([55, 5, 10])
    assert rows["ENGINE"]["volume"] == pytest.approx(2000) and rows["ENGINE"]["bodies"] == 1
    assert rows["ENGINE"]["density"] == pytest.approx(d * 1e9)  # kg/m^3
    assert rows["ENGINE"]["source"] == "no_material" and rows["ENGINE"]["part"] == "ENGINE"
    assert rows["BATTERY"]["source"] == "none" and rows["BATTERY"]["mass"] == 0
    total = data["total"]
    mass = 3000 * d
    assert total["mass"] == pytest.approx(mass)
    cog = [(1000 * 5 + 2000 * 55) / 3000, 5, (1000 * 5 + 2000 * 10) / 3000]
    assert total["center_of_gravity"] == pytest.approx(cog)
    assert total["reference_cog"] == pytest.approx([cog[0], 0, cog[2]])
    assert rows["HULL"]["reference_cog"] == pytest.approx([5, 0, 5])
    assert rows["ENGINE"]["percent"] == pytest.approx(200 / 3)
    own = 1000 * d * (10**2 + 10**2) / 12  # the fake's per-body moments use its own box
    arm = 1000 * d * (cog[1] - 5) ** 2 + 1000 * d * (cog[2] - 5) ** 2
    assert total["inertia"][0][0] > own + arm  # both bodies, moved to the total CoG
    assert len(total["principal_moments"]) == 3 and "reference_inertia" in total
    assert data["units"] == {"mass": "kg", "length": "mm", "density": "kg/m^3",
                             "inertia": "kg*mm^2"}  # fmt: skip
    codes = [w["message"] for w in ok.warnings]
    assert any("default density" in m for m in codes) and any("BATTERY" in m for m in codes)


def test_items_override_components_and_add_assumed_masses():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    d = nx.density
    ok = run(
        nx,
        "nx_measure_assembly_mass",
        items=[
            {"component": _ref(nx, battery), "mass": 60, "center": [1, 2, 3], "frame": "component"},
            {"component": _ref(nx, engine), "mass": 350, "name": "MAIN ENGINE"},
            {"name": "FUEL", "mass": 150, "center": [30, 5, 5], "group": "fuel"},
        ],
    )
    assert ok.ok, ok
    rows = _rows(ok.data)
    assert rows["BATTERY"]["source"] == "override"
    assert rows["BATTERY"]["center_of_gravity"] == pytest.approx([101, 2, 3])
    engine_row = rows["MAIN ENGINE"]
    assert engine_row["mass"] == 350 and engine_row["center_of_gravity"] == pytest.approx(
        [55, 5, 10]
    )
    assert rows["FUEL"]["source"] == "assumed" and "component" not in rows["FUEL"]
    mass = 1000 * d + 60 + 350 + 150
    assert ok.data["total"]["mass"] == pytest.approx(mass)
    x = (1000 * d * 5 + 60 * 101 + 350 * 55 + 150 * 30) / mass
    assert ok.data["total"]["center_of_gravity"][0] == pytest.approx(x)


def test_item_and_change_arguments_are_refused_before_measuring():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    cases = [
        ({"items": [{"mass": 5, "center": [0, 0, 0]}]}, "/items/0/name"),
        ({"items": [{"name": "X", "mass": 5}]}, "/items/0/center"),
        ({"items": [{"name": "X", "mass": 5, "center": [0, 0, 0], "frame": "component"}]},
         "/items/0/frame"),
        ({"items": [{"component": _ref(nx, battery), "mass": 5}]}, "/items/0/center"),
        ({"what_if": [{"name": "a", "mass": 5}]}, "/what_if/0/center"),
        ({"what_if": [{"name": "a", "item": "X", "offset": [1, 0, 0], "remove": True}]},
         "/what_if/0"),
        ({"group_by": "attribute"}, "/attribute"),
        ({"group_by": "named"}, "/groups"),
        ({"groups": {"g": []}}, "/groups/g"),
        ({"groups": {"g": ["o:1"]}, "group_by": "part"}, "/groups"),
        ({"items": [{"name": "X", "mass": -1, "center": [0, 0, 0]}]}, "/items/0/mass"),
        ({"reference": {"origin": [0, 0, 0], "x_axis": [1, 0, 0], "y_axis": [1, 0, 0]}},
         "/reference/y_axis"),
    ]  # fmt: skip
    for args, field in cases:
        bad = run(nx, "nx_measure_assembly_mass", **args)
        assert bad.error["category"] == "invalid_argument", (args, bad)
        assert bad.error["field"] == field, (args, bad.error)


def test_what_if_adds_moves_and_removes_without_changing_rows():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    d = nx.density
    ok = run(
        nx,
        "nx_measure_assembly_mass",
        items=[{"name": "FUEL", "mass": 10, "center": [0, 0, 0]}],
        reference={"origin": [0, 0, 0], "x_axis": [-1, 0, 0]},
        what_if=[
            {"name": "crew", "mass": 80, "center": [20, 5, 30]},
            {"name": "engine aft", "component": _ref(nx, engine), "offset": [-10, 0, 0]},
            {"name": "hull at", "component": _ref(nx, hull), "move_to": [0, 5, 5]},
            {"name": "no fuel", "item": "fuel", "remove": True},
        ],
    )
    assert ok.ok, ok
    w = ok.data["what_if"]
    base = ok.data["total"]
    assert w["mass_change"] == pytest.approx(70)
    masses = {"hull": 1000 * d, "engine": 2000 * d}
    mass = masses["hull"] + masses["engine"] + 80
    x = (masses["hull"] * 0 + masses["engine"] * 45 + 80 * 20) / mass
    assert w["mass"] == pytest.approx(mass) and w["center_of_gravity"][0] == pytest.approx(x)
    assert w["shift"][0] == pytest.approx(x - base["center_of_gravity"][0])
    assert w["reference_shift"][0] == pytest.approx(-w["shift"][0])  # x_axis is -X
    assert [c["rows"] for c in w["changes"]] == [0, 1, 1, 1]
    assert w["changes"][1]["shift"] == [-10, 0, 0] and w["changes"][3]["mass"] == -10
    assert _rows(ok.data)["ENGINE"]["center_of_gravity"] == pytest.approx([55, 5, 10])
    missing = run(nx, "nx_measure_assembly_mass",
                  what_if=[{"name": "x", "item": "nothing", "remove": True}])  # fmt: skip
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/what_if/0"


def test_grouping_by_part_top_component_attribute_named_and_nx_groups():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    items = [{"name": "FUEL", "mass": 1, "center": [0, 0, 0], "group": "fuel"}]
    by_part = run(nx, "nx_measure_assembly_mass", group_by="part", items=items)
    names = {g["name"]: g for g in by_part.data["groups"]}
    assert set(names) == {"HULL", "ENGINE", "BATTERY", "fuel"}
    assert names["ENGINE"]["rows"] == 1 and names["fuel"]["mass"] == 1
    named = run(
        nx, "nx_measure_assembly_mass",
        groups={"propulsion": ["eng*"], "structure": [_ref(nx, hull)]},
    )  # fmt: skip
    groups = {g["name"]: g for g in named.data["groups"]}
    assert set(groups) == {"propulsion", "structure", "(other)"}
    assert groups["propulsion"]["center_of_gravity"] == pytest.approx([55, 5, 10])
    assert _rows(named.data)["HULL"]["group"] == "structure"
    nx.nx.NXObject = NS(AttributeType=NS(Any=0))
    for c in (hull, engine, battery):
        c.HasUserAttribute = lambda title, kind, index, c=c: c is engine and title == "SYSTEM"
        c.GetUserAttributeAsString = lambda title, kind, index: "propulsion"
    by_attribute = run(nx, "nx_measure_assembly_mass", group_by="attribute", attribute="SYSTEM")
    groups = {g["name"] for g in by_attribute.data["groups"]}
    assert groups == {"propulsion", "(other)"}, by_attribute
    nx.part.ComponentGroups = [
        NS(Name="AllComponents", GetComponents=lambda: [hull, engine, battery]),
        NS(Name="Engines", GetComponents=lambda: [engine]),
    ]
    by_group = run(nx, "nx_measure_assembly_mass", group_by="component_group")
    assert {g["name"] for g in by_group.data["groups"]} == {"Engines", "(other)"}


def test_include_exclude_and_suppressed_components():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    only = run(nx, "nx_measure_assembly_mass", components=[_ref(nx, engine)])
    assert [r["name"] for r in only.data["rows"]] == ["ENGINE"]
    without = run(nx, "nx_measure_assembly_mass", exclude=[_ref(nx, engine)])
    assert "ENGINE" not in _rows(without.data)
    assert without.data["skipped"] == [{"component": nx.row(engine, "component"),
                                        "reason": "excluded"}]  # fmt: skip
    engine.IsSuppressed = True
    skipped = run(nx, "nx_measure_assembly_mass")
    assert skipped.data["skipped"][0]["reason"] == "suppressed"
    proto = engine.Prototype.Bodies[0]
    proto.volume = 2000
    nx.remember(proto)
    engine.position = point(100, 0, 0)  # the prototype body is placed with the pose
    included = run(nx, "nx_measure_assembly_mass", include_suppressed=True)
    row = _rows(included.data)["ENGINE"]
    assert row["suppressed"] is True and row["center_of_gravity"] == pytest.approx([155, 5, 10])
    stranger = Component("OTHER", [])
    nx.remember(stranger)
    bad = run(nx, "nx_measure_assembly_mass", components=[_ref(nx, stranger)])
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.error["field"] == "/components/0"


def test_nx_assertions_are_used_unless_disabled():
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    asserted = {int(battery.Tag): (60.0, [1000.0, 0.0, 0.0]),  # mm, component frame
                int(id(engine.Prototype)): (350.0, None)}  # fmt: skip
    engine.Prototype.Tag = id(engine.Prototype)

    def ask(tag, units):
        mass, center = asserted.get(int(tag), (None, None))
        return NS(Mass=mass or 0.0, MassState=2 if mass else 0,
                  CenterOfMass=center or [0, 0, 0], CofmState=2 if center else 0)  # fmt: skip

    nx.uf.Weight = NS(AskProps=ask)
    nx.nx.UF = NS(Weight=NS(StateType=NS(ASSERTED=2), UnitsType=NS(UNITS_KMM=6)))
    ok = run(nx, "nx_measure_assembly_mass")
    rows = _rows(ok.data)
    assert rows["BATTERY"]["source"] == "asserted_component"
    assert rows["BATTERY"]["center_of_gravity"] == pytest.approx([1100, 0, 0])
    assert rows["ENGINE"]["source"] == "asserted_part" and rows["ENGINE"]["mass"] == 350
    assert rows["ENGINE"]["center_of_gravity"] == pytest.approx([55, 5, 10])  # measured


def test_part_bodies_materials_sorting_and_truncation():
    nx = inspection_nx()
    small = nx.block(1, 1, 1)
    big = nx.block(2, 2, 2, origin=(10, 0, 0))
    steel = NS(Name="STEEL")
    nx.part.MaterialManager = NS(PhysicalMaterials=NS(
        AskMaterialOfObject=lambda obj: steel if obj is big else None))  # fmt: skip
    ok = run(nx, "nx_measure_assembly_mass")
    assert ok.ok, ok
    assert ok.data["row_count"] == 2 and ok.data["truncated"] is False
    row = ok.data["rows"][1]
    assert (
        row["body"]["id"] == big.ref and row["material"] == "STEEL" and row["source"] == "material"
    )
    assert ok.data["total"]["mass"] == pytest.approx(9 * nx.density)
    assert small.ref


def test_export_writes_an_excel_friendly_csv(tmp_path):
    nx = inspection_nx()
    hull, engine, battery = _boat(nx)
    root = services(nx).root
    target = root / "reports" / "weights.csv"
    ok = run(
        nx, "nx_export_mass_report", path=str(target),
        report_args={"group_by": "top_component",
                     "items": [{"name": "FUEL", "mass": 150, "center": [30, 5, 5]}],
                     "reference": {"origin": [0, 5, 0]},
                     "what_if": [{"name": "crew", "mass": 80, "center": [20, 5, 30]}]},
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["path"] == "reports/weights.csv" and ok.data["row_count"] == 4
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM: Excel reads the encoding
    lines = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    header = lines[0]
    sections = [line[0] for line in lines[1:] if line]
    assert sections.count("component") == 3 and sections.count("item") == 1
    assert "group" in sections and "total" in sections and "what_if_total" in sections
    total = next(line for line in lines if line and line[0] == "total")
    assert float(total[header.index("mass_kg")]) == pytest.approx(ok.data["total"]["mass"])
    assert float(total[header.index("ref_y")]) == pytest.approx(0, abs=1e-9)
    again = run(nx, "nx_export_mass_report", path=str(target))
    assert again.error["code"] == "NX_ALREADY_EXISTS"
    replaced = run(nx, "nx_export_mass_report", path=str(target), overwrite=True)
    assert replaced.ok and replaced.data["overwritten"] is True
    bad_args = run(nx, "nx_export_mass_report", path=str(root / "x.csv"),
                   report_args={"group_by": "nonsense"})  # fmt: skip
    assert bad_args.error["field"] == "/report_args/group_by", bad_args.error
    assert bad_args.mutation_outcome == "not_started" and not (root / "x.csv").exists()
    wrong = run(nx, "nx_export_mass_report", path=str(root / "w.txt"))
    assert wrong.error["category"] in ("invalid_argument", "unsupported"), wrong.error
