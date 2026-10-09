"""Regressions found by the NX 2406 platform/file-tool regression (reg_platform)."""

import math

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Feature, Sketch
from tests.test_authoring_review import author  # noqa: F401
from tests.test_engineering import eng, project  # noqa: F401

pytestmark = pytest.mark.fake_nx


class DimensionError(Exception):
    ErrorCode = 3270081


def test_bind_parameter_dimension_error_is_a_stable_code(author):  # noqa: F811
    """NX 2406 raised a raw NX_API_ERROR 'Dimension error' for an angle on a length."""
    edit = author.part.Expressions.EditExpression.side_effect

    def reject(target, formula):
        if formula == "height":
            raise DimensionError("Dimension error")
        return edit(target, formula)

    author.part.Expressions.EditExpression.side_effect = reject
    with pytest.raises(NXToolError) as error:
        author.e.execute(
            "nx_bind_parameter",
            {
                "feature": author.ref(author.feature, "feature"),
                "parameter": "end",
                "expression": "height",
            },
        )
    assert error.value.code == "NX_INVALID_ARGUMENT"
    assert error.value.nx_code == 3270081
    assert error.value.details["mutation_outcome"] == "rolled_back"
    author.builder.CommitFeature.assert_not_called()
    author.builder.Destroy.assert_called_once()


@pytest.mark.parametrize(
    "start,end",
    [({"x": 0, "y": 7}, {"x": 0, "y": 7}), ({"x": 0, "y": 0}, {"x": math.inf, "y": 0})],
)
def test_degenerate_sketch_line_is_refused(rig, start, end):
    """NX 2406 silently created a zero-length line (also through nx_batch)."""
    sk = Sketch(rig.session)
    rig.session.ActiveSketch = sk
    with pytest.raises(NXToolError) as error:
        rig.e._create_sketch_line(sk, rig.part, start, end)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    assert not sk.GetAllGeometry()


def test_rename_by_unique_name_and_feature_timestamp(rig):
    """Renaming by name raised KeyError (NX_API_ERROR); feature info lacked its timestamp."""
    feature = Feature("Block")
    feature.Timestamp = 3
    rig.part.Features.append(feature)
    result = rig.e.execute("nx_rename_object", {"object_id": "Block", "name": "Renamed"})
    assert result["object"]["kind"] == "feature" and feature.Name == "Renamed"
    info = rig.e.execute("nx_get_feature_info", {"name": result["object"]["id"]})
    assert info["name"] == "Renamed" and info["timestamp"] == 3


def _load_options(r):
    from types import SimpleNamespace as NS

    r.nx.LoadOptions = NS(LoadMethod=NS(AsSaved="as_saved"))
    options = NS(ComponentLoadMethod="search_dirs")
    r.session.Parts.LoadOptions = options
    return options


def test_copy_project_clones_with_as_saved_loading_and_restores(project):  # noqa: F811
    """NX 2406 clone used search directories and saw components as lost (720103)."""
    options = _load_options(project)
    seen = []
    project.clone.AddAssembly.side_effect = lambda _: seen.append(options.ComponentLoadMethod)
    dest = project.e.workspace.root / "copied"
    assert project.e._copy_project(str(dest), "COPY_")["references_verified"]
    assert seen == ["as_saved"] and options.ComponentLoadMethod == "search_dirs"


def test_copy_project_unloadable_component_is_a_stable_error(project):  # noqa: F811
    from types import SimpleNamespace as NS

    options = _load_options(project)
    project.clone.AddAssembly.return_value = (
        NS(Failed=True, FileNames=["body"], Statuses=[720090]),
        1,
    )
    dest = project.e.workspace.root / "copied"
    with pytest.raises(NXToolError) as error:
        project.e._copy_project(str(dest), "COPY_")
    assert error.value.code == "NX_UNLOADED_COMPONENT" and error.value.nx_code == 720090
    assert not dest.exists() and options.ComponentLoadMethod == "search_dirs"
    project.clone.Terminate.assert_called_once()
