"""nx_export_drawing to CGM: every selected sheet into one binary metafile."""

from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.domains.drawing.export import cgm_pictures
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.drawing.fakes import drawing_nx


def element(cls, eid, body=b"", long=False):
    if long:
        head = (cls << 12) | (eid << 5) | 31
        out = head.to_bytes(2, "big") + len(body).to_bytes(2, "big") + body
    else:
        out = ((cls << 12) | (eid << 5) | len(body)).to_bytes(2, "big") + body
    return out + (b"\x00" if len(body) & 1 else b"")


def text(value):
    return bytes([len(value)]) + value


# NX's metafile name carries an ISO 2022 escape; one picture per sheet; then END METAFILE.
CGM = (
    element(0, 1, text(b"\x1b%/INX8.5 Metafile"), long=True)
    + element(1, 1, b"\x00\x03")
    + element(0, 3, text(b"SHT1"))
    + element(4, 1, b"\x00" * 16, long=True)
    + element(0, 5)
    + element(0, 3, text(b"SHT2"))
    + element(0, 5)
    + element(0, 2)
)


def cgm_nx(data=CGM):
    nx = drawing_nx()
    nx.make_sheet()
    nx.make_sheet("SHT2")
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], TableSections=[])
    nx.part.PlotManager = NS(CreateCgmBuilder=lambda: nx.builder("CGMBuilder"))
    nx.part.SaveOptions = NS(DrawingCgmData=False)
    written = []

    def plot(b):
        (name,), _ = b.SetFilenames.calls[-1]
        written.append(name)
        Path(name[0]).write_bytes(data)

    nx.on_commit("CGMBuilder", plot)
    return nx, written


def test_cgm_element_stream_gives_the_metafile_and_picture_names():
    assert cgm_pictures(CGM) == ("NX8.5 Metafile", ["SHT1", "SHT2"])
    assert cgm_pictures(CGM[:-2]) is None  # no END METAFILE: cut short
    assert cgm_pictures(element(0, 1) + element(0, 2)) == ("", [])
    assert cgm_pictures(b"%PDF-1.7") is None
    assert cgm_pictures(b"BEGMF 'clear';") is None
    assert cgm_pictures(element(0, 1, text(b"abcd"), long=True)[:-3]) is None


def test_cgm_export_writes_all_sheets_into_one_file():
    nx, written = cgm_nx()
    root = services(nx).root
    ok = run(nx, "nx_export_drawing", path="out/all.cgm")
    assert ok.ok, ok
    assert ok.data["format"] == "cgm"
    assert [s["name"] for s in ok.data["sheets"]] == ["SHT1", "SHT2"]
    assert ok.data["pictures"] == ["SHT1", "SHT2"]
    assert (root / "out" / "all.cgm").read_bytes() == CGM
    b = next(b for b in nx.builders if b.kind == "CGMBuilder")
    assert b.Multiple == b.MultipleOption.Single and b.Action == b.ActionOption.FileBrowser
    assert len(b.SourceBuilder.SetSheets.calls[-1][0][0]) == 2 and len(written) == 1
    assert nx.live_builders == 0
    one = run(nx, "nx_export_drawing", path="out/one.cgm", format="CGM", sheets=["sht2"])
    assert one.ok and [s["name"] for s in one.data["sheets"]] == ["SHT2"]
    pdf_keys = run(nx, "nx_export_drawing", path="out/x.dxf", sheets=["sht1"])
    assert "pictures" not in (pdf_keys.data or {})


def test_cgm_export_rejects_a_file_that_is_not_a_metafile():
    nx, _ = cgm_nx(b"not a cgm")
    bad = run(nx, "nx_export_drawing", path="out/bad.cgm")
    assert bad.error["code"] == "NX_EXPORT_FAILED" and bad.mutation_outcome == "rolled_back"
    assert not (services(nx).root / "out" / "bad.cgm").exists()
    mismatch = run(nx, "nx_export_drawing", path="out/a.cgm", format="pdf")
    assert mismatch.error["field"] == "/path" and mismatch.mutation_outcome == "not_started"
