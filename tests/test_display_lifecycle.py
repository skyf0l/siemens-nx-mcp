"""Legacy section and collision-highlight helpers still used by the legacy inspection report
(display tools are catalogue tools: tests/unit/display)."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Object, point

pytestmark = pytest.mark.fake_nx


def section_builder(rig):
    builders = []

    def create(*args):
        target = args[0] if len(args) == 2 else Object("section")
        if not hasattr(target, "origin"):
            target.origin = point()
            target.normal = point(0, 0, 1)
        b = NS(
            GetOrigin=lambda: target.origin,
            GetNormal=lambda: target.normal,
            Destroy=Mock(),
            ShowClip=True,
            ShowCap=True,
        )
        b.SetName = target.SetName
        b.SetNormal = lambda v: setattr(target, "normal", v)
        b.SetOrigin = lambda v: setattr(target, "origin", v)

        def commit():
            if target not in rig.part.DynamicSections:
                rig.part.DynamicSections.append(target)
            return target

        b.Commit = Mock(side_effect=commit)
        builders.append(b)
        return b

    rig.part.DynamicSections.CreateSectionBuilder = create
    rig.part.DynamicSections.DeleteSections = lambda _, v: [
        rig.part.DynamicSections.remove(x) for x in v
    ]
    return builders


@pytest.mark.parametrize(
    "origin,normal,name",
    [
        ([0, 0], [0, 0, 1], "x"),
        ([0, 0, 0], [0, 0, 0], "x"),
        ([0, 0, 0], [0, 0, 1], ""),
        ([0, 0, 0], [float("nan"), 0, 0], "x"),
    ],
)
def test_invalid_section_parameters_do_not_create_builder(rig, origin, normal, name):
    builders = section_builder(rig)
    with pytest.raises(NXToolError):
        rig.e._section_view(origin, normal, name=name)
    assert not builders
