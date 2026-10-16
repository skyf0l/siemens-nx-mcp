"""The kernel: one fixed gate order for every catalogue call (architecture.md 3.1).

``run(spec, args, ctx)`` applies the NX-side gates to a top-level call and
always returns the surface 1.6 envelope (``ok`` true or false); it never raises
for a tool failure. ``run_child(spec, args, parent_tx)`` applies the gates
marked C to a child of ``nx_run_batch`` / ``nx_preview_changes`` and raises
``NXError`` so the parent rolls everything back under its one mark. A child resolves its
own target (its refs, a ``part`` call key in its arguments, else the parent's part) and
is switched to it for its step (gates 3t and 4s); NX's undo is session-wide, so the one
mark covers every part. A failure after a child ran always rolls back (never
``not_started``, even when the failing child refused inside ``tx.checks()``).

Gate order: 1 schema, 3t target part, 3r references (every ref argument, list items
included, is live and of a kind its field takes, else refused ``not_started`` with its
JSON pointer as ``field``), 2 workspace paths, 3 variant inference (in the target part)
and evidence/version, 4 ``requires``,
5 engineer control hold (the host, before the kernel runs), 6 part ownership,
7 receipt, 8 undo mark, 9 unit conversion, 10 handler, 11 read-back (new NX feature
errors and warnings since the call began come back as warnings naming the feature), 12
reference invalidation, 13 annotation refresh, 14 result projection, 15 error mapping
(an update failure names its failing features; a read-back mismatch that NX explained
with feature alerts, or that left the measured value unchanged, is ``geometry_failed``).
A committed call that changed nothing and said so (``NX_NO_EFFECT``) is ``not_applicable``.
Nothing else applies gates. Around 8-15 a non-read call's loads become barriers in the
undo history and dropped undo marks get their reason (``core/marks.py``); a committed
changing call stamps the parts it worked in (``core/busy.py``). Gate 4's
``no_active_sketch`` also refuses in a part whose sketch is parked (``core/parking.py``);
an active drafting (sheet) sketch NX re-activated is deactivated instead of refusing.

Batch placeholders: a Ref field of a child may be ``"$<i>.<path>"`` naming the
result envelope of child ``i`` (0-based, earlier children only), e.g.
``"$0.data.feature"``; a row resolves to its ``id``.
"""

from __future__ import annotations

import contextlib
import copy
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from nx_mcp.core import busy, evidence, parking, switching
from nx_mcp.core import marks as undo_marks
from nx_mcp.core import target as targets
from nx_mcp.core.errors import CATEGORIES, NXError, classify, fail, missing_member
from nx_mcp.core.receipts import ANY_PART, Receipts, new_operation_id
from nx_mcp.core.references import GENERIC_NAME as _GENERIC
from nx_mcp.core.tx import Services, Tx, check_owned
from nx_mcp.core.types import (
    SHEET_KINDS,
    convert_quantity,
    expand_kinds,
    suffix_kinds,
    suffix_of,
)
from nx_mcp.core.validate import (
    Checked,
    at_child,
    combine,
    get_at,
    pointer,
    range_error,
    set_at,
    variant_needs,
)

REQUIRE_FAILURES = {
    "work_part": (
        "NX_NO_WORK_PART",
        "No work part is open",
        "Open or create a part with nx_open_part or nx_create_part",
    ),
    "no_active_sketch": (
        "NX_SKETCH_ACTIVE",
        "A sketch is active",
        "Finish the active sketch with nx_finish_sketch",
    ),
    "drawing_sheet": (
        "NX_WRONG_APPLICATION",
        "No drawing sheet is active",
        "Open a sheet with nx_activate_drawing_sheet",
    ),
    "assembly": (
        "NX_WRONG_PART_CONTEXT",
        "The work part is not an assembly",
        "Name the assembly with part (nx_list_parts lists the loaded parts)",
    ),
    "interactive": (
        "NX_VIEWPORT_UNAVAILABLE",
        "This tool needs the interactive NX session",
        "Run it in interactive NX with the NX MCP panel",
    ),
}


@dataclass
class Context:
    services: Services
    operation_id: str | None = None
    intent: str | None = None
    # Call keys inside ``arguments`` (core/target.py): the target part (file name, path
    # or p:N) and when the client gives up (epoch seconds or ISO 8601).
    part: str | None = None
    expires_at: Any = None
    parent: Any = None  # the calling Tx's target (nx_redo_operation's own calls)
    caller: Any = None  # who sends the call (the sidecar's ``caller`` key, core/busy.py)


# ---------------------------------------------------------------------------
# Gates shared by top-level calls and children


def _gate_schema(  # gate 1
    spec, args, *, placeholders=False, where="nx", services=None
) -> Checked:
    # ``where`` is the process the services run in (``Services.runs``): NX, or the
    # sidecar for ``runs="sidecar"`` workspace tools (sidecar/local.py).
    if spec.runs != where:
        fail("NX_UNSUPPORTED_OPTION", f"{spec.name} runs in the {spec.runs}, not in {where}")
    return spec.check(args, placeholders=placeholders, shown=offered_variants(spec, services))


def offered_variants(spec, services) -> tuple | None:
    """The variant values this NX build offers (tested, not refused for a license this
    session), which a variant error lists; None for all (development mode, no variants)."""
    if services is None or not isinstance(spec.variants, str) or getattr(services, "dev", False):
        return None
    with contextlib.suppress(Exception):
        values = evidence.variants_for(spec, evidence.build_key(services.build))
        remembered = {e.get("variant") for e in evidence.licenses() if e.get("tool") == spec.name}
        return tuple(v for v in values if v not in remembered) or None
    return None


def _gate_variant(spec, checked: Checked, services: Services, target=None) -> None:  # gate 3
    """An omitted variant of a ``variant_from`` tool, taken from the object its ref names
    (``nx_edit_drawing_table`` without ``kind``: the table's kind); then the fields that
    variant needs (``required_by``). The resolver reads the call's target part (gate 3t),
    which need not be the work part yet (gate 4s comes later)."""
    if spec.variant_from is None or checked.args.get(spec.variants) is not None:
        return
    name, resolver = spec.variant_from
    ref = checked.args.get(name)
    value = None
    if isinstance(ref, str):
        previous = services.bound
        try:
            obj = services.references.resolve(ref, ())  # the resolver tells kinds apart
            if resolver is not None:
                if target is not None and target.part is not None:
                    services.bound = target.part  # tx.part is the target part
                value = resolver(Tx(services, spec, operation_id=None), obj)
            else:
                value = getattr(services.references.entry(ref), "kind", None)
        except NXError as error:  # a stale or unknown ref: say which field
            error.field = error.field or "/" + name
            raise
        except Exception:
            value = None  # the resolver cannot tell: the variant must be given
        finally:
            services.bound = previous
    values = spec.variant_values()
    if value not in values:
        field = "/" + spec.variants
        what = f"the {value} that {ref} names" if value else f"/{name}"
        fail(
            "NX_INVALID_ARGUMENT",
            f"{field}: required; it cannot be taken from {what} "
            f"(one of {', '.join(repr(v) for v in values)})",
            field=field,
        )
    checked.args[spec.variants] = value
    errors = variant_needs(spec.params, checked.args, spec.variants, spec.required_by, spec.name)
    if errors:
        raise combine(errors)


def _application_first(spec, services: Services, error: NXError) -> None:
    """A schema error of a tool that needs an application NX is not in: name the
    application step too, so the agent does not fix the arguments only to be refused
    by gate 4 on the next call."""
    wanted = next((r.split(":", 1)[1] for r in spec.requires if r.startswith("application:")), None)
    if wanted is None:
        return
    with contextlib.suppress(Exception):
        if services.state().get("application") != wanted:
            base = (error.hint or CATEGORIES[error.category][2]).rstrip(".")
            error.hint = f"{base}. This tool also needs the {wanted} application: " + (
                "switch with nx_activate_application first"
            )


def _relative(services: Services, path: Path) -> str:
    """``path`` relative to the workspace root (never the host's absolute path)."""
    try:
        return path.resolve().relative_to(Path(services.workspace.root).resolve()).as_posix()
    except (ValueError, OSError, TypeError, AttributeError):
        return path.name


def _given(services: Services, value) -> str:
    """A path argument as error details show it: workspace-relative when it is an
    absolute path inside the workspace (the sidecar sends paths resolved), else as given."""
    text = str(value)
    with contextlib.suppress(ValueError, OSError, TypeError, AttributeError):
        if Path(text).is_absolute():
            return Path(text).relative_to(Path(services.workspace.root)).as_posix()
    return text


def _free_name(resolved: Path) -> Path:
    """The first ``<stem>-<n><suffix>`` beside ``resolved`` that does not exist."""
    for n in range(2, 1000):
        candidate = resolved.with_name(f"{resolved.stem}-{n}{resolved.suffix}")
        if not candidate.exists():
            return candidate
    return resolved


def _gate_paths(spec, checked: Checked, services: Services, target=None) -> None:  # gate 2
    """Workspace checks of every path argument, by the sidecar's rules (both process
    boundaries refuse the same paths, every refusal fixable: ``retry: after_fix``): a
    folder where a file is wanted is refused; a path without a suffix takes, for a
    ``read`` path, the one existing file of an allowed suffix, for a new file the only
    allowed kind or the one its format option names (``format: 'iges'`` -> ``.iges``),
    else it is refused; a suffix the tool does not take is ``NX_UNSUPPORTED_FILE_TYPE``.
    ``save`` paths compare against the call's target part (gate 3t), else the work part."""
    for path, marker in checked.paths:
        field = pointer(path)
        value = get_at(checked.args, path)
        try:
            resolved = Path(services.workspace.resolve(value))
        except Exception as error:
            reserved = any(p.casefold() == ".nx-mcp" for p in Path(str(value)).parts)
            raise NXError(
                "NX_PATH_RESERVED" if reserved else "NX_PATH_OUTSIDE_WORKSPACE",
                f"{field}: {error}",
                field=field,
                details={"path": _given(services, value)},
            ) from None
        resolved = _suffixed(spec, checked.args, field, str(value), resolved, marker, services)
        if marker.mode == "read" and not resolved.is_file():
            fail(
                "NX_FILE_NOT_FOUND",
                f"{field}: no such file",
                field=field,
                hint="Check the path with nx_list_files on its folder",
                details={"path": _relative(services, resolved)},
            )
        if marker.mode == "new" and resolved.exists():
            free = _relative(services, _free_name(resolved))
            fail(
                "NX_ALREADY_EXISTS",
                f"{field}: a file already exists there",
                field=field,
                hint=f"Choose a new path such as '{free}'; files are never overwritten",
                details={"path": _relative(services, resolved), "suggested_path": free},
            )
        if marker.mode == "replace" and resolved.exists() and not resolved.is_file():
            fail(
                "NX_WRONG_KIND",
                f"{field}: not a file; only an existing file can be replaced",
                field=field,
                hint="Choose a file path; nx_list_files shows what exists",
                details={"path": _relative(services, resolved)},
            )
        if marker.mode == "dir" and resolved.exists() and not resolved.is_dir():
            fail(
                "NX_WRONG_KIND",
                f"{field}: a file already exists at this path, not a folder",
                field=field,
                hint="Choose another folder path; nx_list_files shows what exists",
                details={"path": _relative(services, resolved)},
            )
        if marker.mode == "save" and resolved.exists():
            part = target.part if target is not None else services.work_part()
            own = str(getattr(part, "FullPath", "") or "") if part is not None else ""
            if not own or Path(own).resolve() != resolved:
                free = _relative(services, _free_name(resolved))
                fail(
                    "NX_ALREADY_EXISTS",
                    f"{field}: saving may only write the part's own file",
                    field=field,
                    hint=f"Choose a new path such as '{free}', or omit it to save in place",
                    details={"path": _relative(services, resolved), "suggested_path": free},
                )
        set_at(checked.args, path, str(resolved))


def _gate_refs(spec, checked: Checked, services: Services) -> None:  # gate 3r
    """Every reference in a list field names a live object of a kind the field takes
    (``RefTo``): a stale, unknown or wrong-kind item is refused before the receipt and the
    undo mark (``not_started``), with its JSON pointer (``/objects/2``); a tool explains a
    wrong kind through its ``@kind_hint`` hook. A single ref is left to the handler, which
    checks the call's other arguments first and explains the ref in the tool's terms (a
    call whose refs all fail is refused at gate 3t). Values that are not refs (a name a
    field also takes) and batch placeholders (``$0.data.body``, checked once filled in) are
    skipped."""
    references = getattr(services, "references", None)
    if references is None or spec.runs != "nx":
        return
    for path, kinds in checked.refs:
        value = get_at(checked.args, path)
        if not (path and isinstance(path[-1], int)) or not targets.is_ref(value):
            continue
        try:
            references.resolve(value, kinds)
        except NXError as error:
            error.field = error.field or pointer(path)
            if error.code == "NX_WRONG_KIND":
                _explain_kind(spec, services, value, kinds, error)
            setattr(error, "unchanged", True)  # noqa: B010 (nothing ran: not_started)
            raise


def _explain_kind(spec, services: Services, ref: str, kinds, error: NXError) -> None:
    """The hint of a wrong-kind list item: the tool's ``@kind_hint`` hook, else for a body
    where a feature is wanted, the feature that made it. Never raises."""
    from nx_mcp.core import catalogue

    hook = catalogue.hooks("kind_hint").get(spec.name)
    with contextlib.suppress(Exception):
        if hook is not None:
            hook(Tx(services, spec, operation_id=None), ref, error, error.field)
            return
        if error.details.get("kind") == "body" and "feature" in expand_kinds(kinds):
            body = services.references.resolve(ref, ())
            made = [services.references.row(f, "feature")["id"] for f in body.GetFeatures()]
            error.hint = "A body is not a feature: pass the feature that made it" + (
                f" ({made[0]}; nx_list_objects kind feature lists them)" if made else ""
            )
            if made:
                error.details["features"] = made[:20]


SLASHES = ("/", chr(92))


def _folder_error(field: str, shown: str) -> NoReturn:
    fail(
        "NX_WRONG_KIND",
        f"{field}: '{shown}' is a folder; give a file path",
        field=field,
        hint="Name a file inside it, e.g. '" + shown.rstrip("/") + "/name.<suffix>'; "
        "nx_list_files shows what it holds",
        details={"path": shown},
    )


def _suffixed(spec, args, field, given: str, resolved: Path, marker, services) -> Path:
    """Gate 2's suffix and folder rules (the sidecar's ``_path_problem``): the path to use,
    a suffix added where one is certain, or a fixable refusal."""
    mode, suffixes = marker.mode, tuple(marker.suffixes)
    shown = _relative(services, resolved)
    if mode == "dir" or not suffixes:
        if mode != "dir" and given.endswith(SLASHES):
            _folder_error(field, shown)
        return resolved
    if given.endswith(SLASHES) or resolved.is_dir():
        _folder_error(field, shown)
    expected = "/".join(suffixes)
    if not resolved.suffix:
        groups = suffix_kinds(suffixes)
        if mode == "read":
            found = [resolved.with_name(resolved.name + s) for s in suffixes]
            found = [f for f in found if f.is_file()]
            if len(found) == 1 or len(groups) == 1:
                return found[0] if found else resolved.with_name(resolved.name + suffixes[0])
            fail(
                "NX_INVALID_ARGUMENT",
                f"{field}: '{shown}' has no file suffix; name the file with its suffix "
                f"({expected})",
                field=field,
                hint="Check the file name with nx_list_files on its folder",
                details={"path": shown, "expected": list(suffixes)},
            )
        if len(groups) == 1:
            return resolved.with_name(resolved.name + suffixes[0])
        named = suffix_of(spec.variant_of(args), suffixes) if spec is not None else None
        if named is not None:
            return resolved.with_name(resolved.name + named)
        fail(
            "NX_INVALID_ARGUMENT",
            f"{field}: '{shown}' needs a file suffix: one of {expected}",
            field=field,
            hint=f"End the path with the suffix of the file you mean, e.g. '{shown}{suffixes[0]}'",
            details={"path": shown, "expected": list(suffixes)},
        )
    if resolved.suffix.lower() in suffixes:
        return resolved
    suffix = resolved.suffix.lower()
    if mode == "read":
        hint = f"Name an existing {expected} file; nx_list_files shows the folder"
    elif len(suffix_kinds(suffixes)) == 1:
        hint = f"End the path with {suffixes[0]}, or leave the suffix out"
    else:
        hint = f"End the path with one of {expected}"
    fail(
        "NX_UNSUPPORTED_FILE_TYPE",
        f"{field}: {getattr(spec, 'name', 'the tool')} takes a {expected} file, not {suffix} "
        f"('{shown}')",
        field=field,
        hint=hint,
        retry="after_fix",
        details={"path": shown, "expected": list(suffixes)},
    )


def _gate_evidence(spec, checked: Checked, services: Services, top=False) -> None:  # gate 3
    """Evidence (``core/evidence.py``): tested here, or tested on another build and allowed
    here. ``top``: a top-level call, which on an untested build runs NX's undo self-test
    first when it is the session's first changing call (:func:`_self_test`)."""
    key, dev = evidence.build_key(services.build), bool(services.dev)
    refusal = evidence.check(spec, key, checked.args, dev=dev)
    changing = top and spec.effect not in evidence.READ_EFFECTS
    if refusal is None and changing and _self_test(spec, services):
        refusal = evidence.check(spec, key, checked.args, dev=dev)  # failed: reads only
    if refusal is None and not dev:
        # Session version memory: a tool that met a missing NXOpen member on this build.
        refusal = evidence.version_refusal(spec, checked.args)
    if refusal is None:
        # Session license memory: a tested tool/variant that NX refused for a missing
        # license is refused before NX for 15 minutes (also in development mode).
        refusal = evidence.license_refusal(spec, checked.args)
    if refusal is not None:
        raise refusal


def _remember_license(spec, args, services: Services, error) -> None:
    with contextlib.suppress(Exception):
        evidence.remember_license(spec, services.build, args, error)


def _self_test(spec, services: Services) -> bool:
    """NX's undo self-test (``Services.self_test(tx)``: a block made and undone in a scratch
    part, never in the engineer's), once per NX session on an untested build, before its
    first changing call; the result is kept with the session memory
    (``evidence.self_test_result``) and a failure leaves read and view tools only.
    Returns whether it ran now."""
    hook = getattr(services, "self_test", None)
    if not callable(hook) or evidence.self_test_result() is not None:
        return False
    if evidence.known_build(evidence.build_key(services.build)):
        return False
    started = time.monotonic()
    try:
        result = dict(hook(Tx(services, spec, operation_id=None)) or {})
    except Exception as error:
        result = {"state": "failed", "reason": f"{type(error).__name__}: {error}"}
    if result.get("state") not in ("passed", "failed", "skipped"):
        result["state"] = "failed"
    result.setdefault("reason", None)
    result["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    result["seconds"] = round(time.monotonic() - started, 3)
    evidence.set_self_test(result)
    return True


def _version_net(spec, args, services: Services, exc, error: NXError) -> NXError:
    """Gate 15: a tool without evidence on this build that met a missing NXOpen member
    (an AttributeError on an NXOpen object, class or module, NX itself not refusing) is
    ``NX_VERSION_UNSUPPORTED`` and is remembered for the NX session (not in development
    mode); so is ``kit.builders.first`` finding none of its names. On a build where the
    tool is tested it stays the internal error it is (our bug)."""
    try:
        details = error.details
        if details.get("child_tool") or details.get("remembered_from"):
            return error  # a batch step (its own spec remembered it), or a refusal
        variant = spec.variant_of(args if isinstance(args, dict) else {})
        if evidence.available(spec, evidence.build_key(services.build), variant):
            return error
        if error.code == "NX_VERSION_UNSUPPORTED":
            member = details.get("member")  # kit.builders.first
            if member:
                details.setdefault("tool", spec.name)
                details.setdefault("variant", variant)
                details.setdefault("build", services.build)
                if not services.dev:
                    evidence.remember_version(
                        spec, services.build, variant, member, error.message, "MissingFactory"
                    )
            return error
        member = missing_member(exc)
        if member is None or error.nx_code is not None:
            return error
        found = evidence.version_unsupported(spec, services.build, variant, member)
        found.trace = error.trace
        if not services.dev:
            evidence.remember_version(
                spec, services.build, variant, member, error.message, type(exc).__name__
            )
        return found
    except Exception:
        return error


def _gate_requires(spec, services: Services, *, explicit: bool = False) -> None:  # gate 4
    """The tool's ``requires``. ``explicit``: the call named its part (``part``), so a
    part-level refusal (not an assembly) points at ``/part``."""
    if not spec.requires:
        return
    state = services.state()
    for item in spec.requires:
        if item.startswith("application:"):
            wanted = item.split(":", 1)[1]
            if state.get("application") != wanted:
                fail(
                    "NX_WRONG_APPLICATION",
                    f"{spec.name} needs the {wanted} application",
                    hint="Switch with nx_activate_application",
                    details={"application": state.get("application")},
                )
            continue
        if item == "no_active_sketch":  # NX's active sketch, or the target's parked one
            active = bool(state.get("active_sketch")) and not _release_drafting(services)
            ok = not active and not parking.open_in(services, services.work_part())
        else:
            ok = bool(state.get(item))
        if not ok:
            code, message, hint = REQUIRE_FAILURES[item]
            if item == "assembly":
                with contextlib.suppress(Exception):
                    name = targets.file_name(services.work_part())
                    message = f"{name} has no components yet, so it is not an assembly"
                    hint = (
                        "Add a component with nx_create_component, or name the assembly with "
                        "part (nx_list_parts lists the loaded parts)"
                    )
                fail(code, message, hint=hint, field="/part" if explicit else None)
            fail(code, message, hint=hint)


def _release_drafting(services: Services) -> bool:
    """Gate 4 ``no_active_sketch``: an active drafting (sheet) sketch does not block.
    NX re-activates a sheet's last sheet sketch on opening the sheet; the services
    deactivate it quietly (``release_drafting_sketch``) and the call goes on. A model
    sketch stays active and still refuses. Services without the seam release nothing."""
    release = getattr(services, "release_drafting_sketch", None)
    if release is None:
        return False
    try:
        return bool(release())
    except Exception:
        return False  # it stays active: NX_SKETCH_ACTIVE as before


def _gate_ownership(spec, services: Services, args: dict) -> dict | None:  # gate 6 (before)
    """Refuse a model change of a part the agent does not own; return IsModified flags.

    A call given its ``new_part`` path works in a part it creates (agent-owned), so
    the current work part is not checked; the after-handler check still undoes any
    change to a part the agent does not own. Development mode does not turn it off.
    """
    if spec.effect not in ("model", "file"):
        return None
    in_new_part = spec.new_part is not None and args.get(spec.new_part) is not None
    if spec.effect == "model" and not in_new_part:
        check_owned(services, services.work_part())
    return services.modified_flags()


def _check_ownership_after(services: Services, flags: dict | None) -> None:  # gate 6 (after)
    if flags is None:
        return
    after = services.modified_flags()
    changed = sorted(k for k, was in flags.items() if not was and after.get(k))
    if changed:
        fail(
            "NX_PART_NOT_GRANTED",
            "The call modified a part the agent does not own; it was undone",
            hint="Ask the engineer in chat to allow that part on the NX MCP panel",
            details={"parts": changed},
        )


def sheet_target(spec, args: dict, refs, services: Services) -> tuple:
    """``(sheet, units)``: the drawing sheet a sheet tool's arguments name, and its units.

    In order: the first reference argument that is a drawing sheet or a view on
    one (``services.sheet_of``); a ``sheet`` or ``view`` argument given by name; for a
    tool that creates a sheet, its top-level
    ``units`` argument (``(None, "mm")``); the work part's current sheet. Returns
    ``(None, None)`` when nothing names a sheet. Never raises: an unresolvable
    reference is left to the handler, which reports it.
    """
    for path, kinds in refs:
        if kinds and not expand_kinds(kinds) & SHEET_KINDS:
            continue
        value = get_at(args, path)
        if not isinstance(value, str):
            continue
        try:
            sheet = services.sheet_of(services.references.resolve(value, kinds))
            if sheet is not None:
                return sheet, services.sheet_units(sheet)
        except Exception:
            continue
    named = _named_sheet(spec, args, services)
    if named is not None:
        return named, services.sheet_units(named)
    units = args.get("units")
    if "units" in spec.params and units in ("mm", "in"):
        return None, units
    try:
        sheet = services.current_sheet()
        if sheet is not None:
            return sheet, services.sheet_units(sheet)
    except Exception:
        pass
    return None, None


# Arguments that may name a drawing sheet or view by its name instead of a ref.
SHEET_NAME_ARGS = (("sheet", "DrawingSheets"), ("view", "DraftingViews"))


def _named_sheet(spec, args: dict, services: Services):
    """The drawing sheet a ``sheet`` or ``view`` argument names by its name (any case; a
    view 'TOP@1' or 'top'), in the work part; None when none does or it is ambiguous (the
    handler reports a bad name)."""
    with contextlib.suppress(Exception):
        part = services.work_part()
        for name, collection in SHEET_NAME_ARGS:
            text = args.get(name) if name in spec.params else None
            if not isinstance(text, str) or not text.strip() or ":" in text:
                continue  # a ref was looked up above
            wanted = text.strip().casefold()
            found = [
                obj
                for obj in getattr(part, collection, None) or []
                if str(getattr(obj, "Name", "")).casefold() == wanted
                or (
                    name == "view"
                    and str(getattr(obj, "Name", "")).casefold().split("@")[0] == wanted
                )
            ]
            if len(found) == 1:
                sheet = services.sheet_of(found[0])
                if sheet is not None:
                    return sheet
    return None


def _gate_units(spec, checked: Checked, services: Services, tx: Tx) -> dict:  # gate 9
    """Convert quantities: lengths to part units, sheet lengths to the target sheet's
    units (part units when no sheet is named), angles to degrees. An NX expression name
    or formula is evaluated by ``services.evaluate_formula`` (a formula only for model
    tools: it needs a scratch expression) and becomes a ``types.Formula``."""
    part = services.work_part()
    length = services.part_units(part) if part is not None else None
    tx.units = {"length": length, "angle": "deg"}
    sheet_units = None
    if spec.sheet:
        tx.sheet, sheet_units = sheet_target(spec, checked.args, checked.refs, services)
        sheet_units = sheet_units or length or "mm"
        tx.units["sheet_length"] = sheet_units

    def evaluate(formula, dim, unit, field=None):
        return services.evaluate_formula(
            formula, dim, unit, field=field, scratch=spec.effect == "model"
        )

    for path, dim in checked.quantities:
        if dim == "sheet_length":
            target = sheet_units or length or "mm"
        elif dim == "angle":
            target = "deg"
        elif dim in ("count", "scalar"):
            target = "unitless"
        elif dim == "mass":
            target = "kg"
        else:
            target = length or "mm"
        where = pointer(path)
        try:
            value = convert_quantity(get_at(checked.args, path), dim, target, evaluate, where)
        except ValueError as error:
            fail("NX_INVALID_ARGUMENT", f"{where}: {error}", field=where)
        set_at(checked.args, path, value)
    for path, bounds in checked.bounds:  # a unit string or formula, now converted
        where = pointer(path)
        value = get_at(checked.args, path)
        broken = range_error(value, bounds)
        if broken is not None:
            unit = tx.units.get(dict(checked.quantities).get(path, "")) or ""
            got = f"{float(value):g} {unit}".strip()
            fail("NX_INVALID_ARGUMENT", f"{where}: {broken} (got {got})", field=where)
    for path, canonical in checked.forms:  # an axis-angle pose -> {translation, rotation}
        try:
            set_at(checked.args, path, canonical(get_at(checked.args, path)))
        except (ValueError, KeyError, TypeError) as error:
            fail("NX_INVALID_ARGUMENT", f"{pointer(path)}: {error}", field=pointer(path))
    return checked.args


def _resolve_placeholders(checked: Checked, prior: list) -> None:
    """Fill ``"$<i>.<path>"`` from child ``i``'s result envelope; ``$0.<key>`` with a key
    that is not an envelope key means ``$0.data.<key>`` (``$0.sketch``). An error lists the
    keys there are."""
    for path, text in checked.placeholders:
        index, *keys = text[1:].split(".")
        value: Any = None
        try:
            value = prior[int(index)]
            data = value.get("data") if isinstance(value, dict) else None
            if keys and keys[0] not in value and isinstance(data, dict) and keys[0] in data:
                keys = ["data", *keys]  # "$0.sketch" is "$0.data.sketch"
            for key in keys:
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (TypeError, IndexError, KeyError, ValueError):
            available = _placeholder_keys(prior, index, keys)
            fail(
                "NX_INVALID_ARGUMENT",
                f"{pointer(path)}: {text} does not name an earlier result"
                + (f"; there: {', '.join(available)}" if available else ""),
                field=pointer(path),
                hint="Name an earlier step and a key of its result, e.g. '$0.data.feature' "
                "(the step's data keys are listed in the message)",
                details={"available": available},
            )
        if isinstance(value, dict) and "id" in value:
            value = value["id"]
        set_at(checked.args, path, value)


def _placeholder_keys(prior: list, index: str, keys: list) -> list[str]:
    """The keys a placeholder could take at the point where it failed: the step's data
    keys (``$0.<key>``), or the keys at the deepest level it reached."""
    try:
        node: Any = prior[int(index)]
    except (TypeError, IndexError, ValueError):
        return [f"${i}" for i in range(len(prior))]
    data = node.get("data") if isinstance(node, dict) else None
    level = data if isinstance(data, dict) else node
    for key in keys[1:] if keys[:1] == ["data"] else keys:
        nxt = None
        if isinstance(level, dict):
            nxt = level.get(key)
        elif isinstance(level, list) and key.isdigit() and int(key) < len(level):
            nxt = level[int(key)]
        if not isinstance(nxt, (dict, list)):
            break
        level = nxt
    if isinstance(level, dict):
        return sorted(str(k) for k in level)[:30]
    if isinstance(level, list):
        return [str(i) for i in range(min(len(level), 30))]
    return []


def _internal(row) -> bool:
    """An unnamed internal NX object without a short ref (``ENTITY 3 1 1``: a sketch's
    helper curve, a pattern's own constraint): counted, not listed one by one."""
    return ":" not in str(row.get("id") or "") and bool(_GENERIC.match(row.get("name") or ""))


def _changes(
    services: Services,
    before: dict,
    after: dict,
    spec,
    refreshed=(),
    *,
    retired=None,
    floor: int | None = None,
    tx: Tx | None = None,
) -> dict:
    """``changes`` of a committed call: created, modified and deleted rows and the
    earlier refs that went stale (``stale_refs``: deleted objects, consumed faces and
    edges). Every other ref stays valid."""
    created = [services.describe(after[k], live=True) for k in after if k not in before]
    deleted = [services.describe(before[k], live=False) for k in before if k not in after]
    internal = {
        "created": sum(map(_internal, created)),
        "deleted": sum(map(_internal, deleted)),
    }
    created = [r for r in created if not _internal(r)]
    deleted = [r for r in deleted if not _internal(r)]
    modified: list = list(refreshed)
    for row in tx.changed if tx is not None else ():
        if all(r.get("id") != row.get("id") for r in modified):
            modified.append(row)
    stale: list[str] = list(tx.stale_refs) if tx is not None else []
    stale_of = getattr(getattr(services, "references", None), "stale", None)
    if retired and stale_of is not None:
        with contextlib.suppress(Exception):
            stale.extend(r for r in stale_of(retired, floor) if r not in stale)
    if tx is not None and tx.children:  # a batch or an accepted preview: its steps' rows
        _merge_children(tx.children, created, modified, deleted, stale, floor)
    changes: dict = {
        "created": created,
        "modified": modified,
        "deleted": deleted,
        "references": "listed" if deleted or stale else "preserved",
    }
    if stale:
        changes["stale_refs"] = stale[:50]
        changes["stale_references"] = len(stale)
    if any(internal.values()):
        changes["internal"] = {k: v for k, v in internal.items() if v}
    if retired is None and spec.invalidates == "topology":
        # Services that cannot say which faces and edges died retired them all.
        changes["references"] = "listed"
        changes["stale_kinds"] = ["face", "edge"]
    return changes


def _number(row) -> int | None:
    """The number of a row's short ref (``c:825493`` -> 825493), else None."""
    ref = str(row.get("id") or "") if isinstance(row, dict) else ""
    head, _, tail = ref.partition(":")
    return int(tail) if head and tail.isdigit() else None


def _merge_children(children, created, modified, deleted, stale, floor) -> None:
    """Add the rows of a batch's or preview's steps to the call's own ``changes``: what a
    step created and no later step deleted, what a step changed, and what a step deleted
    or made stale that existed before the call (a step's own invalidation already retired
    those refs, so the call's snapshot comparison alone misses them)."""
    seen = {r.get("id") for rows in (created, modified, deleted) for r in rows}
    gone_ids = {
        r.get("id")
        for child in children
        for r in (child.get("changes") or {}).get("deleted") or []
        if isinstance(r, dict)
    }

    def older(row) -> bool:
        number = _number(row)
        return floor is None or (number is not None and number < floor)

    for child in children:
        changes = child.get("changes") or {}
        for key, rows in (("created", created), ("modified", modified), ("deleted", deleted)):
            for row in changes.get(key) or []:
                if not isinstance(row, dict) or not row.get("id") or row["id"] in seen:
                    continue
                if key == "created" and row["id"] in gone_ids:
                    continue  # made and deleted inside the call
                if key == "deleted" and not older(row):
                    continue
                if key == "modified" and row["id"] in gone_ids:
                    continue
                rows.append(row)
                seen.add(row["id"])
        for ref in changes.get("stale_refs") or []:
            number = _number({"id": ref})
            if ref not in stale and (floor is None or (number is not None and number < floor)):
                stale.append(ref)


def _readback(spec, services: Services, part, before: dict, mark) -> dict:  # gate 11
    after = services.snapshot(part) if part is not None else {}
    failed = services.feature_errors(part, before, mark) if part is not None else []
    if failed:
        raise NXError(
            "NX_UPDATE_FAILED",
            "A new feature failed to update: " + "; ".join(f.get("message", "") for f in failed),
            details={"features": failed},
        )
    return after


def _readback_parts(spec, services: Services, tx: Tx, part, before: dict, mark) -> tuple:
    """Gate 11 on the work part and on every part the handler declared with
    ``tx.touch_part``: ``(before, after)`` snapshots merged over those parts (keys are
    session-unique NX tags)."""
    after = _readback(spec, services, part, before, mark)
    if not tx.extra_parts:
        return before, after
    before, after = dict(before), dict(after)
    for extra, extra_before in tx.extra_parts.values():
        before.update(extra_before)
        after.update(_readback(spec, services, extra, extra_before, mark))
    return before, after


# ---------------------------------------------------------------------------
# NX feature alerts (gate 11 and gate 15)

# NXOpen ``Feature.GetFeature<kind>Messages`` readers of the alerts a call compares.
ALERT_READERS = (("error", "GetFeatureErrorMessages"), ("warning", "GetFeatureWarningMessages"))
# Parts with more features skip the comparison (two reads per feature before and after).
ALERT_FEATURES_MAX = 4000
ALERTS_LISTED = 10


def feature_messages(
    feature, kinds=("error", "warning"), problems: list | None = None
) -> list[tuple[str, str]]:
    """``[(kind, message)]`` NX attaches to ``feature`` (``error``, ``warning``,
    ``information``), in order without repeats; ``[]`` where NX cannot say (``problems``
    gets the reader that could not answer, once). Never raises."""
    readers = dict(ALERT_READERS, information="GetFeatureInformationalMessages")
    out: list[tuple[str, str]] = []
    for kind in kinds:
        read = getattr(feature, readers[kind], None)
        try:
            if not callable(read):
                raise AttributeError(readers[kind])
            messages = list(read() or [])
        except Exception:  # a feature NX cannot answer for has no alerts to report
            messages = []
            if problems is not None and readers[kind] not in problems:
                problems.append(readers[kind])
        for message in messages:
            text = str(message).strip()
            if text and (kind, text) not in out:
                out.append((kind, text))
    return out


def feature_alerts(services, part, problems: list | None = None) -> dict | None:
    """``{tag: (feature, frozenset((kind, message)))}`` of the user-visible features of
    ``part`` that report errors or warnings; None when they cannot be read (no part, more
    than ``ALERT_FEATURES_MAX`` features). Services may supply ``feature_alerts(part)``.
    ``problems`` gets the readers NX could not answer (:func:`feature_messages`)."""
    hook = getattr(services, "feature_alerts", None)
    if callable(hook):
        try:
            return hook(part)
        except Exception:
            return None
    try:
        features = list(part.Features)
    except Exception:
        return None
    if len(features) > ALERT_FEATURES_MAX:
        return None
    out: dict = {}
    for feature in features:
        if getattr(feature, "IsInternal", False):
            continue
        pairs = frozenset(feature_messages(feature, problems=problems))
        if pairs:
            with contextlib.suppress(Exception):
                out[int(feature.Tag)] = (feature, pairs)
    return out


def _feature_row(services, feature) -> dict:
    try:
        return dict(services.references.row(feature, "feature"))
    except Exception:
        name = str(getattr(feature, "JournalIdentifier", "") or getattr(feature, "Name", ""))
        return {"kind": "feature", "name": name}


def new_alerts(services, part, before: dict | None, problems: list | None = None) -> list[dict]:
    """``[{feature: row, kind, message}]``: the feature errors and warnings of ``part``
    that were not there before the call (``before`` from :func:`feature_alerts`), at most
    ``ALERTS_LISTED``. ``[]`` when ``before`` is None."""
    if before is None or part is None:
        return []
    after = feature_alerts(services, part, problems)
    if after is None and problems is not None and "Features" not in problems:
        problems.append("Features")
    if not after:
        return []
    out: list[dict] = []
    for tag, (feature, pairs) in after.items():
        old = before.get(tag, (None, frozenset()))[1]
        fresh = sorted(pairs - old)
        if not fresh:
            continue
        row = _feature_row(services, feature)
        out.extend({"feature": row, "kind": kind, "message": text} for kind, text in fresh)
        if len(out) >= ALERTS_LISTED:
            break
    return out[:ALERTS_LISTED]


def _alert_text(alert: dict) -> str:
    row = alert["feature"]
    who = row.get("name") or "a feature"
    if row.get("id"):
        who += f" ({row['id']})"
    return f"{who}: {alert['message']}"


def _alert_warning(alert: dict) -> dict:
    """A result warning for a new NX feature alert: the feature row is ``object``."""
    word = "error" if alert["kind"] == "error" else "warning"
    return {
        "code": "NX_UPDATE_WARNING",
        "message": f"NX reports a new feature {word}: {_alert_text(alert)}",
        "object": alert["feature"],
    }


def update_failures(services, problems: list | None = None) -> list[dict]:
    """``[{feature: row, message, nx_code}]`` of NX's update error list (``UpdateManager.
    ErrorList``): after "Update undo happened" it names the feature whose update failed
    (NX 2406 measured: the blend, when a block edit left its edge too short). Read before
    the rollback clears the list; ``[]`` where NX cannot say (``problems`` gets
    ``UpdateManager.ErrorList``)."""
    out: list[dict] = []
    try:
        errors = services.session.UpdateManager.ErrorList
        count = int(errors.Length)
    except Exception:
        if problems is not None:
            problems.append("UpdateManager.ErrorList")
        return out
    with contextlib.suppress(Exception):
        for i in range(min(count, ALERTS_LISTED)):
            with contextlib.suppress(Exception):
                info = errors.GetErrorInfo(i)
                obj = info.ErrorObject
                row = _feature_row(services, obj) if obj is not None else {}
                if not row.get("name"):
                    row["name"] = str(getattr(info, "ErrorObjectDescription", "") or "")
                item: dict[str, Any] = {
                    "feature": row,
                    "message": str(info.Description or "").strip(),
                }
                with contextlib.suppress(Exception):
                    item["nx_code"] = int(info.ErrorCode)
                out.append(item)
    return out


def _unchanged_measure(details: dict) -> bool:
    """A read-back failure whose measured value did not move (``NX left the volume 0 ->
    0``): NX built nothing that counts for the request."""
    actual = (details or {}).get("actual")
    if not isinstance(actual, dict) or not {"before", "after"} <= set(actual):
        return False
    try:
        old, new = float(actual["before"]), float(actual["after"])
    except (TypeError, ValueError):
        return False
    return abs(new - old) <= 1e-9 * max(1.0, abs(old))


UNVERIFIED = (
    "{build} is not tested with NX MCP and NX could not answer {what}: the result is not "
    "verified; check it (nx_check_model, measurements)"
)


def unverified(services) -> bool:
    """Whether the call runs on an untested NX build, where a check NX cannot answer is
    reported (``NX_UNVERIFIED``) instead of passing silently (fail closed)."""
    try:
        return not evidence.known_build(evidence.build_key(services.build))
    except Exception:
        return False


def _unverified_warning(services, problems: list) -> dict | None:
    """The ``NX_UNVERIFIED`` warning for the readers in ``problems`` on an untested build."""
    if not problems or not unverified(services):
        return None
    what = ", ".join(dict.fromkeys(str(p) for p in problems))
    message = UNVERIFIED.format(build=evidence.build_label(services.build), what=what)
    return {"code": "NX_UNVERIFIED", "message": message}


def explain_failure(
    services, part, before_alerts: dict | None, error: NXError, problems: list | None = None
) -> NXError:
    """Gate 15, before the rollback: say what NX reported about the failed call.

    An update failure names its failing features (``details.failed_features``: NX's update
    error list, else new feature errors). A read-back mismatch (``NX_VERIFICATION_FAILED``)
    that NX explained with new feature alerts, or that left the measured value unchanged,
    is ``NX_BUILD_FAILED`` (``geometry_failed``, fix the input) instead of a report to the
    engineer. Never raises."""
    try:
        alerts = new_alerts(services, part, before_alerts, problems)
        if error.code == "NX_UPDATE_FAILED" and "failed_features" not in error.details:
            failed = update_failures(services, problems) or [
                {"feature": a["feature"], "message": a["message"]}
                for a in alerts
                if a["kind"] == "error"
            ]
            if failed:
                error.details["failed_features"] = failed
                names = ", ".join(_alert_text(f) for f in failed[:3])
                error.message = f"{error.message.rstrip('.')}. Failing: {names}"
                error.hint = error.hint or (
                    "The model was rolled back. Change the input so the features in "
                    "details.failed_features can rebuild (or edit them first); do not "
                    "repeat unchanged"
                )
            return error
        if error.code != "NX_VERIFICATION_FAILED":
            if alerts:
                error.details.setdefault("nx_alerts", alerts)
            return error
        if not alerts and not _unchanged_measure(error.details):
            return error
        said = "; ".join(_alert_text(a) for a in alerts[:3])
        message = f"NX did not build what was asked: {error.message}"
        if said:
            message += f" (NX: {said})"
        details = {**error.details, "verification": error.message}
        if alerts:
            details["nx_alerts"] = alerts
        return NXError(
            "NX_BUILD_FAILED",
            message,
            field=error.field,
            hint="The model was rolled back. Check the input NX's message or the check "
            "names (a closed profile, faces that meet, a valid order) and change it; do "
            "not repeat unchanged",
            nx_code=error.nx_code,
            details=details,
        )
    except Exception:
        return error


def _no_effect(tx: Tx, changes: dict | None) -> bool:
    """A committed call that warned ``NX_NO_EFFECT`` and changed nothing (no created,
    modified or deleted object, no stale ref, no file): ``not_applicable``, not
    ``committed``."""
    if not any(w.get("code") == "NX_NO_EFFECT" for w in tx.warnings):
        return False
    if tx.files.published or tx.changed or tx.children:
        return False
    if changes is None:
        return True
    return not any(changes.get(k) for k in ("created", "modified", "deleted", "stale_refs"))


def _extra_unchanged(services: Services, tx: Tx | None) -> bool:
    """Whether every part declared with ``tx.touch_part`` has the objects it had."""
    for extra, extra_before in tx.extra_parts.values() if tx is not None else ():
        if set(services.snapshot(extra)) != set(extra_before):
            return False
    return True


def _refresh(spec, services: Services, tx: Tx, bound=None) -> list:  # gate 13
    """Managed annotations after a commit, inside the call's undo mark: every
    ``@refresher`` hook of every domain. On a switched target that is not displayed the
    display-bound ones wait (gate 4s, ``Bound.defers``) and an ``NX_NOTE`` says so; a
    displayed part they waited for gets them on its next model call, drawing calls too."""
    key = tx.target.key if tx.target is not None else None
    waiting = switching.deferred(services)
    if not spec.refresh_annotations and (
        key not in waiting or not switching.displayed(services, tx.target.part)
    ):
        return []
    rows: list = []
    skipped = False
    for kind, refresh in sorted(services.refreshers().items()):
        if bound is not None and bound.defers(kind):
            skipped = True
            continue
        if not spec.refresh_annotations and kind not in switching.DISPLAY_REFRESHERS:
            continue
        tx.checkpoint()
        rows.extend(refresh(tx, None) or [])
    if skipped:
        part = tx.target.part if tx.target is not None else None
        if part is not None and switching.has_drafting(part):
            tx.warn("NX_NOTE", switching.DEFERRED_NOTE.format(file=tx.target.file))
    elif key is not None:
        waiting.discard(key)
    return rows


# ---------------------------------------------------------------------------
# Top-level call


class _Phases(dict):
    """Seconds a call spent per kernel phase, kept in its receipt (``phases``) so a slow
    call shows where the time went: gates, snapshot, handler, readback, invalidate,
    refresh, changes."""

    def __init__(self) -> None:
        super().__init__()
        self._at = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self[name] = round(self.get(name, 0.0) + now - self._at, 3)
        self._at = now


def run(spec, args, ctx: Context) -> dict:
    services = ctx.services
    operation_id = ctx.operation_id or new_operation_id()
    receipts = Receipts(services.store, services.session_id)
    envelope: dict = {"tool": spec.name, "operation_id": operation_id}
    tx: Tx | None = None
    record: dict | None = None
    target = None
    census = None
    mark = None
    part = None
    before: dict = {}
    floor: int | None = None
    observer = None
    watched = None
    alerts: dict | None = None  # NX feature alerts before the handler (gate 11)
    unanswered: list = []  # NX readers that could not answer (NX_UNVERIFIED)
    bound: switching.Bound | None = None
    handler_started = handler_done = False
    args, keys = targets.split_call_keys(spec, args)  # part/expires_at in arguments
    part_text = keys.get("part", ctx.part)
    caller = targets.check_caller(keys.get("caller", ctx.caller))
    raw_args = copy.deepcopy(args) if isinstance(args, dict) else args
    phases = _Phases()
    busy.adopt(services)  # agent parts of this NX process after a host reload
    try:
        expires = targets.check_call_keys(part_text, keys.get("expires_at", ctx.expires_at))
        targets.check_expiry(spec, expires)  # the ghost guard: no receipt, not_started
        try:
            checked = _gate_schema(
                spec, args, where=getattr(services, "runs", "nx"), services=services
            )  # 1
        except NXError as schema_error:
            _application_first(spec, services, schema_error)
            raise
        try:
            target = targets.resolve(spec, checked, services, part=part_text, parent=ctx.parent)
        except NXError:  # 3t: a repeat of a finished call still replays (its own part)
            if spec.effect != "read" and ctx.operation_id:
                replay = receipts.replay(spec.name, raw_args, operation_id, ANY_PART)
                if replay is not None:
                    return replay
            raise
        file_key = target.file_key if target is not None else None
        if spec.effect != "read" and ctx.operation_id:
            # Gate 7's replay goes first: repeating a committed call must not fail on
            # what it created (a `new` path that now exists, a part it loaded).
            replay = receipts.replay(spec.name, raw_args, operation_id, file_key)
            if replay is not None:
                return replay
        _gate_refs(spec, checked, services)  # 3r
        _gate_paths(spec, checked, services, target)  # 2
        _gate_variant(spec, checked, services, target)  # 3
        _gate_evidence(spec, checked, services, True)  # 3 (top: the self-test may run)
        bound = switching.enter(  # 4s: the target becomes current (core/switching.py)
            spec, checked.args, target, services, explicit=part_text is not None
        )
        _gate_requires(spec, services, explicit=part_text is not None)  # 4
        flags = _gate_ownership(spec, services, checked.args)  # 6
        part = services.work_part()
        if spec.effect != "read":  # 7
            record, replay = receipts.begin(
                spec.name,
                raw_args,
                operation_id,
                services.part_key(part) if part else None,
                part_file=file_key,
            )
            if replay is not None:
                return replay
            if record is not None and caller is not None:  # who sent it (cancel, status)
                record["caller"] = caller["id"]
        if spec.effect != "read":  # loads and dropped undo marks (core/marks.py)
            watched = undo_marks.watch(services)
            census = targets.census(services)  # the agent's loaded parts (gate 14 note)
        if spec.effect == "model":  # 8
            phases.lap("gates")
            before = services.snapshot(part) if part is not None else {}
            alerts = feature_alerts(services, part, unanswered)
            if alerts is None and part is not None:
                unanswered.append("Features")
            floor = _ref_floor(services)
            mark = services.set_mark("NX MCP: " + spec.label)
            phases.lap("snapshot")
        tx = Tx(
            services,
            spec,
            operation_id=operation_id,
            receipts=receipts,
            record=record,
            arguments=raw_args,
        )
        tx.mark = mark
        tx.target = target
        tx.bound = bound
        tx.caller = caller
        for code, message in bound.notes:
            tx.warn(code, message)
        if record is not None and record.get("previous"):
            tx.warn(
                "NX_UPDATE_WARNING",
                f"{operation_id} was reverted by {record['previous']['reverted_by']}; this "
                "call ran it again (nx_get_operation lists the reverted run under previous)",
            )
        call_args = _gate_units(spec, checked, services, tx)  # 9
        handler_started = True
        with services.observe() as observer:
            data = spec.handler(tx, **call_args)  # 10
        handler_done = True  # Later failures roll back whatever the handler did.
        phases.lap("handler")
        if not isinstance(data, dict):
            fail("NX_ERROR", f"{spec.name} returned {type(data).__name__}, not an object")
        bound.check()  # 4s (after): the switched target is still the work part
        _check_ownership_after(services, flags)  # 6 (after)
        changes = None
        if spec.effect == "model":
            seen, after = _readback_parts(spec, services, tx, part, before, mark)  # 11
            for alert in new_alerts(services, part, alerts, unanswered):  # NX's new alerts
                if not any(alert["message"] in str(w.get("message")) for w in tx.warnings):
                    tx.warnings.append(_alert_warning(alert))
            unchecked = _unverified_warning(services, unanswered)
            if unchecked is not None:  # an untested build: never a silent pass
                tx.warnings.append(unchecked)
            phases.lap("readback")
            retired = services.invalidate(seen, after, spec.invalidates == "topology")  # 12
            phases.lap("invalidate")
            refreshed = _refresh(spec, services, tx, bound)  # 13
            phases.lap("refresh")
            changes = _changes(
                services, seen, after, spec, refreshed, retired=retired, floor=floor, tx=tx
            )
            phases.lap("changes")
            if tx.references == "part_invalidated":  # the handler invalidated the part
                changes["references"] = tx.references
                changes.pop("stale_kinds", None)
            if tx.outcome is None and _no_effect(tx, changes):  # changed nothing, said so
                tx.outcome = "not_applicable"
                with contextlib.suppress(Exception):
                    services.delete_mark(mark)
            tx.changes = changes  # the undo-history entry's notes read it (core/marks.py)
            if tx.outcome != "not_applicable":  # a discarded preview: no mark to undo
                _record(services, tx, mark, part, operation_id, spec.name)
        elif tx.changed:  # a file or session tool reporting the objects it changed
            changes = {"created": [], "modified": list(tx.changed), "deleted": []}
            changes["references"] = "preserved"
        elif spec.effect != "read" and tx.outcome is None and _no_effect(tx, None):
            tx.outcome = "not_applicable"
        undo_marks.settle(services, watched, spec, operation_id, tx)
        note = targets.load_note(services, census)
        if note is not None:  # the call brought the agent's loaded parts to two
            tx.warn("NX_NOTE", note)
        if not bound.switched:  # a switched call's work part comes back: not a change
            _work_part_units(services, part, tx)
        envelope.update(ok=True)  # 14
        envelope["mutation_outcome"] = tx.outcome or (
            "not_applicable" if spec.effect == "read" else "committed"
        )
        if tx.units.get("length"):
            envelope["units"] = {k: v for k, v in tx.units.items() if v}
        envelope["frame"] = tx.frame
        page = data.pop("page", None) if isinstance(data.get("page"), dict) else None
        envelope["data"] = data
        if changes is not None:
            envelope["changes"] = changes
        envelope["warnings"] = tx.warnings
        if page is not None:
            envelope["page"] = page
        _report_part(envelope, target, services, part_text)
        if envelope["mutation_outcome"] == "committed":  # the parts it worked in are busy
            busy.record(services, spec, target, tx, census[0] if census else None)
        if spec.restorable:
            restore_id = tx.display.snapshot()
            if restore_id is not None:
                envelope["restore_id"] = restore_id
        _leave(bound, envelope)  # 4s: the engineer's display and work parts come back
        services.after_commit(spec)
        _bump_parts(services, spec, tx, target, envelope["mutation_outcome"])
        phases.lap("finish")
        if record is not None:
            record["phases"] = dict(phases)
        receipts.finish(record, envelope)
        return envelope
    except BaseException as exc:  # 15 (SystemExit/KeyboardInterrupt from a handler too)
        error = classify(exc)
        error = _version_net(spec, raw_args, services, exc, error)
        _remember_license(spec, raw_args, services, error)
        if spec.effect == "model" and handler_started and part is not None:
            # before the rollback
            error = explain_failure(services, part, alerts, error, unanswered)
            unchecked = _unverified_warning(services, unanswered)
            if unchecked is not None and tx is not None:
                tx.warnings.append(unchecked)
        touched = bool(
            handler_started
            and (
                handler_done
                or (tx is not None and (tx.touched or bool(tx.children)))
                or (observer is not None and observer.touched)
            )
        )
        if (
            getattr(error, "unchanged", False)
            and not handler_done
            and not (tx is not None and tx.children)  # earlier children ran: roll back
            and _still_unchanged(spec, services, tx, part, before)
        ):
            touched = False  # raised inside tx.checks(): nothing was changed yet
        outcome, references, error = _settle(
            spec, services, tx, mark, part, before, error, touched, handler_started, floor
        )
        undo_marks.settle(services, watched, spec, operation_id, tx)
        envelope.update(ok=False, mutation_outcome=outcome, error=error.error_dict())
        if spec.effect == "model":
            envelope["changes"] = {"references": references}
            stale = error.details.get("stale_references")
            if isinstance(stale, int):
                envelope["changes"]["stale_references"] = stale
        if tx is not None and tx.warnings:
            envelope["warnings"] = tx.warnings
        elif bound is not None and bound.notes:
            envelope["warnings"] = [{"code": c, "message": m} for c, m in bound.notes]
        _leave(bound, envelope)  # 4s (after the rollback)
        _report_part(envelope, target, services, part_text)
        _after_failure(services, spec, outcome)
        _bump_parts(services, spec, tx, target, outcome)
        trace = getattr(error, "trace", None)
        receipts.finish(record, envelope, trace=trace)
        if trace:  # development only (NX_MCP_DEV=1); reads have no receipt
            receipts.log_trace(spec.name, operation_id, trace)
        return envelope
    finally:
        if bound is not None and not bound.left:  # a replay after the switch, or a crash
            with contextlib.suppress(Exception):
                bound.leave()
        if tx is not None:  # this call's staging folder, committed or not (Files)
            with contextlib.suppress(Exception):
                tx.files.discard()


def _leave(bound, envelope: dict) -> None:
    """Gate 4s, at the end of the call: restore what the switch changed; a restore that
    failed is a warning on the envelope (a committed change stays committed)."""
    if bound is None:
        return
    warning = bound.leave()
    if warning:
        warnings = envelope.setdefault("warnings", [])
        warnings.append({"code": "NX_UPDATE_WARNING", "message": warning})


def _report_part(envelope: dict, target, services, part_text) -> None:
    """Gate 14: ``part`` {id, file, from} when the call named its part or two or
    more parts are loaded."""
    if target is not None and target.reported(part_text is not None):
        with contextlib.suppress(Exception):
            envelope["part"] = target.row(services)


def _record(services, tx: Tx, mark, part, operation_id: str, tool: str) -> None:
    """Gate 14: the committed model call's undo-history entry (none when an undo or
    restore went back past its mark), with the handler's ``tx.history_note``. New work
    in a part ends that part's redo list; an undo by deletion (``compensation``) does not."""
    history = services.undo_history()
    depth = len(history)
    services.record_history(mark, part, operation_id, tool)
    if len(history) <= depth:
        return
    entry = history[-1]
    if tx.history_note:
        entry.update(tx.history_note)
    if entry.get("compensation"):
        return
    redo = services.redo_steps()
    key = entry.get("part_id")
    redo[:] = [item for item in redo if item.get("part_id") != key]


def _bump_parts(services, spec, tx: Tx | None, target, outcome) -> None:
    """The revision of the parts a call may have changed (``Services.part_revision``):
    its target and the parts it touched; a model call without a target moves them all.
    Reads, view changes, refusals and discarded previews move none."""
    if spec.effect in ("read", "view") or outcome in ("not_started", "not_applicable", None):
        return
    bump = getattr(services, "bump_part_revision", None)
    if bump is None:
        return
    keys: set = set(tx.extra_parts) if tx is not None else set()
    if target is not None:
        keys.add(target.key)
    if not keys and spec.effect == "model":
        keys.add(None)
    with contextlib.suppress(Exception):
        for key in keys:
            bump(key)


def _after_failure(services, spec, outcome: str) -> None:
    """``Services.after_failure`` with the outcome: a refusal moves no epoch."""
    try:
        cast(Any, services).after_failure(spec, outcome)
    except TypeError:  # services that take only the spec
        services.after_failure(spec)


def _work_part_units(services, part, tx: Tx) -> None:
    """Gate 14: when the call changed the work part (create, open, activate, close),
    ``units.length`` is the new work part's."""
    with contextlib.suppress(Exception):
        now = services.work_part()
        if now is None:
            return
        if part is None or services.part_key(now) != services.part_key(part):
            tx.units["length"] = services.part_units(now)


def _still_unchanged(spec, services, tx, part, before) -> bool:
    """A refusal from ``tx.checks()`` is ``not_started`` only if nothing changed: no
    published file and, for a model tool, the same objects as before the handler (a
    misplaced ``checks()`` after a change still rolls back)."""
    if tx is not None and tx.files.published:
        return False
    if spec.effect == "model" and part is not None:
        try:
            return set(services.snapshot(part)) == set(before) and _extra_unchanged(services, tx)
        except Exception:
            return False
    return True


def _mark_gone(services, mark) -> bool:
    """Whether NX already dropped the call's undo mark.

    NX can undo a failed NXOpen call itself, back to the call's mark, and remove that
    mark (NX 2406: a license refusal with "protected" error recovery, seen live). The
    model is then as before the call and there is nothing left to undo; ``UndoToMark``
    would fail with "Undo mark is missing". Unknown means present."""
    try:
        return not services.mark_exists(mark)
    except Exception:
        return False


def _ref_floor(services) -> int | None:
    """The next short-ref number (refs below it existed before the call), or None."""
    try:
        return int(services.references.issued())
    except Exception:
        return None


def rebind_after_undo(services, floor: int | None = None) -> dict:
    """After an undo: keep the refs of every object NX restored (NX 2406 undo restores
    the same tags), re-bind anchored refs whose object came back under a new tag and
    retire the rest. Never touches part refs of parts that are still open.

    ``{"rebound": [refs], "stale": [refs] | None}`` (``None``: the services retired
    every handle of the parts and cannot say which)."""
    retired = services.retire_undone(services.parts())
    rebind = getattr(getattr(services, "references", None), "rebind", None)
    if retired is None or rebind is None:
        return {"rebound": [], "stale": None}
    return rebind(retired, floor)


def _report_stale(error, summary: dict) -> str:
    """``changes.references`` after a rollback, and the error's explicit note of the
    refs that went stale (renumbered) or were re-bound."""
    stale, rebound = summary.get("stale"), summary.get("rebound") or []
    if rebound:
        error.details["rebound_references"] = len(rebound)
    if stale is None:
        error.details["references"] = "renumbered"
        error.details["stale_references"] = "all"
    elif stale:
        error.details["references"] = "renumbered"
        error.details["stale_references"] = len(stale)
        error.details["stale_refs"] = stale[:20]
    else:
        return "preserved"
    note = (
        "The rollback renumbered references of the part: list the objects again "
        "(nx_list_objects, nx_find_geometry) before reusing older refs."
    )
    base = error.hint or CATEGORIES[error.category][2]
    error.hint = f"{base.rstrip('.')}. {note}" if base else note
    return "part_invalidated"


def _settle(spec, services, tx, mark, part, before, error, touched, handler_started, floor=None):
    """``(mutation_outcome, references, error)`` after a failed call; rolls back."""
    if spec.effect == "read":
        outcome = "partial" if error.code == "NX_ROLLBACK_FAILED" else "not_applicable"
        return outcome, "preserved", error
    if not handler_started or not touched:
        if mark is not None:
            with contextlib.suppress(Exception):
                services.delete_mark(mark)
        if tx is not None:
            tx.files.discard()
        return "not_started", "preserved", error
    if spec.effect == "model":
        try:
            # Compare before undoing: a failure that created or deleted nothing keeps
            # every reference (undo restores the same NX objects).
            unchanged, failed_snap = False, None
            try:
                failed_snap = services.snapshot(part) if part is not None else None
                unchanged = failed_snap is not None and set(failed_snap) == set(before)
                unchanged = unchanged and _extra_unchanged(services, tx)
            except Exception:
                unchanged = False
            gone = _mark_gone(services, mark)
            if gone and not unchanged:
                raise RuntimeError("NX removed the call's undo mark and the model has changed")
            if not gone:  # else NX undid the failed step itself (see _mark_gone)
                services.undo_to(mark)
            references = "preserved"
            if not unchanged:
                # NX 2406 undo restores the same objects under the same tags: keep their
                # refs, re-bind moved anchored ones, retire only what did not come back.
                references = _report_stale(error, rebind_after_undo(services, floor))
            if not gone:
                services.delete_mark(mark)
            services.clear_update_errors()
        except Exception as rollback_error:
            with contextlib.suppress(Exception):
                services.invalidate_parts(services.parts())
            failed = NXError(
                "NX_ROLLBACK_FAILED",
                f"The operation failed and rollback also failed: {rollback_error}",
                details={"operation_error": error.error_dict()},
            )
            return "partial", "part_invalidated", failed
        if tx.display.kept:
            with contextlib.suppress(Exception):
                tx.display.restore()  # NX undo does not restore the camera.
        if tx.non_undoable_reason:
            error.details.setdefault("non_undoable", tx.non_undoable_reason)
            return "partial", "part_invalidated", error
        return "rolled_back", references, error
    if spec.effect == "file":
        clean = tx.files.discard()
        if tx.files.published or tx.non_undoable_reason or not clean:
            return "partial", "preserved", error
        return "rolled_back", "preserved", error
    if spec.effect == "view" and tx is not None and tx.display.kept:
        try:
            tx.display.restore()
        except Exception as restore_error:
            failed = NXError(
                "NX_RESTORE_FAILED",
                f"The view change failed and the display was not fully restored: {restore_error}",
                details={"operation_error": error.error_dict()},
            )
            return "partial", "preserved", failed
        return "rolled_back", "preserved", error
    if spec.effect == "session" and tx is not None and tx.outcome in ("rolled_back", "partial"):
        # The handler undid its own changes (tx.preview(), nx_redo_operation): it knows the
        # outcome, and the references when it rebound them (tx.after_undo()).
        return tx.outcome, tx.references or "part_invalidated", error
    # A view handler that kept nothing, and session effects, have no generic rollback.
    return "unknown", "preserved", error


# ---------------------------------------------------------------------------
# Children of nx_run_batch / nx_preview_changes


def run_child(
    spec, args, parent: Tx, *, prior: list | None = None, mode: str | None = None
) -> dict:
    """Run one child under the parent's mark and receipt; raise ``NXError`` on failure.

    The child resolves its own target (gate 3t): its refs, or a ``part`` call key in its
    arguments, else the parent's target; under a parent given ``part``, a child without
    its own is checked against that part (:func:`_batch_part`). A target that is not the
    current work part is made current for the child (gate 4s) and the parent's state comes
    back after it; the parent snapshots that part first (``tx.touch_part``), so its
    rollback, read-back and ``changes`` cover it. NX's undo is session-wide: the parent's
    one mark rolls back every part its children changed."""
    services = parent.services
    mode = mode or parent.mode or ("preview" if parent.spec.effect == "session" else "batch")
    index = len(parent.children)
    args, keys = targets.split_call_keys(spec, args)
    part_text = keys.get("part")
    bound: switching.Bound | None = None
    started = False  # the child's handler ran: a failure before it changed nothing
    try:
        if mode == "batch" and not spec.batchable:
            fail("NX_UNSUPPORTED_OPTION", f"{spec.name} cannot run in a batch", field="/tool")
        if mode == "preview" and not spec.previewable:
            fail("NX_UNSUPPORTED_OPTION", f"{spec.name} cannot run in a preview", field="/tool")
        if parent.mark is None:
            fail("NX_ERROR", "A child call needs the parent's undo mark")
        targets.check_call_keys(part_text)  # a child's expires_at is the parent's
        parent.checkpoint(done=index)
        checked = _gate_schema(spec, args, placeholders=True, services=services)  # 1
        held = {pointer(path) for path, _text in checked.placeholders}  # earlier steps' refs
        if checked.placeholders:
            _resolve_placeholders(checked, prior if prior is not None else parent.children)
            checked = _gate_schema(spec, checked.args, services=services)
        inherited = _batch_part(services, parent, part_text)
        try:
            target = targets.resolve(  # 3t: refs, part, else the parent's part
                spec, checked, services, part=part_text or inherited, parent=_root(parent).target
            )
        except NXError as refused:  # refs of another part than the batch's own part
            if inherited is None or refused.code != "NX_WRONG_PART_CONTEXT":
                raise
            if not _from_steps(spec, checked, services, _root(parent).target, held):
                raise _off_batch_part(spec, checked, services, _root(parent), refused) from None
            target = targets.resolve(  # refs an earlier step returned: that step's part
                spec, checked, services, part=None, parent=_root(parent).target
            )
        _gate_refs(spec, checked, services)  # 3r
        _gate_paths(spec, checked, services, target)  # 2
        _gate_variant(spec, checked, services, target)  # 3
        _gate_evidence(spec, checked, services)  # 3
        if target is not None and spec.effect == "model":
            parent.touch_part(target.part)  # before the switch: the parent's rollback
        bound = switching.enter(  # 4s
            spec,
            checked.args,
            target,
            services,
            explicit=part_text is not None or _switched(parent),
            nested=_switched(parent),
        )
        for code, message in bound.notes:
            parent.warn(code, message)
        _gate_requires(spec, services, explicit=part_text is not None)  # 4
        flags = _gate_ownership(spec, services, checked.args)  # 6
        part = services.work_part()
        before = services.snapshot(part) if part is not None else {}
        child = Tx(
            services,
            spec,
            operation_id=parent.operation_id,
            parent=parent,
            mode=mode,
            arguments=args,
        )
        child.mark = parent.mark
        child.target = target
        child.bound = bound
        call_args = _gate_units(spec, checked, services, child)  # 9
        observer = None
        started = True
        try:
            with services.observe() as observer:
                data = spec.handler(child, **call_args)  # 10
        finally:
            parent.touched = parent.touched or child.touched or bool(observer and observer.touched)
            parent.warnings.extend(child.warnings)
            if child.non_undoable_reason:
                parent.non_undoable(child.non_undoable_reason)
        bound.check()  # 4s (after): the child's target is still the work part
        _check_ownership_after(services, flags)
        before, after = _readback_parts(spec, services, child, part, before, parent.mark)  # 11
        retired = services.invalidate(before, after, spec.invalidates == "topology")  # 12
        for key, extra in child.extra_parts.items():  # the parent rolls these back too
            parent.extra_parts.setdefault(key, extra)
        result = {
            "tool": spec.name,
            "data": data,
            "frame": child.frame,
            "changes": _changes(services, before, after, spec, retired=retired, tx=child),
        }
        if child.units.get("length"):
            result["units"] = {k: v for k, v in child.units.items() if v}
        if target is not None and (part_text is not None or not _same_target(parent, target)):
            with contextlib.suppress(Exception):
                result["part"] = target.row(services)  # a child in another part says so
        parent.children.append(result)
        return result
    except Exception as exc:
        error = classify(exc)
        error = _version_net(spec, args, services, exc, error)
        _remember_license(spec, args, services, error)
        error.details.setdefault("child_index", index)
        error.details.setdefault("child_tool", spec.name)
        if not started:  # refused by a gate: the parent is not_started unless steps ran
            setattr(error, "unchanged", True)  # noqa: B010
        raise at_child(error, index) from exc
    finally:
        if bound is not None:  # 4s: the parent's part is current again
            warning = bound.leave()
            if warning:
                parent.warn("NX_UPDATE_WARNING", warning)


def _root(tx: Tx) -> Tx:
    while tx.parent is not None:
        tx = tx.parent
    return tx


def _batch_part(services, parent: Tx, part_text) -> str | None:
    """Gate 3t of a child without its own ``part`` under a call that named one (the
    ``part`` of nx_run_batch or nx_preview_changes): that part as ``p:N``, so the child's
    refs are checked against it as a top-level call's are (``NX_WRONG_PART_CONTEXT``). A
    step acts in another part only with its own ``part``."""
    root = _root(parent).target
    if part_text is not None or root is None or root.source != "part":
        return None
    return targets.part_id(services, root.part)


def _foreign_fields(spec, checked, services, target) -> list:
    """``(part, fields)`` of the refs that name another part than ``target``'s (an
    occurrence held by ``target`` counts as its own)."""
    out = []
    for key, (part, where, _kinds, holders) in targets.pinned_parts(
        services, checked, spec.target
    ).items():
        if key != target.key and target.key not in holders:
            out.append((part, where))
    return out


def _from_steps(spec, checked, services, target, held: set) -> bool:
    """Whether every ref of another part than the batch's came from an earlier step's
    result (``"$0.data.sketch"``): that step passed the gate in its own part, so the
    chained step follows it there."""
    foreign = _foreign_fields(spec, checked, services, target)
    return bool(foreign) and all(f in held for _part, where in foreign for f in where)


def _off_batch_part(spec, checked, services, root: Tx, error: NXError) -> NXError:
    """The refusal of a child whose refs name another part than the batch's ``part``."""
    target = root.target
    field, other = None, str((error.details.get("own") or {}).get("file") or "another part")
    with contextlib.suppress(Exception):
        for part, where in _foreign_fields(spec, checked, services, target)[:1]:
            field, other = where[0], targets.file_name(part)
    named = f"{field} names objects of {other}" if field else f"its object belongs to {other}"
    return NXError(
        "NX_WRONG_PART_CONTEXT",
        f"{root.spec.name} acts in {target.file} (its part) but this step's {named}: a step "
        "acts in another part only with a part of its own",
        field=field,
        hint=f'Add "part": "{other}" to this step\'s arguments to act there, or pass refs '
        f"of {target.file}",
        details={**error.details, "batch_part": target.file},
    )


def _switched(parent: Tx) -> bool:
    """Whether the top-level call made NX current on a part of its own (gate 4s): its
    children then switch whenever their target is not the current work part."""
    bound = _root(parent).bound
    return bool(bound is not None and (bound.switched or bound.bound))


def _same_target(parent: Tx, target) -> bool:
    own = _root(parent).target
    return own is not None and own.key == target.key
