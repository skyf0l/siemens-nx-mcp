import hashlib
import struct

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import files


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def test_metadata_is_workspace_relative_with_sha256(tmp_path):
    file = tmp_path / "a" / "b.txt"
    file.parent.mkdir()
    file.write_bytes(b"x" * (files.CHUNK + 7))
    meta = files.metadata(file, tmp_path)
    assert meta == {
        "path": "a/b.txt",
        "size": files.CHUNK + 7,
        "sha256": hashlib.sha256(b"x" * (files.CHUNK + 7)).hexdigest(),
    }
    assert files.relative(tmp_path.parent / "elsewhere", tmp_path).endswith("elsewhere")


def test_publish_new_never_overwrites_and_removes_the_temp(tmp_path):
    temp = tmp_path / "t.bin"
    temp.write_bytes(b"new")
    target = tmp_path / "out" / "x.bin"
    assert files.publish_new(temp, target) == target
    assert target.read_bytes() == b"new" and not temp.exists()
    temp.write_bytes(b"other")
    with pytest.raises(NXError) as caught:
        files.publish_new(temp, target, field="/path")
    assert caught.value.code == "NX_ALREADY_EXISTS" and caught.value.field == "/path"
    assert target.read_bytes() == b"new" and not temp.exists()


def test_write_new_is_exclusive(tmp_path):
    target = tmp_path / "d" / "f.dxf"
    files.write_new(target, b"abc")
    with pytest.raises(NXError) as caught:
        files.write_new(target, b"zzz")
    assert caught.value.code == "NX_ALREADY_EXISTS"
    assert target.read_bytes() == b"abc"


def test_png_size_reads_the_header_only(tmp_path):
    good, bad = tmp_path / "a.png", tmp_path / "b.png"
    good.write_bytes(png(64, 32) + b"rest")
    bad.write_bytes(b"GIF89a" + b"\x00" * 30)
    assert files.png_size(good) == (64, 32)
    assert files.png_size(bad) is None


def test_temp_path_is_under_reserved_staging(tmp_path):
    temp = files.temp_path(tmp_path, "sub/x.step", "op_1")
    assert temp.parent == tmp_path / ".nx-mcp" / "staging" / "op_1"
    assert temp.name.endswith("_x.step") and temp.parent.is_dir()
