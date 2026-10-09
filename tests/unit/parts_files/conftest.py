"""Fixtures: a fake NX with part files (tests/kit/parts.py) and the real kernel."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.kit import fake_nx
from tests.kit.kernel import services
from tests.kit.parts import with_parts


@pytest.fixture
def nx():
    fake = fake_nx()
    with_parts(fake, services(fake).root)
    return fake


@pytest.fixture
def root(nx) -> Path:
    return services(nx).root


@pytest.fixture
def ugii(tmp_path, monkeypatch):
    base = tmp_path / "nxbase"
    (base / "STEP214UG").mkdir(parents=True)
    (base / "STEP214UG" / "ugstep214.def").write_text("export settings")
    (base / "STEP214UG" / "step214ug.def").write_text("import settings")
    (base / "IGES").mkdir()
    (base / "IGES" / "igesexport.def").write_text("iges export settings")
    (base / "IGES" / "igesimport.def").write_text("iges import settings")
    for folder, name in (("DXFDWG", "dxfdwg.def"), ("CATIAV5", "catiav5.def")):
        (base / folder).mkdir()
        (base / folder / name).write_text("translator settings")
    monkeypatch.setenv("UGII_BASE_DIR", str(base))
    return base
