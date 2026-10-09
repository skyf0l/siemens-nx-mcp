import base64
import hashlib
import struct
from unittest.mock import AsyncMock

import pytest

from nx_mcp.domains.parts_files import workspace as ws
from nx_mcp.sidecar.proxy import CatalogueProxy
from nx_mcp.workspace import Workspace
from tests.unit.parts_files.support import side


def png(width=64, height=32):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"x"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "proj" / "sub").mkdir(parents=True)
    (tmp_path / "proj" / "a.txt").write_bytes(b"alpha")
    (tmp_path / "proj" / "b.txt").write_bytes(b"beta")
    (tmp_path / ".nx-mcp").mkdir()
    (tmp_path / ".NX-MCP2").mkdir()
    return tmp_path


def test_list_files_pages_with_checksums_and_hides_internal_state(tree):
    ok = side(tree, ws.nx_list_files, path="proj", limit=2)
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    assert ok.data["path"] == "proj"
    assert ok.data["items"] == [
        {"path": "proj/a.txt", "size": 5, "sha256": hashlib.sha256(b"alpha").hexdigest(),
         "kind": "file"},
        {"path": "proj/b.txt", "size": 4, "sha256": hashlib.sha256(b"beta").hexdigest(),
         "kind": "file"},
    ]  # fmt: skip
    assert ok.page == {"offset": 0, "limit": 2, "returned": 2, "total": 3, "next_offset": 2}
    rest = side(tree, ws.nx_list_files, path="proj", offset=2)
    assert rest.data["items"] == [{"path": "proj/sub", "kind": "directory"}]
    assert side(tree, ws.nx_list_files, path="proj", prefix="B").page["total"] == 1
    top = side(tree, ws.nx_list_files)
    assert [i["path"] for i in top.data["items"]] == [".NX-MCP2", "proj"]


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("proj/a.txt", "NX_WRONG_KIND"),
        ("proj/missing", "NX_FILE_NOT_FOUND"),
        ("../outside", "NX_PATH_OUTSIDE_WORKSPACE"),
        (".nx-mcp", "NX_PATH_RESERVED"),
        ("proj/../.NX-MCP/operations", "NX_PATH_RESERVED"),
        ("C:relative", "NX_PATH_OUTSIDE_WORKSPACE"),
        ("\\rooted", "NX_PATH_OUTSIDE_WORKSPACE"),
    ],
)
def test_list_files_rejections(tree, path, code):
    bad = side(tree, ws.nx_list_files, path=path)
    assert bad.error["code"] == code and bad.error["field"] == "/path"
    assert bad.mutation_outcome == "not_applicable"


def test_create_directory_is_idempotent_and_refuses_files(tree):
    first = side(tree, ws.nx_create_directory, path="new/deep")
    assert first.ok and first.data == {"path": "new/deep", "created": True}
    assert first.mutation_outcome == "committed"
    assert side(tree, ws.nx_create_directory, path="new/deep").data["created"] is False
    bad = side(tree, ws.nx_create_directory, path="proj/a.txt")
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.mutation_outcome == "not_started"
    reserved = side(tree, ws.nx_create_directory, path=".Nx-Mcp/x")
    assert reserved.error["code"] == "NX_PATH_RESERVED"
    assert not (tree / ".Nx-Mcp" / "x").exists()


def chunked(data, size):
    return [(i, base64.b64encode(data[i : i + size]).decode()) for i in range(0, len(data), size)]


def test_upload_in_chunks_with_retry_conflict_and_gap(tree):
    data = bytes(range(256)) * 1200  # 307200 bytes
    digest = hashlib.sha256(data).hexdigest()
    (o1, c1), (o2, c2) = chunked(data, 200_000)
    common = {"path": "in/blob.json", "total_size": len(data), "sha256": digest}
    first = side(tree, ws.nx_upload_file, data_base64=c1, offset=o1, **common)
    assert first.data["committed"] is False and first.data["received"] == 200_000
    assert not (tree / "in" / "blob.json").exists()
    again = side(tree, ws.nx_upload_file, data_base64=c1, offset=o1, **common)
    assert again.ok and again.data["received"] == 200_000
    other = base64.b64encode(b"z" * 10).decode()
    conflict = side(tree, ws.nx_upload_file, data_base64=other, offset=0, **common)
    assert (
        conflict.error["code"] == "NX_TRANSFER_FAILED" and conflict.error["field"] == "/data_base64"
    )
    gap = side(tree, ws.nx_upload_file, data_base64=other, offset=250_000, **common)
    assert gap.error["code"] == "NX_TRANSFER_FAILED" and gap.error["details"]["received"] == 200_000
    done = side(tree, ws.nx_upload_file, data_base64=c2, offset=o2, **common)
    assert done.ok and done.data["committed"] is True and done.mutation_outcome == "committed"
    assert (tree / "in" / "blob.json").read_bytes() == data
    exists = side(tree, ws.nx_upload_file, data_base64=c2, offset=o2, **common)
    assert exists.error["code"] == "NX_ALREADY_EXISTS" and exists.mutation_outcome == "not_started"
    assert not list((tree / ".nx-mcp" / "uploads").iterdir())


def test_upload_checksum_mismatch_publishes_nothing(tree):
    data = b"hello world"
    bad = side(tree, ws.nx_upload_file, path="w.txt", data_base64=base64.b64encode(data).decode(),
               offset=0, total_size=len(data), sha256="0" * 64)  # fmt: skip
    assert bad.error["code"] == "NX_TRANSFER_FAILED" and bad.error["field"] == "/sha256"
    assert not (tree / "w.txt").exists()


@pytest.mark.parametrize(
    ("args", "code", "field"),
    [
        ({"path": "x.exe"}, "NX_UNSUPPORTED_FILE_TYPE", "/path"),
        ({"path": "../x.txt"}, "NX_PATH_OUTSIDE_WORKSPACE", "/path"),
        ({"path": ".nx-mcp/x.txt"}, "NX_PATH_RESERVED", "/path"),
        ({"sha256": "A" * 64}, "NX_INVALID_ARGUMENT", "/sha256"),
        ({"data_base64": "!!"}, "NX_INVALID_ARGUMENT", "/data_base64"),
        ({"offset": 5}, "NX_INVALID_ARGUMENT", "/offset"),
        (
            {"data_base64": base64.b64encode(b"x" * 262_145).decode(), "total_size": 300_000},
            "NX_REQUEST_TOO_LARGE",
            "/data_base64",
        ),  # fmt: skip
    ],
)
def test_upload_rejections(tree, args, code, field):
    call = {"path": "u.txt", "data_base64": "YWJj", "offset": 0, "total_size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(), **args}  # fmt: skip
    bad = side(tree, ws.nx_upload_file, **call)
    assert bad.error["code"] == code and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started"


def test_download_metadata_chunks_and_image(tree):
    (tree / "big.bin").write_bytes(b"0123456789" * 30_000)
    meta = side(tree, ws.nx_download_file, path="big.bin")
    assert meta.data["delivery"] == "metadata" and meta.data["size"] == 300_000
    assert "data_base64" not in meta.data
    one = side(tree, ws.nx_download_file, path="big.bin", delivery="chunk")
    assert one.data["bytes_returned"] == 262_144 and one.data["next_offset"] == 262_144
    two = side(tree, ws.nx_download_file, path="big.bin", delivery="chunk", offset=262_144)
    assert two.data["eof"] is True and two.data["next_offset"] is None
    joined = base64.b64decode(one.data["data_base64"]) + base64.b64decode(two.data["data_base64"])
    assert joined == (tree / "big.bin").read_bytes()
    tail = side(tree, ws.nx_download_file, path="big.bin", delivery="chunk", offset=299_990,
                length=100)  # fmt: skip
    assert tail.data["bytes_returned"] == 10
    (tree / "cap.png").write_bytes(png())
    image = side(tree, ws.nx_download_file, path="cap.png", delivery="image")
    assert image.data["image"]["inline"] is True and image.data["image"]["width"] == 64
    assert image.data["image"]["sha256"] == image.data["sha256"]


@pytest.mark.parametrize(
    ("args", "code", "field"),
    [
        ({"path": "big.bin", "delivery": "image"}, "NX_UNSUPPORTED_FILE_TYPE", "/path"),
        ({"path": "big.bin", "offset": 3}, "NX_INVALID_ARGUMENT", "/offset"),
        ({"path": "big.bin", "delivery": "image", "length": 3}, "NX_INVALID_ARGUMENT", "/length"),
        (
            {"path": "big.bin", "delivery": "chunk", "length": 262_145},
            "NX_INVALID_ARGUMENT",
            "/length",
        ),  # fmt: skip
        ({"path": "nope.bin"}, "NX_FILE_NOT_FOUND", "/path"),
        ({"path": ".nx-mcp/x"}, "NX_PATH_RESERVED", "/path"),
    ],
)
def test_download_rejections(tree, args, code, field):
    (tree / "big.bin").write_bytes(b"abc")
    bad = side(tree, ws.nx_download_file, **args)
    assert bad.error["code"] == code and bad.error["field"] == field


async def test_the_proxy_runs_sidecar_tools_locally_and_attaches_png(tree, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    store = type("Store", (), {"put": lambda self, e: "res_1"})()
    proxy = CatalogueProxy(bridge, Workspace(tree), store, build="v2406")
    (tree / "cap.png").write_bytes(png(8, 4))
    answer = await proxy.call(
        ws.nx_download_file.spec, {"path": "cap.png", "delivery": "image"}, detail="full"
    )
    assert not answer.isError and bridge.call.await_count == 0
    kinds = [c.type for c in answer.content]
    assert kinds == ["text", "image", "resource_link"]
    assert str(answer.content[2].uri) == "nx-artifact://workspace/cap.png"
    assert base64.b64decode(answer.content[1].data) == png(8, 4)
    assert "iVBOR" not in answer.content[0].text  # the bytes never appear as text
    made = await proxy.call(ws.nx_create_directory.spec, {"path": "made"}, detail="full")
    assert made.structuredContent["mutation_outcome"] == "committed"
    assert made.structuredContent["operation_id"].startswith("op_")
    assert bridge.call.await_count == 0


async def test_a_changed_image_is_not_attached(tree, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    proxy = CatalogueProxy(AsyncMock(), Workspace(tree), None, build="v2406")
    (tree / "cap.png").write_bytes(png())
    envelope = {"ok": True, "data": {"image": {"path": "cap.png", "sha256": "0" * 64,
                                               "inline": True}}}  # fmt: skip
    assert proxy._image(envelope) is None
    assert "changed" in envelope["warnings"][0]["message"]


@pytest.mark.parametrize("name", ["expr/params.exp", "inbox/plate.dxf", "inbox/block.x_t"])
def test_upload_expression_and_import_files(tree, name):
    data = b"p0=10\n"
    ok = side(tree, ws.nx_upload_file, path=name, data_base64=base64.b64encode(data).decode(),
              offset=0, total_size=len(data), sha256=hashlib.sha256(data).hexdigest())  # fmt: skip
    assert ok.ok and ok.data["committed"] is True, ok
    assert (tree / name).read_bytes() == data
