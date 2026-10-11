"""NX material library: list, assign, read back and refuse, on the shared fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling.material import parse_property
from tests.kit import run
from tests.unit.modeling.fakes import modeling_nx

LIBRARY = {
    "Steel": {
        "Category": "METAL",
        "MassDensityConstant": "7.829e-06 kg/mm\xb3",
        "YoungsModulusConstant": "206940000 kPa",
        "PoissonsRatioConstant": "0.288 ",
        "ThermalExpansionConstant": "1.128e-05 1/\xb0C",
        "ThermalConductivity": (
            "Tabular Data: \n   temperature    Thermal Conductivity (K) \n"
            "   \xb0C   \xb5W/(mm\xb7\xb0C) \n   10   56000 \n   20   55700 \n   50   54850 "
        ),
        "SpecificHeat": "434000000 \xb5J/(kg\xb7K)",
    },
    "Aluminum_6061": {
        "Category": "METAL",
        "MassDensityConstant": "2.711e-06 kg/mm\xb3",
        "YoungsModulusConstant": "68980000 kPa",
        "PoissonsRatioConstant": "0.33 ",
    },
    "ABS": {"Category": "PLASTIC", "MassDensityConstant": "1.05e-06 kg/mm\xb3"},
    "Air": {"Category": "OTHER", "ThermalConductivity": "26.3 \xb5W/(mm\xb7\xb0C)"},
}


LIBRARY_PATH = "C:/NX/ugii/materials/physicalmateriallibrary.xml"


def library_nx(names=LIBRARY):
    nx = modeling_nx()
    nx.nx.UF = NS(Modl=NS(DensityUnits=NS(KILOGRAMS_METERS=1)))
    assigned, loads = {}, []

    class Material:
        def __init__(self, name, density, library=None):
            self.Name, self.density, self.library = name, density, library
            self.OwningPart = nx.part

        def IsLoadedLibraryMaterial(self):
            return self.library is not None

        def GetLibraryName(self):
            return self.library

        def AssignObjects(self, bodies):
            for b in bodies:
                nx.densities[int(b.Tag)] = self.density
                assigned[int(b.Tag)] = self

    class Materials(list):
        def GetMaterialsFromLibrary(self, library):
            if library not in ("", LIBRARY_PATH):
                raise RuntimeError("Material Library format error; unable to read.")
            return list(names)

        def GetMaterialPropertyValueAndDisplayName(self, library, name, prop):
            value = names[name].get(prop)
            if value is None:
                raise RuntimeError("The material property is missing.")
            return value, prop

        def GetLoadedLibraryMaterial(self, library, name):
            return next((m for m in self if m.library is not None and m.Name == name), None)

        def LoadFromNxmatmllibrary(self, name):
            density = parse_property(names[name]["MassDensityConstant"], "density")
            made = Material(name, density, LIBRARY_PATH)
            loads.append(name)
            self.append(made)
            return made

        def CreatePhysicalMaterialBuilder(self, kind):
            return nx.builder("PhysicalMaterialBuilder", kind)

        def AskMaterialOfObject(self, body):
            return assigned.get(int(body.Tag))

    materials = Materials()
    nx.part.MaterialManager = NS(PhysicalMaterials=materials)
    nx.Material, nx.materials, nx.loads = Material, materials, loads
    return nx


@pytest.mark.parametrize(
    ("text", "dim", "wanted"),
    [
        ("7.829e-06 kg/mm\xb3", "density", 7829.0),
        ("206940000 kPa", "stress", 206940.0),
        ("0.288 ", "ratio", 0.288),
        ("1.128e-05 1/\xb0C", "expansion", 1.128e-05),
        ("434000000 \xb5J/(kg\xb7K)", "specific_heat", 434.0),
        ("0.283 lbm/in\xb3", "density", 0.283 * 27679.9047),
        (
            "Tabular Data: \n t  K \n \xb0F  W/(m\xb7K) \n 0  1 \n 70  2 \n 200  3 ",
            "conductivity",
            2.0,
        ),
        ("12 furlongs", "stress", None),
        ("", "density", None),
    ],
)
def test_parse_property_units(text, dim, wanted):
    got = parse_property(text, dim)
    assert got == pytest.approx(wanted) if wanted is not None else got is None


def test_list_materials_filters_pages_and_converts():
    nx = library_nx()
    listed = run(nx, "nx_list_materials", query="metal", limit=1)
    assert listed.ok and listed.mutation_outcome == "not_applicable", listed
    assert listed.page["total"] == 2 and listed.page["next_offset"] == 1
    steel = listed.data["items"][0]
    assert steel["name"] == "Steel" and steel["category"] == "METAL"
    assert steel["density"] == pytest.approx(7829.0)
    assert steel["youngs_modulus"] == pytest.approx(206940.0)
    assert steel["thermal_conductivity"] == pytest.approx(55.7)
    air = run(nx, "nx_list_materials", query="air").data["items"][0]
    assert air["density"] is None and air["thermal_conductivity"] == pytest.approx(0.0263)
    other = run(nx, "nx_list_materials", library="site_lib.xml")
    assert other.error["category"] == "invalid_argument"


def test_empty_library_is_unavailable():
    nx = library_nx(names={})
    empty = run(nx, "nx_list_materials")
    assert empty.error["code"] == "NX_CATALOG_UNAVAILABLE"
    nx = library_nx()

    def unreadable(library):
        raise RuntimeError("Material Library format error; unable to read.")

    nx.materials.GetMaterialsFromLibrary = unreadable
    block = nx.block(10, 10, 10)
    gone = run(nx, "nx_set_material", bodies=[block.ref], material={"library": "Steel"})
    assert gone.error["code"] == "NX_CATALOG_UNAVAILABLE" and nx.loads == []


def test_assign_library_material_reads_back_and_reuses_load():
    nx = library_nx()
    one, two = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(50, 0, 0))
    ok = run(nx, "nx_set_material", bodies=[one.ref], material={"library": "steel"})
    assert ok.ok and ok.mutation_outcome == "committed", ok
    row = ok.data["items"][0]
    assert row["material"] == "Steel" and row["library"] == "physicalmateriallibrary.xml"
    assert row["density"] == pytest.approx(7829.0) and row["poissons_ratio"] == 0.288
    every = run(nx, "nx_set_material", material={"library": "Aluminum_6061"})
    assert every.ok and len(every.data["items"]) == 2, every
    again = run(nx, "nx_set_material", bodies=[two.ref], material={"library": "Steel"})
    assert again.ok and nx.loads == ["Steel", "Aluminum_6061"]
    read = run(nx, "nx_get_material")
    rows = read.data["items"]
    assert [r["material"] for r in rows] == ["Aluminum_6061", "Steel"]
    assert rows[0]["youngs_modulus"] == pytest.approx(68980.0) and rows[0]["category"] == "METAL"
    assert rows[1]["specific_heat"] == pytest.approx(434.0)


def test_unknown_library_material_and_custom_clash_are_refused():
    nx = library_nx()
    block = nx.block(10, 10, 10)
    unknown = run(nx, "nx_set_material", bodies=[block.ref], material={"library": "Steal"})
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.error["field"] == "/material/library"
    assert "Steel" in unknown.error["details"]["candidates"] and nx.loads == []
    nx.materials.append(nx.Material("ABS", 1000.0))
    clash = run(nx, "nx_set_material", bodies=[block.ref], material={"library": "ABS"})
    assert clash.error["code"] == "NX_ALREADY_EXISTS"
    both = run(nx, "nx_set_material", bodies=[block.ref],
               material={"library": "ABS", "custom": {"name": "X", "density": 1}})  # fmt: skip
    assert both.error["category"] == "invalid_argument"
    path = run(nx, "nx_set_material", bodies=[block.ref],
               material={"library": "Steel", "file": "lib.xml"})  # fmt: skip
    assert path.error["category"] == "invalid_argument"


def test_custom_material_reports_no_library_properties():
    nx = library_nx()
    block = nx.block(10, 10, 10)
    nx.on_commit(
        "PhysicalMaterialBuilder",
        lambda b: nx.Material(
            b.Name, b.PropertyTable.SetBaseScalarWithDataPropertyValue.calls[0][0][1]
        ),
    )
    ok = run(nx, "nx_set_material", material={"custom": {"name": "AL", "density": 2700}})
    assert ok.ok, ok
    row = ok.data["items"][0]
    assert row["library"] is None and row["youngs_modulus"] is None and row["density"] == 2700
    assert block.ref


# ---------------------------------------------------------------------------
# Live tester batch b30: natural material values and clearer refusals


def test_bare_and_loose_library_names_resolve():
    nx = library_nx()
    one, two = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(50, 0, 0))
    bare = run(nx, "nx_set_material", bodies=[one.ref], material="steel")
    assert bare.ok and bare.data["items"][0]["material"] == "Steel", bare
    assert bare.data["items"][0]["density"] == pytest.approx(7829.0)
    loose = run(nx, "nx_set_material", bodies=[two.ref], material={"library": "aluminium 6061"})
    assert loose.ok and loose.data["items"][0]["material"] == "Aluminum_6061", loose
    vague = run(nx, "nx_set_material", bodies=[two.ref], material="Aluminium")
    assert vague.error["code"] == "NX_NOT_FOUND" and vague.error["field"] == "/material", vague
    assert vague.error["details"]["candidates"][0] == "Aluminum_6061"
    assert "did you mean Aluminum_6061" in vague.error["message"]
    assert run(nx, "nx_get_material", bodies=[two.ref]).data["items"][0]["material"] == (
        "Aluminum_6061"
    )


def test_material_forms_are_named_when_ambiguous_or_empty():
    nx = library_nx()
    block = nx.block(10, 10, 10)
    for material, text in (
        ({"library": "Steel", "custom": {"name": "PLA", "density": 1240}}, "not both"),
        ({}, "library or custom is required"),
    ):
        bad = run(nx, "nx_set_material", bodies=[block.ref], material=material)
        assert bad.error["code"] == "NX_INVALID_ARGUMENT", bad
        assert bad.error["field"] == "/material" and text in bad.error["message"]
        assert "{custom: {name: 'PLA', density: 1240}}" in bad.error["hint"]
        assert bad.mutation_outcome == "not_started" and nx.loads == []
    wrong = run(nx, "nx_set_material", bodies=[block.ref], material={"custom": {"name": "X"}})
    assert wrong.error["field"] == "/material/custom/density", wrong


@pytest.mark.parametrize(
    ("density", "kg_m3"),
    [("1.24 g/cm3", 1240.0), ("1240 kg/m^3", 1240.0), ("1.24g/cm\xb3", 1240.0),
     ("1240", 1240.0), ("0.0448 lb/in3", 0.0448 * 27679.9047), (1240, 1240.0)],
)  # fmt: skip
def test_custom_density_accepts_unit_strings(density, kg_m3):
    nx = library_nx()
    nx.block(10, 10, 10)
    nx.on_commit(
        "PhysicalMaterialBuilder",
        lambda b: nx.Material(
            b.Name, b.PropertyTable.SetBaseScalarWithDataPropertyValue.calls[0][0][1]
        ),
    )
    ok = run(nx, "nx_set_material", material={"custom": {"name": "PLA", "density": density}})
    assert ok.ok and ok.data["items"][0]["density"] == pytest.approx(kg_m3), ok
    stored = nx.last("PhysicalMaterialBuilder").PropertyTable.SetBaseScalarWithDataPropertyValue
    assert stored.calls[0][0][1] == pytest.approx(kg_m3)


@pytest.mark.parametrize("density", ["1.2 furlongs", "heavy", "0 g/cm3", "2000 g/cm3"])
def test_unreadable_custom_density_is_refused_before_nx(density):
    nx = library_nx()
    nx.block(10, 10, 10)
    bad = run(nx, "nx_set_material", material={"custom": {"name": "PLA", "density": density}})
    assert bad.error["code"] == "NX_INVALID_ARGUMENT", bad
    assert bad.error["field"] == "/material/custom/density"
    assert bad.mutation_outcome == "not_started"
    assert not any(b.kind == "PhysicalMaterialBuilder" for b in nx.builders)
    if "furlongs" in density:
        assert "g/cm3" in bad.error["hint"] and "lb/in3" in bad.error["hint"]


def test_custom_with_an_existing_name_updates_its_density_and_bare_name_reuses_it():
    nx = library_nx()
    one, two = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(50, 0, 0))
    kg = NS(Tag=1)
    nx.part.UnitCollection = NS(FindObject=lambda name: kg)

    def committed(b):
        made = nx.Material(
            b.Name, b.PropertyTable.SetBaseScalarWithDataPropertyValue.calls[0][0][1]
        )

        def set_density(name, value, unit):
            made.density = value  # NX applies it to bodies on their next assign

        table = NS(
            GetBaseScalarWithDataPropertyValue=lambda name: (made.density, kg),
            SetBaseScalarWithDataPropertyValue=set_density,
        )
        made.GetPropTable = lambda: table
        made.GetMaterialEditable = lambda: True
        nx.materials.append(made)
        return made

    nx.on_commit("PhysicalMaterialBuilder", committed)
    made = run(nx, "nx_set_material", bodies=[one.ref],
               material={"custom": {"name": "PLA", "density": 1240}})  # fmt: skip
    assert made.ok, made
    # The same name with another density edits the material: both bodies follow.
    edited = run(nx, "nx_set_material", bodies=[two.ref],
                 material={"custom": {"name": "pla", "density": 1300}})  # fmt: skip
    assert edited.ok, edited
    assert edited.data["items"][0]["density"] == pytest.approx(1300.0)
    assert nx.densities[int(one.Tag)] == pytest.approx(1300.0)  # the other user too
    (note,) = edited.warnings
    assert "was 1240" in note["message"] and "1 other bodies" in note["message"]
    assert len([b for b in nx.builders if b.kind == "PhysicalMaterialBuilder"]) == 1
    reuse = run(nx, "nx_set_material", bodies=[two.ref], material="PLA")
    assert reuse.ok and reuse.data["items"][0]["material"] == "PLA", reuse
    assert reuse.data["items"][0]["density"] == pytest.approx(1300.0)
    assert reuse.data["items"][0]["library"] is None
    # A library material's name is not a custom material to edit.
    lib = run(nx, "nx_set_material", bodies=[one.ref], material="ABS")
    assert lib.ok, lib
    clash = run(nx, "nx_set_material", bodies=[two.ref],
                material={"custom": {"name": "ABS", "density": 1000}})  # fmt: skip
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.mutation_outcome == "not_started"


def test_list_query_ranks_names_and_ignores_spelling():
    names = {
        "Stainless_Steel": {"Category": "METAL"},
        "Steel_Rolled": {"Category": "METAL"},
        "Steel": LIBRARY["Steel"],
        "Aluminum_6061": LIBRARY["Aluminum_6061"],
        "ABS": LIBRARY["ABS"],
    }
    nx = library_nx(names)
    steel = run(nx, "nx_list_materials", query="steel")
    assert [r["name"] for r in steel.data["items"]] == ["Steel", "Steel_Rolled", "Stainless_Steel"]
    assert not steel.warnings
    british = run(nx, "nx_list_materials", query="Aluminium 6061")
    assert [r["name"] for r in british.data["items"]] == ["Aluminum_6061"], british
    plastic = run(nx, "nx_list_materials", query="abs plastic")
    assert [r["name"] for r in plastic.data["items"]] == ["ABS"], plastic
    none = run(nx, "nx_list_materials", query="steal")
    assert none.ok and none.data["items"] == [] and none.warnings, none
    assert "close names: Steel" in none.warnings[0]["message"]


def test_get_material_notes_default_density_and_empty_assembly():
    nx = library_nx()
    nx.block(10, 10, 10)
    bare = run(nx, "nx_get_material")
    assert bare.ok and bare.data["items"][0]["material"] is None, bare
    assert "1 of 1 bodies have no material" in bare.warnings[0]["message"]
    empty = run(nx, "nx_get_material", scope="assembly")
    assert empty.ok and empty.data["items"] == [], empty
    assert "no components" in empty.warnings[0]["message"]
    run(nx, "nx_set_material", material="Steel")
    assert not run(nx, "nx_get_material").warnings
