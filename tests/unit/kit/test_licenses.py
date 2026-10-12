"""kit.licenses: license presence asked before licensed NX work."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import licenses


def tx(served=(), raises=()):
    def check(feature):
        if feature in raises:
            raise RuntimeError("license server busy")
        return feature in served

    return NS(session=NS(LicenseManager=NS(CheckPresence=check)))


def test_the_first_served_feature_is_returned():
    assert licenses.require(tx({"b"}), ("a", "b"), "no") == "b"
    assert licenses.served(tx({"a", "b"}), ("a", "b")) == "a"


def test_a_missing_license_is_refused_with_the_first_feature():
    with pytest.raises(NXError) as error:
        licenses.require(tx(), ("des_top_opt", "sc_des_topol_opt"), "Not licensed", hint="Ask")
    assert error.value.code == "NX_LICENSE_UNAVAILABLE"
    assert error.value.details == {"license": "des_top_opt"} and error.value.hint == "Ask"
    with pytest.raises(NXError) as error:
        licenses.require(tx(), ("nx_sheet_metal",), "Not licensed", by_argument=True)
    assert error.value.details == {"license": "nx_sheet_metal", "argument_license": True}


def test_when_nx_cannot_tell_only_strict_refuses():
    unknown = [NS(session=NS()), tx(raises={"a"})]  # no manager; every query raised
    for case in unknown:
        assert licenses.served(case, ("a",)) is True
        assert licenses.require(case, ("a",), "no") is None
        with pytest.raises(NXError):
            licenses.require(case, ("a",), "no", strict=True)
    # One query raised, another answered no: refused even when not strict.
    with pytest.raises(NXError):
        licenses.require(tx(raises={"a"}), ("a", "b"), "no")
