"""nx_export_model 3D DXF/DWG (DxfdwgCreator, model data) and the DXF model reader."""

import pytest

from nx_mcp.core import evidence
from nx_mcp.domains.parts_files import exchange, neutral
from tests.kit import parts as fake_parts
from tests.kit.objects import Body
from tests.unit.parts_files.support import call

NS = fake_parts.NS
ENUMS = NS(
    ExportDataOption=NS(Modeling=0, Drawing=1),
    ExportAsOption=NS(TwoD=0, ThreeD=1, Cgm=2),
    ExportFromOption=NS(DisplayPart=0, ExistingPart=1),
    OutputFileTypeOption=NS(Dxf=0, Dwg=1),
)


def _pairs(*items) -> str:
    return "".join(f"{code}\n{value}\n" for code, value in items)


def _faces(box):
    x0, y0, z0, x1, y1, z1 = box
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)]
    tops = [(x, y, z1) for x, y, _ in corners]
    out = []
    for quad in (corners, tops):
        items = [(0, "3DFACE"), (5, "A1"), (8, "1")]
        for i, (x, y, z) in enumerate(quad):
            items += [(10 + i, x), (20 + i, y), (30 + i, z)]
        out.append(_pairs(*items))
    return out


def sample_dxf(boxes, units=4, offset=(0.0, 0.0, 0.0)) -> bytes:
    """An NX-like 3D DXF: the faces in a block, placed by one INSERT."""
    header = _pairs((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1018"),
                    (9, "$INSUNITS"), (70, units), (0, "ENDSEC"))  # fmt: skip
    block = _pairs((0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (5, "20"), (2, "UG_SURFACE_001"),
                   (10, 0.0), (20, 0.0), (30, 0.0))  # fmt: skip
    for box in boxes:
        block += "".join(_faces(box))
    block += _pairs((0, "ENDBLK"), (5, "21"), (0, "ENDSEC"))
    entities = _pairs((0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (5, "69"),
                      (2, "UG_SURFACE_001"), (10, offset[0]), (20, offset[1]), (30, offset[2]),
                      (0, "ENDSEC"), (0, "SECTION"), (2, "OBJECTS"), (0, "ENDSEC"),
                      (0, "EOF"))  # fmt: skip
    return (header + block + entities).encode()


class DxfdwgCreator(fake_parts.Translator):
    def __init__(self, fake):
        super().__init__(
            fake, SettingsFile=None, ExportData=None, ExportAs=None, ExportFrom=None,
            OutputFileType=None, InputFile=None, LayerMask=None, OutputFile=None,
            FileSaveFlag=True, ProcessHoldFlag=False,
            ObjectTypes=NS(Solids=False, Surfaces=False, Curves=True),
        )  # fmt: skip
        self.ExportSelectionBlock = self._selector()

    def _file(self, out, boxes, units):
        if self.OutputFileType == ENUMS.OutputFileTypeOption.Dwg:
            return out, b"AC1018" + bytes(400)
        return out, sample_dxf(boxes, 4 if units == "mm" else 1)


@pytest.fixture
def dxf(nx, ugii):
    nx.nx.DxfdwgCreator = ENUMS
    nx.session.DexManager.CreateDxfdwgCreator = lambda: DxfdwgCreator(nx)
    nx.licensed = {"ug_to_dxf": True}
    nx.session.LicenseManager = NS(CheckPresence=lambda f: nx.licensed.get(f, False))
    yield nx
    evidence.forget_licenses()


def modified_part(nx, units="mm"):
    part = nx.new_part("m.prt", units)
    part.Save()
    for box in ([0, 0, 0, 40, 20, 10], [200, 0, 0, 210, 20, 30]):
        body = Body("B", box=list(box))
        body.OwningPart = part
        part.Bodies.append(body)
    part.IsModified = True
    return part


def test_dxf_3d_from_memory(dxf, root, ugii):
    part = modified_part(dxf)
    saved = (root / "m.prt").read_bytes()
    ok = call(dxf, exchange.nx_export_model, part="m", path="out/m.dxf")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    creator = dxf.exporters[-1]
    assert creator.ExportData == ENUMS.ExportDataOption.Modeling
    assert creator.ExportAs == ENUMS.ExportAsOption.ThreeD
    assert creator.ExportFrom == ENUMS.ExportFromOption.DisplayPart
    assert creator.OutputFileType == ENUMS.OutputFileTypeOption.Dxf
    assert creator.FileSaveFlag is False and creator.destroyed
    assert creator.ObjectTypes.Solids and not creator.ObjectTypes.Curves
    assert creator.SettingsFile == str(ugii / "DXFDWG" / "dxfdwg.def")
    data = ok.data
    assert data["format"] == "dxf" and data["schema"] == "AC1018" and data["file_units"] == "mm"
    assert data["surfaces"] == 4 and data["solids"] == 0 and data["bodies"] == 2
    assert data["bounds"] == {"min": [0, 0, 0], "max": [210, 20, 30]}
    assert data["part_saved"] is False and part.IsModified
    assert (root / "m.prt").read_bytes() == saved
    assert (root / "out" / "m.dxf").read_bytes().startswith(b"0\nSECTION")


def test_dwg_selected_body(dxf, root):
    part = modified_part(dxf)
    ref = dxf.ref_of(part.Bodies[1], "body")
    ok = call(dxf, exchange.nx_export_model, part="m", path="m.dwg", bodies=[ref])
    assert ok.ok, ok
    creator = dxf.exporters[-1]
    assert creator.OutputFileType == ENUMS.OutputFileTypeOption.Dwg
    assert creator.selected == [part.Bodies[1]]
    assert ok.data["format"] == "dwg" and ok.data["schema"] == "AC1018"
    assert ok.data["encoding"] == "binary" and ok.data["bounds"]["max"] == [210.0, 20.0, 30.0]
    assert (root / "m.dwg").read_bytes()[:6] == b"AC1018"


def test_dxf_without_its_license_is_refused_before_nx(dxf, root):
    modified_part(dxf)
    dxf.licensed = {}
    bad = call(dxf, exchange.nx_export_model, part="m", path="m.dxf")
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.mutation_outcome == "not_started"
    assert bad.error["details"]["license"] == "ug_to_dxf" and not dxf.exporters
    assert not (root / "m.dxf").exists()


def test_dxf_without_model_data_fails(dxf, root, monkeypatch):
    modified_part(dxf)
    monkeypatch.setattr(DxfdwgCreator, "_file", lambda self, out, b, u: (out, sample_dxf([])))
    bad = call(dxf, exchange.nx_export_model, part="m", path="e.dxf")
    assert bad.error["code"] in ("NX_INCOMPLETE_RESULT", "NX_VERIFICATION_FAILED"), bad
    assert not (root / "e.dxf").exists()


def test_dxf_extent_must_match_the_bodies(dxf, root, monkeypatch):
    modified_part(dxf)
    monkeypatch.setattr(
        DxfdwgCreator, "_file", lambda self, out, b, u: (out, sample_dxf(b, offset=(50, 0, 0)))
    )
    bad = call(dxf, exchange.nx_export_model, part="m", path="x.dxf")
    assert bad.error["details"]["check"] == "extent" and not (root / "x.dxf").exists()


def test_wrong_suffix_for_dxf(dxf):
    modified_part(dxf)
    bad = call(dxf, exchange.nx_export_model, part="m", path="m.dwg", format="dxf")
    assert bad.error["code"] == "NX_UNSUPPORTED_FILE_TYPE" and bad.error["field"] == "/path"


def test_reader_expands_block_references():
    data = sample_dxf([[0, 0, 0, 1, 2, 3]], offset=(10, 0, 5))
    facts = neutral.dxf_model_facts(data)
    assert facts["entities"] == {"3DFACE": 2} and facts["faces"] == 2
    assert facts["min"] == [10, 0, 5] and facts["max"] == [11, 2, 8] and facts["units"] == "mm"


def test_reader_rotated_and_scaled_insert():
    text = sample_dxf([[0, 0, 0, 1, 1, 1]]).decode()
    text = text.replace("2\nUG_SURFACE_001\n10\n0.0", "2\nUG_SURFACE_001\n41\n2\n50\n90\n10\n0.0")
    facts = neutral.dxf_model_facts(text.encode())
    assert facts["min"] == pytest.approx([-1, 0, 0]) and facts["max"] == pytest.approx([0, 2, 1])


def test_reader_rejects_truncated_and_binary():
    with pytest.raises(ValueError):
        neutral.dxf_model_facts(sample_dxf([[0, 0, 0, 1, 1, 1]])[:-5])
    with pytest.raises(ValueError):
        neutral.dxf_model_facts(b"AutoCAD Binary DXF\r\n\x1a\x00")
    with pytest.raises(ValueError):
        neutral.dwg_facts(b"PK\x03\x04" + bytes(300))
    assert neutral.dwg_facts(b"AC1032" + bytes(300))["version"] == "AC1032"
