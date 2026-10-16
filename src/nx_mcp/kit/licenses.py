"""Licensed NX work: try it and report NX's refusal, or ask first where NX would block.

    with licenses.attempt(tx, ("drafting", "nx_2d_edit")):
        ...  # the NX calls that need one of these license features

NX checks a license when the call needs it and refuses with a native error (948801: no
token feature, 949895: feature not available, or a "License Error" text): ``native()``
maps it to ``NX_LICENSE_UNAVAILABLE`` and the kernel rolls back. :func:`attempt` adds what
the engineer needs to know: ``details.license`` (the feature NX named, else the first of
the features the tool tried; ``licenses`` lists alternatives), ``license_check`` (what
``LicenseManager.CheckPresence`` answers now: a "present" feature NX still refused points
at a used-up floating or token pool) and ``argument_license`` (the license follows this
call's arguments: the session does not remember the whole tool,
``core.evidence.remember_license``).

``CheckPresence`` only queries the license server (no checkout, no dialog) and can be wrong
both ways (a feature present but not available; a seat licensed through an alternative
feature), so it never refuses work NX would refuse on its own. :func:`require` asks first
only where NX opens its blocking "License Not Available" dialog instead of raising:
switching to an application (``nx_activate_application``) and entering the Topology
Optimization task.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence

from nx_mcp.core.errors import NXError, classify, fail

HINT = "Tell the engineer which license is missing; use an alternative"
CODE = "NX_LICENSE_UNAVAILABLE"


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
    """Only for work whose missing license opens a blocking NX dialog: the first of
    ``features`` the seat serves, else ``NX_LICENSE_UNAVAILABLE`` before NX is asked
    (details ``license``: the first feature). When NX cannot tell: None, or the refusal
    with ``strict``. ``by_argument``: see the module docstring."""
    found = served(tx, features)
    if isinstance(found, str):
        return found
    if found and not strict:
        return None
    details: dict = {"license": features[0], "license_check": "missing"}
    if len(features) > 1:
        details["licenses"] = list(features)
    if by_argument:
        details["argument_license"] = True
    fail(CODE, message, hint=hint, details=details)


def _presence(tx, features: Sequence[str]) -> dict[str, bool]:
    """``{feature: CheckPresence answer}`` for the features NX could answer for."""
    check = getattr(getattr(tx.session, "LicenseManager", None), "CheckPresence", None)
    out: dict[str, bool] = {}
    if check is None:
        return out
    for feature in features:
        try:
            out[feature] = bool(check(feature))
        except Exception:  # NX could not answer for this feature
            continue
    return out


def explain(
    tx, error: NXError, features: Sequence[str], *, every: bool = False, by_argument: bool = False
) -> NXError:
    """Add the license facts to NX's ``NX_LICENSE_UNAVAILABLE`` refusal (see the module).
    ``features``: alternatives, or with ``every`` all needed (the first one the license
    server does not list is named)."""
    details = error.details
    tried = list(features)
    if tried:
        try:
            presence = _presence(tx, tried)
        except Exception:  # never hide NX's refusal behind a failed query
            presence = {}
        missing = [f for f in tried if presence.get(f) is False]
        if every:
            details.setdefault("license", missing[0] if missing else tried[0])
            if len(tried) > 1:
                details.setdefault("licenses_all", tried)
        else:
            details.setdefault("license", tried[0])
            if len(tried) > 1:
                details.setdefault("licenses", tried)
        gone = missing if every or len(missing) == len(tried) else []
        if gone:
            details.setdefault("license_check", "missing")
            error.message += f" (NX's license server does not list {', '.join(gone)})"
        elif any(presence.values()):
            details.setdefault("license_check", "present")
    if by_argument:
        details["argument_license"] = True
    if error.hint is None:
        named = details.get("license")
        error.hint = f"Tell the engineer NX has no {named} license for this; use an alternative"
    return error


@contextlib.contextmanager
def attempt(
    tx, features: Sequence[str], *, every: bool = False, by_argument: bool = False
) -> Iterator[None]:
    """Run licensed NX work (``features``: alternatives, or with ``every`` all needed);
    NX's own license refusal inside comes out as ``NX_LICENSE_UNAVAILABLE`` with the
    license facts (:func:`explain`). Any other error passes unchanged. Never asks before
    NX: a seat may hold an alternative feature."""
    try:
        yield
    except NXError as error:
        if error.code == CODE:
            explain(tx, error, features, every=every, by_argument=by_argument)
        raise
    except Exception as error:
        classified = classify(error)
        if classified.code != CODE:
            raise
        raise explain(tx, classified, features, every=every, by_argument=by_argument) from error
