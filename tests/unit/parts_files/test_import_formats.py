"""nx_import_model beyond STEP: IGES, Parasolid, STL, DXF/DWG (work part and sketch), CATIA V5."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.parts_files import importing
from tests.kit import NXException, neutral_files
from tests.kit.objects import Body, Object, point
from tests.unit.parts_files.support import call

BOX = [0.0, 0.0, 0.0, 40.0, 20.0, 10.0]
RECT = [((0, 0), (40, 0)), ((40, 0), (40, 20)), ((40, 20), (0, 20)), ((0, 20), (0, 0))]


def r12(lines=RECT, units=4, z=0.0) -> bytes:
    rows = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS",
            "70", str(units), "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]  # fmt: skip
    for (x0, y0), (x1, y1) in lines:
        rows += ["0", "LINE", "8", "0", "10", str(x0), "20", str(y0), "30", str(z),
                 "11", str(x1), "21", str(y1), "31", str(z)]  # fmt: skip
    rows += ["0", "ENDSEC", "0", "EOF"]
    return ("\r\n".join(rows) + "\r\n").encode()


class Line(Object):
    def __init__(self, a, b):
        super().__init__("Line")
        self.StartPoint, self.EndPoint = point(*a), point(*b)

    @property
    def box(self):
        p, q = self.StartPoint, self.EndPoint
        return [min(p.X, q.X), min(p.Y, q.Y), min(p.Z, q.Z),
                max(p.X, q.X), max(p.Y, q.Y), max(p.Z, q.Z)]  # fmt: skip


class Facets(Object):
    def __init__(self, box):
        super().__init__("FACETED")
        self.box, self.AssociatedBody = box, None


class Importer(NS):
    """Any NX import builder: records its settings; Commit runs the format's behaviour."""

    def __init__(self, nx, kind, commit):
        super().__init__(ObjectTypes=NS(Solids=False, Surfaces=False, Curves=False))
        self.kind, self._commit, self.destroyed = kind, commit, False
        nx.importers.append(self)

    def Commit(self):
        self._commit(self)

    def Destroy(self):
        self.destroyed = True


@pytest.fixture
def fx(nx, root, ugii):
    """Fake IGES/Parasolid/STL/DXF/CATIA importers on the work part, with bounds and UF
    transforms for the objects they create."""
    nx.importers, boxes = [], {}
    nx.iges_box, nx.solid_box, nx.catia_error = list(BOX), list(BOX), None
    nx.nx.IgesImporter = NS(ImportToEnum=NS(NewPart=0, WorkPart=1))
    nx.nx.STLImporter = NS(FileFormatType=NS(Ascii=0, Binary=1),
                           FileUnitsType=NS(Inches=0, Meters=1, Millimeters=2))  # fmt: skip
    nx.nx.DxfdwgImporter = NS(ImportToEnum=NS(New=0, Work=1), WorkflowAs=NS(Drawing=0, ThreeD=1),
                              SendModelDataAs=NS(Modeling=2), UnitsEnum=NS(English=0, Metric=1))  # fmt: skip
    nx.nx.Catiav5Importer = NS(ImportToOption=NS(NewPart=0, WorkPart=1))

    def work():
        part = nx.session.Parts.Work
        part.IsModified = True
        if not hasattr(part, "FacetedBodies"):
            part.FacetedBodies = []
        return part

    def add_body(box, solid=True):
        body = Body("IMPORTED", list(box))
        body.IsSolidBody, body.volume = (
            solid,
            (box[3] - box[0]) * (box[4] - box[1]) * (box[5] - box[2]),
        )
        body.OwningPart = work()
        work().Bodies.append(body)

    def iges(b):
        if b.SewSurfaces:
            add_body(nx.iges_box)
        else:
            for _ in range(6):
                add_body(nx.iges_box, solid=False)

    def stl(b):
        data = Path(b.FileName).read_bytes()
        facts = importing.neutral.stl_facts(data)
        if facts["binary"] != (b.FileFormat == 1):
            raise NXException(3550018, "Parsing error in file")
        k = {0: 25.4, 1: 1000.0, 2: 1.0}[b.FileUnits] * nx.stl_scale
        facet = Facets([v * k for v in facts["min"] + facts["max"]])
        work().FacetedBodies.append(facet)
        boxes[facet.Tag] = facet

    def dxf(b):
        for a, c in nx.dxf_lines:
            line = Line((*a, nx.dxf_z), (*c, nx.dxf_z))
            line.OwningPart = work()
            work().Curves.append(line)
            boxes[line.Tag] = line

    def catia(b):
        if nx.catia_error:
            raise nx.catia_error
        add_body(nx.solid_box)

    nx.dxf_lines, nx.dxf_z = RECT, 0.0
    nx.stl_scale = 1.0
    dex = nx.session.DexManager
    dex.CreateIgesImporter = lambda: Importer(nx, "iges", iges)
    dex.CreateDxfdwgImporter = lambda: Importer(nx, "dxf", dxf)
    dex.CreateCatiav5Importer = lambda: Importer(nx, "catia", catia)
    manager = NS(CreateParasolidImporter=lambda: Importer(nx, "x_t", lambda b: add_body(nx.solid_box)),
                 CreateStlImporter=lambda: Importer(nx, "stl", stl))  # fmt: skip
    original = nx.uf.ModlGeneral.AskBoundingBox
    nx.uf.ModlGeneral.AskBoundingBox = lambda tag: (
        list(boxes[tag].box) if tag in boxes else original(tag)
    )

    def mapping(dest, ref):  # NX: maps ``ref`` onto ``dest`` (dest = sketch frame here)
        o, x, y = dest[:3], dest[3:6], dest[6:9]
        n = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
        rows = [[x[i], y[i], n[i], o[i]] for i in range(3)]
        return [v for row in rows for v in row], 0

    def transform(count, tags, m):
        assert len(m) == 16 and count == len(tags)
        for tag in tags:
            line = boxes[tag]
            for name in ("StartPoint", "EndPoint"):
                p = getattr(line, name)
                v = [p.X, p.Y, p.Z, 1.0]
                setattr(line, name, point(*[sum(m[4 * r + c] * v[c] for c in range(4))
                                            for r in range(3)]))  # fmt: skip

    nx.uf.Trns = NS(CreateCsysMappingMatrix=mapping)
    nx.uf.Modl = NS(TransformEntities=transform)
    nx.manager = manager
    part = nx.new_part("w.prt")
    part.ImportManager = manager
    part.FacetedBodies = []
    _patch_new_parts(nx, manager)
    return nx


def _patch_new_parts(nx, manager):
    make = nx.session.Parts.FileNew

    def file_new():
        builder = make()
        commit = builder.Commit

        def committed():
            part = commit()
            part.ImportManager = manager  # FileNew no longer makes it the work part
            return part

        builder.Commit = committed
        return builder

    nx.session.Parts.FileNew = file_new


def put(root, name, data: bytes) -> str:
    (root / name).write_bytes(data)
    return name


def test_iges_sews_into_one_solid_and_reads_back_against_the_file(fx, root, ugii):
    put(root, "a.igs", neutral_files.iges([BOX]))
    ok = call(
        fx,
        importing.nx_import_model,
        part="w",
        path="a.igs",
        format="iges",
        operation_id="op_import_i1",
    )
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = fx.importers[-1]
    assert b.SewSurfaces is True and b.ImportTo == fx.nx.IgesImporter.ImportToEnum.WorkPart
    assert b.SettingsFile.endswith("igesimport.def") and b.FileOpenFlag is False
    assert b.InputFile == str(root / "imports" / "op_import_i1" / "input.igs") and b.destroyed
    assert b.OutputFile == str(root / "imports" / "op_import_i1" / "output.prt")
    d = ok.data
    assert (d["solids"], d["sheets"], d["body_count"], d["volume"]) == (1, 0, 1, 8000)
    assert d["bounds"] == {"min": BOX[:3], "max": BOX[3:]} and d["file_units"] == "mm"
    sheets = call(fx, importing.nx_import_model, part="w", path="a.igs", format="iges",
                  options={"sew": False})  # fmt: skip
    assert sheets.ok and (sheets.data["sheets"], sheets.data["volume"]) == (6, None), sheets


def test_iges_bodies_outside_the_file_extent_roll_back(fx, root, ugii):
    put(root, "a.igs", neutral_files.iges([BOX]))
    fx.iges_box = [0, 0, 0, 80, 20, 10]
    bad = call(
        fx,
        importing.nx_import_model,
        part="w",
        path="a.igs",
        format="iges",
        operation_id="op_import_i2",
    )
    assert (
        bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["details"]["check"] == "extent"
    )
    assert (
        bad.mutation_outcome == "rolled_back" and not (root / "imports" / "op_import_i2").exists()
    )


def test_parasolid_text_and_binary_and_encoding_mismatch(fx, root):
    put(root, "a.x_t", neutral_files.parasolid([BOX]))
    put(root, "a.x_b", neutral_files.parasolid([BOX], binary=True))
    put(root, "wrong.x_b", neutral_files.parasolid([BOX]))
    for name in ("a.x_t", "a.x_b"):
        ok = call(
            fx,
            importing.nx_import_model,
            part="w",
            path=name,
            format="parasolid",
            operation_id=f"op_import_{name[-1]}",
        )
        assert ok.ok and ok.data["solids"] == 1 and ok.data["volume"] == 8000, ok
        assert fx.importers[-1].FileName.endswith("input" + name[-4:])
        assert ok.data["file_units"] == "m"
    count = len(fx.importers)
    bad = call(fx, importing.nx_import_model, part="w", path="wrong.x_b", format="parasolid")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/path"
    assert bad.mutation_outcome == "not_started" and len(fx.importers) == count


def test_stl_makes_facet_bodies_in_the_file_units(fx, root):
    put(root, "a.stl", neutral_files.stl([BOX]))
    put(root, "t.stl", neutral_files.stl([BOX], binary=False))
    ok = call(fx, importing.nx_import_model, part="w", path="a.stl", format="stl")
    assert ok.ok and ok.data["facet_bodies"] == 1 and ok.data["body_count"] == 0, ok
    b = fx.importers[-1]
    assert b.FileFormat == 1 and b.FileUnits == 2 and b.DisplayInformation is False
    assert ok.data["volume"] is None and ok.data["bounds"]["max"] == BOX[3:]
    text = call(
        fx, importing.nx_import_model, part="w", path="t.stl", format="stl", options={"units": "in"}
    )
    assert text.ok and fx.importers[-1].FileFormat == 0 and fx.importers[-1].FileUnits == 0
    assert text.data["bounds"]["max"] == pytest.approx([v * 25.4 for v in BOX[3:]])
    assert text.data["file_units"] == "in"


def test_stl_extent_mismatch_is_caught(fx, root):
    put(root, "a.stl", neutral_files.stl([BOX]))
    fx.stl_scale = 2.0  # NX read the numbers in other units than asked
    bad = call(fx, importing.nx_import_model, part="w", path="a.stl", format="stl")
    assert bad.error["details"]["check"] == "extent" and bad.mutation_outcome == "rolled_back"


def test_dxf_curves_into_the_work_part(fx, root, ugii):
    put(root, "p.dxf", r12())
    ok = call(fx, importing.nx_import_model, part="w", path="p.dxf", format="dxf_dwg")
    assert ok.ok and ok.data["curve_count"] == 4 and len(ok.data["curves"]) == 4, ok
    b = fx.importers[-1]
    assert b.Units == fx.nx.DxfdwgImporter.UnitsEnum.Metric and b.ConvModelData is True
    assert b.SendModelDataTo == fx.nx.DxfdwgImporter.SendModelDataAs.Modeling
    assert b.ImportTo == fx.nx.DxfdwgImporter.ImportToEnum.Work
    assert ok.data["bounds"] == {"min": [0, 0, 0], "max": [40, 20, 0]}
    assert ok.data["sketch"] is None and ok.data["file_units"] == "mm"


def test_dxf_into_a_sketch_maps_file_xy_to_sketch_xy(fx, root, ugii):
    put(root, "p.dxf", r12())
    sk = fx.sketch(origin=(0, 0, 50), x_axis=(1, 0, 0), y_axis=(0, 0, 1))
    sk.OwningPart = fx.session.Parts.Work
    ok = call(fx, importing.nx_import_model, path="p.dxf", format="dxf_dwg", target="sketch",
              sketch=sk.ref)  # fmt: skip
    assert ok.ok, ok
    assert len(sk.geometry) == 4 and fx.session.ActiveSketch is None
    assert ok.data["sketch"]["id"] and ok.data["target"] == "sketch"
    assert ok.data["bounds"] == {"min": [0, 0, 50], "max": [40, 0, 70]}


def test_sketch_target_rules(fx, root, ugii):
    put(root, "p.dxf", r12())
    put(root, "a.stl", neutral_files.stl([BOX]))
    sk = fx.sketch()
    sk.OwningPart = fx.session.Parts.Work
    missing = call(
        fx, importing.nx_import_model, part="w", path="p.dxf", format="dxf_dwg", target="sketch"
    )
    assert missing.error["field"] == "/sketch" and missing.mutation_outcome == "not_started"
    wrong = call(fx, importing.nx_import_model, path="a.stl", format="stl", target="sketch",
                 sketch=sk.ref)  # fmt: skip
    assert wrong.error["field"] == "/target"
    put(root, "z.dxf", r12(z=5.0))
    fx.dxf_z = 5.0
    bad = call(fx, importing.nx_import_model, path="z.dxf", format="dxf_dwg", target="sketch",
               sketch=sk.ref)  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/target"
    assert bad.mutation_outcome == "rolled_back" and not sk.geometry


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("m.dxf", r12().replace(b"AC1009", b"AC1027"), "handles"),
        ("b.dxf", b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 40, "binary"),
        ("cut.dxf", r12()[:-12], "EOF"),
        ("empty.dxf", r12(lines=[]), "ENTITIES"),
        ("x.dwg", b"NOTDWG" + b"\x00" * 2000, "AC1xxx"),
    ],
)
def test_unreadable_drawings_are_refused_before_nx(fx, root, ugii, name, data, message):
    put(root, name, data)
    bad = call(fx, importing.nx_import_model, part="w", path=name, format="dxf_dwg")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/path"
    assert message in bad.error["message"] and bad.mutation_outcome == "not_started"
    assert not fx.importers


def _dwg2004(size: int, last_end: int, page_map: int) -> bytes:
    plain = bytearray(0x6C)
    plain[:12] = b"AcFssFcAJMB\x00"
    struct.pack_into("<q", plain, 0x2C, last_end)
    struct.pack_into("<q", plain, 0x54, page_map - 0x100)
    seed, coded = 1, bytearray()
    for byte in plain:
        seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        coded.append(byte ^ ((seed >> 16) & 0xFF))
    data = bytearray(b"AC1032" + b"\x00" * (size - 6))
    data[0x80 : 0x80 + 0x6C] = coded
    return bytes(data)


def _dwg2000(size: int, seeker: int, length: int) -> bytes:
    data = bytearray(b"AC1015" + b"\x00" * (size - 6))
    struct.pack_into("<i", data, 0x15, 1)
    struct.pack_into("<Bii", data, 0x19, 0, seeker, length)
    return bytes(data)


def test_dwg_facts_find_truncated_files():
    assert importing.dwg_facts(_dwg2004(4096, 4096, 3000))["version"] == "AC1032"
    with pytest.raises(ValueError, match="truncated"):
        importing.dwg_facts(_dwg2004(2048, 4096, 3000))
    assert importing.dwg_facts(_dwg2000(4096, 100, 3000))["version"] == "AC1015"
    with pytest.raises(ValueError, match="truncated"):
        importing.dwg_facts(_dwg2000(1024, 100, 3000))
    with pytest.raises(ValueError, match="R2004"):
        importing.dwg_facts(b"AC1032" + b"\x00" * 4000)


def test_dwg_is_imported_after_its_header_check(fx, root, ugii):
    put(root, "p.dwg", _dwg2004(4096, 4096, 3000))
    ok = call(fx, importing.nx_import_model, part="w", path="p.dwg", format="dxf_dwg",
              options={"units": "in"})  # fmt: skip
    assert ok.ok and ok.data["curve_count"] == 4 and ok.data["file_units"] == "in", ok
    assert fx.importers[-1].Units == fx.nx.DxfdwgImporter.UnitsEnum.English


def test_dxf_facts_extent_of_arcs_and_circles():
    rows = ["0", "SECTION", "2", "ENTITIES",
            "0", "CIRCLE", "10", "0", "20", "0", "30", "0", "40", "5",
            "0", "ARC", "10", "100", "20", "0", "30", "0", "40", "10", "50", "0", "51", "90",
            "0", "ENDSEC", "0", "EOF"]  # fmt: skip
    facts = importing.dxf_facts("\n".join(rows).encode())
    assert facts["version"] == "AC1009" and facts["curves"] == 2
    assert facts["min"] == pytest.approx([-5, -5, 0]) and facts["max"] == pytest.approx(
        [110, 10, 0]
    )
    rows[rows.index("ENDSEC")] = "LWPOLYLINE"
    rows.insert(rows.index("LWPOLYLINE") + 1, "ENDSEC")
    rows.insert(rows.index("LWPOLYLINE") + 1, "0")
    assert importing.dxf_facts("\n".join(rows).encode())["min"] is None  # bulges: not exact


def test_catia_v5_license_and_header(fx, root, ugii, monkeypatch):
    from nx_mcp.core import evidence

    # Session memory applies to tested variants only; catia_v5 has no passing evidence.
    monkeypatch.setattr(evidence, "available", lambda *a, **k: True)
    put(root, "a.CATPart", b"V5_CFV2" + b"\x00" * 64)
    put(root, "fake.CATPart", b"not catia")
    fx.catia_error = NXException(3755014, "There are no licenses available for the NX_CATIAV5 "
                                 "translator.")  # fmt: skip
    bad = call(fx, importing.nx_import_model, part="w", path="a.CATPart", format="catia_v5")
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 3755014
    assert bad.error["details"]["translator"] == "Catiav5Importer"
    assert fx.importers[-1].SettingsFile.endswith("catiav5.def") and fx.importers[-1].destroyed
    count = len(fx.importers)
    again = call(fx, importing.nx_import_model, part="w", path="a.CATPart", format="catia_v5")
    assert again.error["code"] == "NX_LICENSE_UNAVAILABLE" and len(fx.importers) == count
    assert again.error["details"]["remembered_from"]  # refused before NX for this session


def test_catia_v5_imports_a_v5_document_and_refuses_others(fx, root, ugii):
    put(root, "a.CATPart", b"V5_CFV2" + b"\x00" * 64)
    put(root, "fake.CATPart", b"not catia")
    ok = call(fx, importing.nx_import_model, part="w", path="a.CATPart", format="catia_v5")
    assert ok.ok and ok.data["solids"] == 1, ok
    assert fx.importers[-1].ImportTo == fx.nx.Catiav5Importer.ImportToOption.WorkPart
    fake = call(fx, importing.nx_import_model, part="w", path="fake.CATPart", format="catia_v5")
    assert fake.error["field"] == "/path" and fake.mutation_outcome == "not_started"


def test_argument_rules(fx, root, ugii):
    put(root, "a.igs", neutral_files.iges([BOX]))
    put(root, "a.stl", neutral_files.stl([BOX]))
    flat = call(fx, importing.nx_import_model, part="w", path="a.igs", format="iges", flatten=False)
    assert flat.error["field"] == "/flatten" and flat.mutation_outcome == "not_started"
    suffix = call(fx, importing.nx_import_model, part="w", path="a.igs", format="stl")
    assert suffix.error["code"] == "NX_UNSUPPORTED_FILE_TYPE" and suffix.error["field"] == "/path"
    opts = call(
        fx, importing.nx_import_model, part="w", path="a.stl", format="stl", options={"sew": True}
    )
    assert opts.error["field"] == "/options/sew"
    assert not fx.importers


def test_stl_into_a_new_part(fx, root, ugii):
    put(root, "a.stl", neutral_files.stl([BOX]))
    ok = call(fx, importing.nx_import_model, part="w", path="a.stl", format="stl", target="new_part",
              new_part_path="imp/mesh.prt")  # fmt: skip
    assert ok.ok and ok.data["target"] == "new_part" and ok.data["facet_bodies"] == 1, ok
    assert Path(fx.session.Parts.Work.FullPath) == root / "imp" / "mesh.prt"


def test_import_sources_can_be_uploaded():
    from nx_mcp.domains.parts_files import workspace

    assert set(importing.ALL_SUFFIXES) <= set(workspace.UPLOAD_SUFFIXES)


def test_format_defaults_to_the_path_suffix(fx, root, ugii):
    put(root, "a.igs", neutral_files.iges([BOX]))
    put(root, "p.dxf", r12())
    ok = call(fx, importing.nx_import_model, part="w", path="a.igs")
    assert ok.ok and ok.data["format"] == "iges" and fx.importers[-1].kind == "iges", ok
    dxf = call(fx, importing.nx_import_model, part="w", path="p.dxf", options={"units": "mm"})
    assert dxf.ok and dxf.data["format"] == "dxf_dwg" and dxf.data["curve_count"] == 4, dxf
    put(root, "a.CATPart", b"V5_CFV2" + bytes(64))
    count = len(fx.importers)
    catia = call(fx, importing.nx_import_model, part="w", path="a.CATPart")
    assert catia.error["field"] == "/format" and "catia_v5" in catia.error["message"]
    assert catia.mutation_outcome == "not_started" and len(fx.importers) == count


def test_options_name_the_formats_they_apply_to(fx, root, ugii):
    from nx_mcp.sidecar import schema

    spec = importing.nx_import_model.spec
    listed = str(schema.input_schema(spec, call_keys=False)["properties"]["options"])
    assert "sew" in listed and "units" in listed
    schema.adapter(spec).validate_python({"path": "a.stl"})  # format may be omitted
    put(root, "a.igs", neutral_files.iges([BOX]))
    put(root, "p.dxf", r12())
    units = call(fx, importing.nx_import_model, part="w", path="a.igs", options={"units": "mm"})
    assert units.error["field"] == "/options/units" and "stl, dxf_dwg" in units.error["message"]
    metres = call(fx, importing.nx_import_model, part="w", path="p.dxf", options={"units": "m"})
    assert metres.error["field"] == "/options/units" and metres.mutation_outcome == "not_started"
    assert not fx.importers


def test_iges_solid_bounds_are_exact_although_the_quick_box_is_padded(fx, root, ugii):
    """NX 2406: a sewn IGES plate's quick box was 0.125 mm larger than the file extent."""
    put(root, "a.igs", neutral_files.iges([BOX]))
    quick = fx.uf.ModlGeneral.AskBoundingBox

    def padded(tag):
        box = list(quick(tag))
        return [v - 0.125 for v in box[:3]] + [v + 0.125 for v in box[3:]]

    fx.uf.ModlGeneral.AskBoundingBox = padded
    ok = call(fx, importing.nx_import_model, part="w", path="a.igs")
    assert ok.ok and ok.data["solids"] == 1, ok
    assert ok.data["bounds"] == {"min": BOX[:3], "max": BOX[3:]}
