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

Gate order: 1 schema, 2 workspace paths, 3 variant inference and evidence/version,
4 ``requires``,
5 engineer control hold (the host, before the kernel runs), 6 part ownership,
7 receipt, 8 undo mark, 9 unit conversion, 10 handler, 11 read-back, 12 reference
invalidation, 13 annotation refresh, 14 result projection, 15 error mapping.
Nothing else applies gates. Around 8-15 a non-read call's loads become barriers in the
undo history and dropped undo marks get their reason (``core/marks.py``); a committed
changing call stamps the parts it worked in (``core/busy.py``). Gate 4's
``no_active_sketch`` also refuses in a part whose sketch is parked (``core/parking.py``).

Batch placeholders: a Ref field of a child may be ``"$<i>.<path>"`` naming the
result envelope of child ``i`` (0-based, earlier children only), e.g.
``"$0.data.feature"``; a row resolves to its ``id``.
"""

from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from nx_mcp.core import busy, evidence, parking, switching
from nx_mcp.core import marks as undo_marks
from nx_mcp.core import target as targets
from nx_mcp.core.errors import CATEGORIES, NXError, classify, fail
from nx_mcp.core.receipts import ANY_PART, Receipts, new_operation_id
from nx_mcp.core.references import GENERIC_NAME as _GENERIC
from nx_mcp.core.tx import Services, Tx, check_owned
from nx_mcp.core.types import SHEET_KINDS, convert_quantity, expand_kinds
from nx_mcp.core.validate import (
    Checked,
    at_child,
    combine,
    get_at,
    pointer,
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


def _gate_variant(spec, checked: Checked, services: Services) -> None:  # gate 3 (first)
    """An omitted variant of a ``variant_from`` tool, taken from the object its ref names
    (``nx_edit_drawing_table`` without ``kind``: the table's kind); then the fields that
    variant needs (``required_by``)."""
    if spec.variant_from is None or checked.args.get(spec.variants) is not None:
        return
    name, resolver = spec.variant_from
    ref = checked.args.get(name)
    value = None
    if isinstance(ref, str):
        try:
            obj = services.references.resolve(ref, ())  # the resolver tells kinds apart
            if resolver is not None:
                value = resolver(Tx(services, spec, operation_id=None), obj)
            else:
                value = getattr(services.references.entry(ref), "kind", None)
        except NXError as error:  # a stale or unknown ref: say which field
            error.field = error.field or "/" + name
            raise
        except Exception:
            value = None  # the resolver cannot tell: the variant must be given
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
    errors = variant_needs(spec.params, checked.args, spec.variants, spec.required_by)
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


def _free_name(resolved: Path) -> Path:
    """The first ``<stem>-<n><suffix>`` beside ``resolved`` that does not exist."""
    for n in range(2, 1000):
        candidate = resolved.with_name(f"{resolved.stem}-{n}{resolved.suffix}")
        if not candidate.exists():
            return candidate
    return resolved


def _gate_paths(spec, checked: Checked, services: Services, target=None) -> None:  # gate 2
    """Workspace checks of every path argument. A path without a suffix gets the
    marker's first one (``parts/plate`` -> ``parts/plate.prt``). ``save`` paths compare
    against the call's target part (gate 3t), else the work part."""
    for path, marker in checked.paths:
        field = pointer(path)
        value = get_at(checked.args, path)
        if marker.suffixes and isinstance(value, str) and value and not Path(value).suffix:
            value = value.rstrip("/" + chr(92)) + marker.suffixes[0]
        try:
            resolved = Path(services.workspace.resolve(value))
        except Exception as error:
            reserved = any(p.casefold() == ".nx-mcp" for p in Path(str(value)).parts)
            raise NXError(
                "NX_PATH_RESERVED" if reserved else "NX_PATH_OUTSIDE_WORKSPACE",
                f"{field}: {error}",
                field=field,
                details={"path": value},
            ) from None
        if marker.suffixes and resolved.suffix.lower() not in marker.suffixes:
            expected = "/".join(marker.suffixes)
            fail(
                "NX_UNSUPPORTED_FILE_TYPE",
                f"{field}: expected a {expected} file, e.g. 'parts/name{marker.suffixes[0]}' "
                f"(got '{resolved.suffix}')",
                field=field,
                hint=f"End the path with {marker.suffixes[0]}, or leave the suffix out",
                details={"path": value, "expected": list(marker.suffixes)},
            )
        if marker.mode == "read" and not resolved.is_file():
            fail(
                "NX_FILE_NOT_FOUND",
                f"{field}: no such file",
                field=field,
                hint="Check the path with nx_list_files on its folder",
                details={"path": value},
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


def _gate_evidence(spec, checked: Checked, services: Services) -> None:  # gate 3
    refusal = evidence.check(
        spec, evidence.build_key(services.build), checked.args, dev=bool(services.dev)
    )
    if refusal is None:
        # Session license memory: a tested tool/variant that NX refused for a missing
        # license is refused before NX until NX restarts (also in development mode).
        refusal = evidence.license_refusal(spec, checked.args)
    if refusal is not None:
        raise refusal


def _remember_license(spec, args, services: Services, error) -> None:
    with contextlib.suppress(Exception):
        evidence.remember_license(spec, services.build, args, error)


def _gate_requires(spec, services: Services) -> None:  # gate 4
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
            ok = not state.get("active_sketch") and not parking.open_in(
                services, services.work_part()
            )
        else:
            ok = bool(state.get(item))
        if not ok:
            code, message, hint = REQUIRE_FAILURES[item]
            fail(code, message, hint=hint)


def _gate_ownership(spec, services: Services, args: dict) -> dict | None:  # gate 6 (before)
    """Refuse a model change of a part the agent does not own; return IsModified flags.

    A call given its ``new_part`` path works in a part it creates (agent-owned), so
    the current work part is not checked; the after-handler check still undoes any
    change to a part the agent does not own.
    """
    if services.dev or spec.effect not in ("model", "file"):
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
    one (``services.sheet_of``); for a tool that creates a sheet, its top-level
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
        elif dim == "count":
            target = "unitless"
        else:
            target = length or "mm"
        where = pointer(path)
        try:
            value = convert_quantity(get_at(checked.args, path), dim, target, evaluate, where)
        except ValueError as error:
            fail("NX_INVALID_ARGUMENT", f"{where}: {error}", field=where)
        set_at(checked.args, path, value)
    for path, canonical in checked.forms:  # an axis-angle pose -> {translation, rotation}
        try:
            set_at(checked.args, path, canonical(get_at(checked.args, path)))
        except (ValueError, KeyError, TypeError) as error:
            fail("NX_INVALID_ARGUMENT", f"{pointer(path)}: {error}", field=pointer(path))
    return checked.args


def _resolve_placeholders(checked: Checked, prior: list) -> None:
    for path, text in checked.placeholders:
        index, *keys = text[1:].split(".")
        try:
            value = prior[int(index)]
            for key in keys:
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (TypeError, IndexError, KeyError, ValueError):
            fail(
                "NX_INVALID_ARGUMENT",
                f"{pointer(path)}: {text} does not name an earlier result",
                field=pointer(path),
            )
        if isinstance(value, dict) and "id" in value:
            value = value["id"]
        set_at(checked.args, path, value)


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
    bound: switching.Bound | None = None
    handler_started = handler_done = False
    args, keys = targets.split_call_keys(spec, args)  # part/expires_at in arguments
    part_text = keys.get("part", ctx.part)
    caller = targets.check_caller(keys.get("caller", ctx.caller))
    raw_args = copy.deepcopy(args) if isinstance(args, dict) else args
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
        _gate_paths(spec, checked, services, target)  # 2
        _gate_variant(spec, checked, services)  # 3
        _gate_evidence(spec, checked, services)  # 3
        bound = switching.enter(  # 4s: the target becomes current (core/switching.py)
            spec, checked.args, target, services, explicit=part_text is not None
        )
        _gate_requires(spec, services)  # 4
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
        if spec.effect != "read":  # loads and dropped undo marks (core/marks.py)
            watched = undo_marks.watch(services)
            census = targets.census(services)  # the agent's loaded parts (gate 14 note)
        if spec.effect == "model":  # 8
            before = services.snapshot(part) if part is not None else {}
            floor = _ref_floor(services)
            mark = services.set_mark("NX MCP: " + spec.label)
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
        if not isinstance(data, dict):
            fail("NX_ERROR", f"{spec.name} returned {type(data).__name__}, not an object")
        bound.check()  # 4s (after): the switched target is still the work part
        _check_ownership_after(services, flags)  # 6 (after)
        changes = None
        if spec.effect == "model":
            seen, after = _readback_parts(spec, services, tx, part, before, mark)  # 11
            retired = services.invalidate(seen, after, spec.invalidates == "topology")  # 12
            refreshed = _refresh(spec, services, tx, bound)  # 13
            changes = _changes(
                services, seen, after, spec, refreshed, retired=retired, floor=floor, tx=tx
            )
            if tx.references == "part_invalidated":  # the handler invalidated the part
                changes["references"] = tx.references
                changes.pop("stale_kinds", None)
            if tx.outcome != "not_applicable":  # a discarded preview: no mark to undo
                _record(services, tx, mark, part, operation_id, spec.name)
        elif tx.changed:  # a file or session tool reporting the objects it changed
            changes = {"created": [], "modified": list(tx.changed), "deleted": []}
            changes["references"] = "preserved"
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
        receipts.finish(record, envelope)
        return envelope
    except BaseException as exc:  # 15 (SystemExit/KeyboardInterrupt from a handler too)
        error = classify(exc)
        _remember_license(spec, raw_args, services, error)
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
        _gate_paths(spec, checked, services, target)  # 2
        _gate_variant(spec, checked, services)  # 3
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
        _gate_requires(spec, services)  # 4
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
