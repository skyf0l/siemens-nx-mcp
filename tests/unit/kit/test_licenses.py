"""kit.licenses: NX's own license refusal reported with the license facts; pre-checks only
where NX would block on a dialog."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError, classify, license_named, native
from nx_mcp.kit import licenses


def tx(served=(), raises=()):
    queries: list[str] = []

    def check(feature):
        queries.append(feature)
        if feature in raises:
            raise RuntimeError("license server busy")
        return feature in served

    return NS(session=NS(LicenseManager=NS(CheckPresence=check)), queries=queries)


class Refusal(Exception):
    """A native NX exception (``ErrorCode``), as NXOpen raises it."""

    def __init__(self, code=948801, text="License Error, The check out of a feature failed "
                 "because its token feature does not exist."):  # fmt: skip
        super().__init__(text)
        self.ErrorCode = code


# ---- require: the strict pre-check of work NX would block on --------------------------------


def test_the_first_served_feature_is_returned():
    assert licenses.require(tx({"b"}), ("a", "b"), "no") == "b"
    assert licenses.served(tx({"a", "b"}), ("a", "b")) == "a"


def test_a_missing_license_is_refused_with_the_first_feature():
    with pytest.raises(NXError) as error:
        licenses.require(tx(), ("des_top_opt", "sc_des_topol_opt"), "Not licensed", hint="Ask")
    assert error.value.code == "NX_LICENSE_UNAVAILABLE" and error.value.hint == "Ask"
    assert error.value.details == {
        "license": "des_top_opt",
        "license_check": "missing",
        "licenses": ["des_top_opt", "sc_des_topol_opt"],
    }
    with pytest.raises(NXError) as error:
        licenses.require(tx(), ("nx_sheet_metal",), "Not licensed", by_argument=True)
    assert error.value.details == {
        "license": "nx_sheet_metal",
        "license_check": "missing",
        "argument_license": True,
    }


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


# ---- attempt: try NX, explain its refusal ------------------------------------------------


def test_attempt_never_asks_before_nx():
    seat = tx()  # the license server lists nothing: NX is still asked
    ran = []
    with licenses.attempt(seat, ("drafting", "nx_2d_edit")):
        ran.append(True)
    assert ran == [True] and seat.queries == []


def test_a_native_refusal_names_the_feature_tried_and_the_alternatives():
    seat = tx(served={"nx_2d_edit"})
    with pytest.raises(NXError) as error, licenses.attempt(seat, ("drafting", "nx_2d_edit")):
        raise Refusal()
    e = error.value
    assert e.code == "NX_LICENSE_UNAVAILABLE" and e.nx_code == 948801
    assert e.details["license"] == "drafting"
    assert e.details["licenses"] == ["drafting", "nx_2d_edit"]
    assert e.details["license_check"] == "present"  # listed, yet NX refused: a used-up pool
    assert "drafting" in e.hint and isinstance(e.__cause__, Refusal)


def test_a_refusal_inside_native_keeps_its_code_and_gets_the_facts():
    attempt = licenses.attempt(tx(), ("nx_freeform_1",))
    with pytest.raises(NXError) as error, attempt, native(default="NX_BUILD_FAILED"):
        raise Refusal(949895, "license feature is not available")
    e = error.value
    assert e.code == "NX_LICENSE_UNAVAILABLE" and e.nx_code == 949895
    assert e.details["license"] == "nx_freeform_1" and e.details["license_check"] == "missing"
    assert "does not list nx_freeform_1" in e.message


def test_the_feature_nx_names_wins_over_the_one_tried():
    text = 'License Error: feature "nx_sheet_metal" is not available'
    assert license_named(text) == "nx_sheet_metal"
    assert classify(Refusal(948801, text)).details["license"] == "nx_sheet_metal"
    with pytest.raises(NXError) as error, licenses.attempt(tx(), ("nx_flexible_pcb",)):
        raise Refusal(948801, text)
    assert error.value.details["license"] == "nx_sheet_metal"


def test_nx_messages_without_a_feature_name_give_none():
    assert license_named("its token feature does not exist") is None
    assert license_named("license feature is not available") is None
    assert license_named("Not enough tokens for feature (des_top_opt)") == "des_top_opt"
    assert license_named("license feature: drafting") == "drafting"


def test_every_names_the_first_feature_the_server_does_not_list():
    seat = tx(served={"solid_modeling"})
    attempt = licenses.attempt(seat, ("solid_modeling", "nx_freeform_1"), every=True)
    with pytest.raises(NXError) as error, attempt:
        raise Refusal()
    e = error.value
    assert e.details["license"] == "nx_freeform_1"
    assert e.details["licenses_all"] == ["solid_modeling", "nx_freeform_1"]
    assert e.details["license_check"] == "missing"


def test_by_argument_marks_the_refusal_and_other_errors_pass_unchanged():
    attempt = licenses.attempt(tx(), ("ug_kf_execute",), by_argument=True)
    with pytest.raises(NXError) as error, attempt:
        raise Refusal()
    assert error.value.details["argument_license"] is True
    attempt = licenses.attempt(tx(), ("drafting",))
    with pytest.raises(NXError) as other, attempt, native(default="NX_BUILD_FAILED"):
        raise Refusal(670054, "Invalid face")
    assert other.value.code == "NX_BUILD_FAILED" and "license" not in other.value.details
    with pytest.raises(ValueError), licenses.attempt(tx(), ("drafting",)):
        raise ValueError("a bug stays a bug")


def test_no_license_manager_still_reports_the_feature_tried():
    with pytest.raises(NXError) as error, licenses.attempt(NS(session=NS()), ("drafting",)):
        raise Refusal()
    assert error.value.details["license"] == "drafting"
    assert "license_check" not in error.value.details
