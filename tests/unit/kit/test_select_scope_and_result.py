"""kit.select keeps refs inside the work part's assembly tree and maps datum features;
kit.result removes float noise; kit.features reads the same alerts as the kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import features, result, select
from tests.core.fakes import FakePart, FakeServices


def _tx(services):
    return NS(
        services=services,
        part=services.work_part(),
        kind_of=services.infer_kind,
        resolve=lambda ref, *kinds, **_: services.references.resolve(ref, kinds),
    )


def test_objects_of_an_unrelated_part_are_wrong_owner_with_their_item(tmp_path):
    svc = FakeServices(tmp_path)
    own = svc.references.row(svc.part.add("body", "MINE"), "body")["id"]
    plate = FakePart(tmp_path / "plate.prt")
    svc.extra_parts.append(plate)
    other = svc.references.row(plate.add("body", "PLATE"), "body")["id"]
    tx = _tx(svc)
    assert select.many_in_tree(tx, [own], "body")[0].Name == "MINE"
    with pytest.raises(NXError) as caught:
        select.many_in_tree(tx, [own, other], "body")
    error = caught.value
    assert error.code == "NX_WRONG_OWNER" and error.field == "/objects/1"
    assert "plate.prt" in error.message and "a.prt" in error.message


def test_a_datum_plane_feature_stands_for_its_plane(tmp_path):
    svc = FakeServices(tmp_path)
    plane = svc.part.add("datum_plane", "DATUM")
    feature = svc.part.add("feature", "DATUM_PLANE(1)")
    feature.DatumPlane = plane
    tx = _tx(svc)
    assert select.datum_of(tx, feature) is plane
    assert select.datum_of(tx, plane) is plane
    with pytest.raises(NXError) as caught:
        select.datum_of(tx, svc.part.add("feature", "EXTRUDE(2)"), "/plane")
    assert caught.value.code == "NX_WRONG_KIND" and caught.value.field == "/plane"
    assert getattr(caught.value, "unchanged", False)  # reported not_started, not rolled back


def test_denoise_rounds_read_backs_and_zeroes_noise():
    noisy = {"a": 50.00000000000001, "b": [76.19999999999999, 7.2e-32], "c": (89.99999999999999,)}
    assert result.denoise(noisy) == {"a": 50.0, "b": [76.2, 0.0], "c": [90.0]}
    assert result.denoise(1e-9, scale=1.0) == 1e-9  # small but real
    assert result.denoise(True) is True and result.denoise("x") == "x"


def test_feature_diagnostics_read_errors_and_warnings(tmp_path):
    svc = FakeServices(tmp_path)
    blend = svc.part.add("feature", "BLEND")
    blend.errors = ["edges consumed"]
    blend.GetFeatureWarningMessages = lambda: ["tool missed the target"]
    svc.part.add("feature", "BLOCK")
    found = features.diagnostics(_tx(svc))
    assert list(found) == [blend.Tag]
    assert found[blend.Tag][1] == {
        ("error", "edges consumed"),
        ("warning", "tool missed the target"),
    }
    assert features.alerts(blend, ("warning",)) == ["tool missed the target"]
