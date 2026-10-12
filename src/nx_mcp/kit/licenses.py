"""License presence, asked before NX is asked to do licensed work.

    licenses.require(tx, ("nx_freeform_1",), "This seat has no nx_freeform_1 license")

``Session.LicenseManager.CheckPresence(feature)`` only queries the license server: no
checkout and no NX dialog. Entering an application or task NX cannot license (a Studio
application, Topology Optimization without its token) opens NX's blocking "License Not
Available" dialog instead of raising, so the tools that would do that ask first and
refuse with ``NX_LICENSE_UNAVAILABLE`` before any change.

NX 2406 seat features (probed live): ``solid_modeling`` (Modeling), ``nx_sheet_metal``
(Sheet Metal), ``nx_freeform_1``, ``adv_assemblies``, ``des_top_opt``, ``drafting``.
"""

from __future__ import annotations

from collections.abc import Sequence

from nx_mcp.core.errors import fail

HINT = "Tell the engineer which license is missing; use an alternative"


def served(tx, features: Sequence[str]) -> str | bool:
    """The first of ``features`` the seat serves; False when none is; True when NX cannot
    tell (no license manager on an older host or a fake, or every query raised)."""
    manager = getattr(tx.session, "LicenseManager", None)
    check = getattr(manager, "CheckPresence", None)
    if check is None:
        return True
    answered = False
    for feature in features:
        try:
            present = bool(check(feature))
        except Exception:  # NX could not answer for this feature
            continue
        answered = True
        if present:
            return feature
    return not answered


def require(
    tx,
    features: Sequence[str],
    message: str,
    *,
    hint: str = HINT,
    strict: bool = False,
    by_argument: bool = False,
) -> str | None:
    """The first of ``features`` the seat serves, else ``NX_LICENSE_UNAVAILABLE`` (details
    ``license``: the first feature). When NX cannot tell: None, or the refusal with
    ``strict`` (for work whose missing license opens a blocking NX dialog). ``by_argument``:
    the license follows this call's arguments, so the kernel does not refuse the whole
    tool for the rest of the session (``core.evidence.remember_license``)."""
    found = served(tx, features)
    if isinstance(found, str):
        return found
    if found and not strict:
        return None
    details: dict = {"license": features[0]}
    if by_argument:
        details["argument_license"] = True
    fail("NX_LICENSE_UNAVAILABLE", message, hint=hint, details=details)
