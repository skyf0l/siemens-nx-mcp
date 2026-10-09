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
