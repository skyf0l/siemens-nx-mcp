"""``Tx``: a handler's only route to NX and to the services (architecture.md 3.2).

Handlers never set undo marks, write receipts or touch the panel. ``tx.session``
and ``tx.part`` mark the transaction *touched*; a handler failure while
untouched reports ``not_started``.

``Services`` documents what the kernel needs from the NX side. In production it
is ``nx_mcp.host.services.NXServices`` (operation store, object registry, undo
marks, snapshots and workspace); unit tests may supply their own implementation.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nx_mcp.core.errors import WARNING_CODES, NXError, fail

if TYPE_CHECKING:
    from nx_mcp.core.references import Row

# Cooperative budget per cost class (architecture.md 3.3), measured from gate 9.
# tx.checkpoint() past the budget raises interrupted/NX_CANCELLED (reason "budget").
COST_BUDGET_SECONDS = {"light": 30.0, "heavy": 300.0, "long": 1800.0}
DEFAULT_BUDGET_SECONDS = 300.0

NOT_GRANTED_HINT = (
    "The part is the engineer's: ask them in chat to tick 'Allow agent to change {name}' "
    "in the NX MCP panel, then retry, or work in a part you create"
)


def check_owned(services, part, *, field: str | None = None) -> None:
    """Gate 6: refuse to change ``part`` unless the agent owns it (off in development)."""
    if part is None or services.dev or services.owns(part):
        return
    path = services.part_path(part)
    name = str(getattr(part, "Name", "") or path)
    fail(
        "NX_PART_NOT_GRANTED",
        f"The part {path} belongs to the engineer: the agent may change only parts it "
        "created or opened, or parts the engineer allowed on the NX MCP panel",
        hint=NOT_GRANTED_HINT.format(name=name),
        field=field,
        details={"part": path},
    )


class Services:
    """The NX-side services seam. Every member is required unless noted."""

    # Untyped seams (NXOpen has no stubs; the rest are duck-typed): ``Any``.
    nx: Any = None  # the NXOpen module
    uf: Any = None  # the UFSession (may be None where UF is not used)
    session: Any = None  # the NX session
    workspace: Any = None  # nx_mcp.workspace.Workspace-like: root, resolve(path)
    build: str | None = None  # NX version string
    dev: bool = False  # NX_MCP_DEV=1
    session_id = ""
    store: Any = None  # OperationStore-like: get, put, path
    references: Any = None  # core.references.References over this object
    runs = "nx"  # the process these services run in: "nx", or "sidecar" (gate 1)

    # parts and units
    def work_part(self): ...
    def parts(self) -> list: ...
    def part_key(self, part) -> str: ...
    def part_path(self, part) -> str: ...
    def owning_part(self, obj): ...
    def part_units(self, part) -> str: ...  # "mm" or "in"
    def infer_kind(self, obj) -> str: ...

    # drawing sheets (gate 9: SheetLength / SheetPoint arguments)
    def current_sheet(self): ...  # the work part's current drawing sheet or None
    def sheet_of(self, obj): ...  # sheet that is/shows obj (+ @sheet_locator kinds) or None
    def sheet_units(self, sheet) -> str: ...  # "mm" or "in"

    # object registry handles behind core.references
    def register_handle(self, obj, kind: str, part) -> str: ...
    def resolve_handle(self, handle: str): ...
    def handle_live(self, handle: str) -> bool: ...
    def handle_kind(self, handle: str) -> str | None: ...
    def handle_name(self, handle: str) -> str | None: ...

    # state for `requires` and ownership (gate 6)
    def state(self) -> dict: ...
    def owns(self, part) -> bool: ...  # created/opened by the agent, or granted
    def own(self, part) -> None: ...
    def modified_flags(self) -> dict: ...  # {part_key: IsModified} of parts not owned

    # display state: `view` rollback and restore_id (architecture.md 3.2)
    def display_camera(self): ...  # opaque camera snapshot of the work view
    def display_records(self, objects, appearance: bool) -> list: ...  # [(obj, record)]
    def highlights(self) -> list: ...  # objects the agent highlighted
    def restore_display(self, camera, records, highlights) -> None: ...
    def save_display_snapshot(self, records, camera) -> str: ...  # restore_id

    # undo, snapshots, invalidation
    def observe(self): ...  # context manager -> object with .touched
    def set_mark(self, name: str, visible: bool = True): ...
    def undo_to(self, mark) -> None: ...
    def delete_mark(self, mark) -> None: ...
    def snapshot(self, part) -> dict: ...  # {key: entry}; keys compare structure
    def describe(self, entry, live: bool) -> Row: ...  # snapshot entry -> result row
    def feature_errors(self, part, before: dict, mark) -> list: ...
    def clear_update_errors(self) -> None: ...
    def invalidate(self, before: dict, after: dict, topology: bool) -> None: ...
    def invalidate_parts(self, parts) -> None: ...

    def retire_undone(self, parts) -> set | None:
        """After an undo: retire the registry handles of ``parts`` whose object did not
        come back (NX restores the same tags) and return them; ``None`` = every handle
        of ``parts`` was retired (services that cannot tell)."""
        self.invalidate_parts(parts)
        return None

    def forget_parts(self, keys) -> None: ...  # closed parts (by part_key): refs, undo, owner

    def refreshers(self) -> dict:
        """The ``@refresher`` hooks gate 13 runs, ``{kind: fn(tx, objects or None)}``.

        The NX host returns every registered hook; services without NX run none."""
        return {}

    def record_history(self, mark, part, operation_id: str, tool: str) -> None: ...

    # the agent's undo history and checkpoints (nx_undo_operation, nx_*_checkpoint)
    def undo_history(self) -> list:
        """The live list of the agent's committed model calls, oldest first:
        ``{mark, part_id, operation_id, method}``. The host empties it when the
        engineer changes the model, so an undo never reaches across their edits."""
        return self.__dict__.setdefault("_undo_history", [])

    def undo_checkpoints(self) -> dict:
        """The live ``{checkpoint_id: {mark, part_id, index, label}}`` of this session;
        ``index`` is the length of :meth:`undo_history` when it was created."""
        return self.__dict__.setdefault("_undo_checkpoints", {})

    def mark_exists(self, mark) -> bool:
        """False once NX dropped the undo mark (a save, a part close)."""
        return bool(self.session.DoesUndoMarkExist(mark, None))

    def after_commit(self, spec) -> None: ...
    def after_failure(self, spec) -> None: ...

    # optional members (defaults below): previews and the NX MCP panel
    def revision(self) -> int:
        """Counter of changes to the session: every non-read call (``after_commit`` and
        ``after_failure``) and every engineer change between agent calls bump it.
        ``nx_finish_preview`` re-applies a preview only when nothing else happened."""
        return 0

    def control_status(self) -> dict | None:
        """The NX MCP panel's state (mode, activity, ...), None without the panel."""
        return None


def publish_new(temp, target, *, field: str | None = "/path") -> Path:
    """Move a finished ``temp`` file to ``target`` without ever overwriting it.

    ``os.link`` creates ``target`` atomically and fails when it exists, also when
    another writer created it meanwhile (``os.replace`` would overwrite it). Where
    hard links are unavailable the bytes are copied into an exclusively created
    file. A file at ``target`` raises ``NX_ALREADY_EXISTS``; ``temp`` is removed
    either way, and a partial copy never stays behind.
    """
    temp, target = Path(temp), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            os.link(temp, target)
        except FileExistsError:
            raise
        except OSError:  # no hard links on this volume: exclusive create + copy
            with temp.open("rb") as source, target.open("xb") as out:
                try:
                    shutil.copyfileobj(source, out, 1024 * 1024)
                except BaseException:
                    out.close()
                    with contextlib.suppress(OSError):
                        target.unlink()
                    raise
    except FileExistsError:
        fail(
            "NX_ALREADY_EXISTS",
            f"{target.name} already exists; files are never overwritten",
            field=field,
            details={"path": str(target)},
        )
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    return target


class Files:
    """Atomic workspace writes: stage, then publish (architecture.md 3.2).

    Staged files live in ``.nx-mcp/staging/<operation_id>/``; the kernel removes
    that folder when the call ends, committed or not (``cleanup``). ``publish``
    never overwrites: a target that exists fails the whole publish before any
    file is moved. The one exception is a file staged with ``replace=True`` (a
    ``replace`` path the caller asked to overwrite): it replaces an existing file
    atomically (``os.replace``: the old file or the complete new one, never a part).
    """

    def __init__(self, tx: Tx) -> None:
        self._tx = tx
        self._staged: list[tuple[Path, Path, bool]] = []
        self.published = False
        self.replaced: list[Path] = []  # targets whose earlier file ``publish`` replaced

    @property
    def folder(self) -> Path:
        root = Path(self._tx.workspace.root)
        if not getattr(self, "_name", None):
            self._name = self._tx.operation_id or uuid.uuid4().hex
        return root / ".nx-mcp" / "staging" / self._name

    def stage(self, path, *, replace: bool = False) -> Path:
        """A temporary path to write; ``publish()`` moves it to ``path``. With
        ``replace`` an existing file at ``path`` is replaced (never a directory)."""
        target = Path(path)
        folder = self.folder
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f"{len(self._staged)}_{target.name}"
        self._tx.touched = True
        self._staged.append((temp, target, replace))
        return temp

    def publish(self) -> list[Path]:
        for temp, target, replace in self._staged:
            if not temp.exists():
                fail("NX_EXPORT_FAILED", f"Nothing was written for {target.name}")
            if replace and target.exists() and not target.is_file():
                fail(
                    "NX_WRONG_KIND",
                    f"{target.name} is not a file; only a file can be replaced",
                    details={"path": str(target)},
                )
            if not replace and target.exists():
                fail(
                    "NX_ALREADY_EXISTS",
                    f"{target.name} already exists; files are never overwritten",
                    details={"path": str(target)},
                )
        out: list[Path] = []
        staged, self._staged = self._staged, []
        try:
            for temp, target, replace in staged:
                if replace and target.exists():
                    os.replace(temp, target)  # atomic on one volume (staging is inside)
                    self.replaced.append(target)
                else:
                    publish_new(temp, target, field=None)
                self.published = True
                out.append(target)
        except BaseException:
            self._staged = staged[len(out) + 1 :]  # the rest stay staged until discard()
            raise
        self.cleanup()
        return out

    def discard(self) -> bool:
        """Remove unpublished staged files and the staging folder; True when nothing
        of them remains."""
        clean = True
        for temp, _target, _replace in self._staged:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                clean = False
        self._staged = []
        self.cleanup()
        return clean

    def cleanup(self) -> None:
        """Remove this call's staging folder (and ``staging`` itself once empty)."""
        if self._staged or self._tx.parent is not None:
            return  # files still waiting, or a batch child: the top-level call cleans up
        folder = self.folder
        shutil.rmtree(folder, ignore_errors=True)
        with contextlib.suppress(OSError):
            folder.parent.rmdir()  # only when no other call is staging


class Display:
    """Display state a handler is about to change, so the kernel can restore it.

    Call before changing: ``tx.display.keep_camera()``, ``tx.display.keep(objs,
    appearance=True)`` (blank state, colour, transparency; bodies expand to their
    faces with ``appearance``), ``tx.display.keep_highlights()``. A failed call
    restores what was kept (``view`` tools: outcome ``rolled_back``); a successful
    call of a ``restorable`` tool returns ``restore_id`` for ``nx_restore_display``.
    """

    def __init__(self, tx: Tx) -> None:
        self._tx = tx
        self.camera: Any = None  # opaque snapshot from Services.display_camera
        self.records: dict = {}  # key -> [obj, record]
        self.highlights: list | None = None

    @property
    def kept(self) -> bool:
        return self.camera is not None or bool(self.records) or self.highlights is not None

    def keep_camera(self) -> None:
        if self.camera is None:
            self.camera = self._tx.services.display_camera()

    def keep(self, objects, *, appearance: bool = False) -> None:
        for obj, record in self._tx.services.display_records(list(objects), appearance):
            tag = getattr(obj, "Tag", None)
            key = int(tag) if tag is not None else id(obj)
            if key in self.records:
                known = self.records[key][1]
                known.update({k: v for k, v in record.items() if k not in known})
            else:
                self.records[key] = [obj, dict(record)]

    def keep_highlights(self) -> None:
        if self.highlights is None:
            self.highlights = list(self._tx.services.highlights())

    def restore(self) -> None:
        """Put back everything kept: records, highlights, then the camera."""
        self._tx.services.restore_display(
            self.camera, [tuple(pair) for pair in self.records.values()], self.highlights
        )

    def snapshot(self) -> str | None:
        """A restore_id for the kept camera and records, or None."""
        if self.camera is None and not self.records:
            return None
        return self._tx.services.save_display_snapshot(
            [tuple(pair) for pair in self.records.values()], self.camera
        )


class Tx:
    def __init__(
        self,
        services: Services,
        spec,
        *,
        operation_id: str | None,
        receipts=None,
        record=None,
        parent: Tx | None = None,
        mode: str | None = None,
    ) -> None:
        self._services = services
        self.spec = spec
        self.operation_id = operation_id
        self.receipts = receipts
        self.record = record
        self.parent = parent
        self.mode = mode  # None, "batch" or "preview" for children
        self.started = time.monotonic()
        self.touched = False
        self.frame = "work_part_absolute"
        self.units = {"length": None, "angle": "deg"}  # + "sheet_length" (gate 9)
        self.sheet: Any = None  # the target NX drawing sheet of a sheet tool (gate 9), or None
        self.warnings: list[dict] = []
        self.non_undoable_reason: str | None = None
        self.mark: Any = None  # the NX undo mark of this call
        self.outcome: str | None = None  # "previewed" after tx.preview()
        self.references: str | None = None  # "part_invalidated": overrides changes
        self.files = Files(self)
        self.display = Display(self)
        self.children: list[dict] = []

    # -- NX access ------------------------------------------------------------
    @property
    def nx(self):
        return self._services.nx

    @property
    def uf(self):
        return self._services.uf

    @property
    def session(self):
        self.touched = True
        return self._services.session

    @property
    def part(self):
        part = self._services.work_part()
        if part is None:
            fail("NX_NO_WORK_PART", "No work part is open", hint="Open a part with nx_open_part")
        self.touched = True
        return part

    @property
    def workspace(self):
        return self._services.workspace

    @property
    def dev(self) -> bool:
        return bool(self._services.dev)

    @property
    def services(self) -> Services:
        return self._services

    # -- references -------------------------------------------------------------
    def resolve(self, ref: str, *kinds: str, work_part: bool = False):
        """The live object for ``ref``: stale_reference, wrong_kind or wrong_owner."""
        obj = self._services.references.resolve(ref, kinds)
        if work_part:
            owner = self._services.owning_part(obj)
            work = self._services.work_part()
            if (
                getattr(obj, "IsOccurrence", False)
                or owner is None
                or work is None
                or (self._services.part_key(owner) != self._services.part_key(work))
            ):
                fail(
                    "NX_WRONG_OWNER",
                    f"{ref} is not owned by the work part",
                    hint="Use geometry of the work part or activate the owning part",
                    details={"ref": ref},
                )
        return obj

    def require_owned(self, part, *, field: str | None = None) -> None:
        """Refuse to save, close or change ``part`` unless the agent owns it (gate 6).

        The kernel checks the work part of ``model`` tools; ``file`` and ``session``
        tools that save or close a specific part call this before acting.
        """
        check_owned(self._services, part, field=field)

    def kind_of(self, obj) -> str:
        """The exact reference kind of a live NX object (``feature``, ``face``, ...)."""
        return self._services.infer_kind(obj)

    def ref(self, obj, kind: str | None = None, *, anchor: bool = False, part=None) -> Row:
        """A result row ``{id, kind, name, part}`` (+ anchor) for ``obj``."""
        kind = kind or self._services.infer_kind(obj)
        return self._services.references.row(obj, kind, part=part, anchor=anchor)

    # -- reporting ----------------------------------------------------------------
    def warn(self, code: str, message: str) -> None:
        if code not in WARNING_CODES:
            code = "NX_UPDATE_WARNING"
        self.warnings.append({"code": code, "message": str(message)})

    def checkpoint(self, done=None, total=None) -> None:
        """Progress, cooperative cancellation and the cost budget (architecture 3.3)."""
        root = self
        while root.parent is not None:
            root = root.parent
        # A batch or preview child's own progress (curves of one step) would overwrite
        # the parent's step count; children only check cancellation and the budget.
        if root is self and root.receipts is not None and (done is not None or total is not None):
            root.receipts.progress(root.record, done, total)
        if root.receipts is not None and root.receipts.cancel_requested(root.operation_id):
            raise NXError(
                "NX_CANCELLED", "Cancelled by nx_cancel_operation", details={"reason": "cancel"}
            )
        budget = COST_BUDGET_SECONDS.get(root.spec.cost, DEFAULT_BUDGET_SECONDS)
        elapsed = time.monotonic() - root.started
        if elapsed > budget:
            raise NXError(
                "NX_CANCELLED",
                f"Stopped after {elapsed:.0f} s, over the {budget:g} s budget of a "
                f"{root.spec.cost} operation",
                hint="Split the work into smaller calls (fewer objects or steps per call)",
                details={
                    "reason": "budget",
                    "budget_seconds": budget,
                    "elapsed_seconds": round(elapsed, 3),
                },
            )

    def non_undoable(self, reason: str) -> None:
        """Declare a step that undo cannot revert; a later failure reports partial."""
        self.non_undoable_reason = str(reason)

    @contextlib.contextmanager
    def checks(self):
        """Read-only lookups and refusals that run before the handler changes anything.

        An ``NXError`` raised inside is marked ``unchanged``: the kernel reports
        ``not_started`` although NX was read (a busy part, a name clash, a stale
        reference). Only wrap code that changes nothing, before the first change.
        """
        try:
            yield
        except NXError as error:
            error.unchanged = True
            raise

    # -- temporary geometry ---------------------------------------------------------
    @contextlib.contextmanager
    def scratch(self):
        """Temporary geometry under a hidden mark that is always undone."""
        services = self._services
        part = services.work_part()
        before = services.snapshot(part) if part is not None else {}
        self.touched = True
        mark = services.set_mark("NX MCP: scratch", visible=False)
        try:
            yield mark  # the scratch mark, e.g. for model.update(tx, mark)
        finally:
            try:
                services.undo_to(mark)
                services.delete_mark(mark)
                after = services.snapshot(part) if part is not None else {}
            except Exception as error:
                raise NXError(
                    "NX_ROLLBACK_FAILED", f"Temporary geometry was not removed: {error}"
                ) from error
            if set(after) != set(before):
                raise NXError(
                    "NX_ROLLBACK_FAILED",
                    "Temporary geometry changed the object count",
                    details={"before": len(before), "after": len(after)},
                )

    @contextlib.contextmanager
    def preview(self):
        """Apply children temporarily under a visible mark that is always undone.

        Success sets ``outcome`` ``previewed``. When the body fails, its own error
        propagates and ``outcome`` says what happened to the model: ``rolled_back``,
        or ``partial`` when the undo failed too (``details.rollback_error``); an undo
        that fails after a successful body raises ``NX_ROLLBACK_FAILED``.
        """
        services = self._services
        part = services.work_part()
        before = services.snapshot(part) if part is not None else {}
        self.touched = True
        mark = services.set_mark("NX MCP: preview")
        previous, self.mark, self.mode = self.mark, mark, "preview"
        failed = None
        try:
            yield
        except BaseException as error:
            failed = error
            raise
        finally:
            self.mark, self.mode = previous, None
            rollback_error = None
            try:
                services.undo_to(mark)
                services.delete_mark(mark)
                services.clear_update_errors()
                after = services.snapshot(part) if part is not None else {}
            except Exception as error:
                rollback_error = error
            if rollback_error is not None:
                with contextlib.suppress(Exception):
                    services.invalidate_parts(services.parts())
            elif set(after) != set(before) or self.children:
                from nx_mcp.core import kernel

                kernel.rebind_after_undo(services)
            if failed is not None:
                self.outcome = "rolled_back" if rollback_error is None else "partial"
                if rollback_error is not None and isinstance(failed, NXError):
                    failed.details["rollback_error"] = str(rollback_error)
            elif rollback_error is not None:
                self.outcome = "partial"
                raise NXError(
                    "NX_ROLLBACK_FAILED", f"The preview was not undone: {rollback_error}"
                ) from rollback_error
            else:
                self.outcome = "previewed"

    # -- nested calls -----------------------------------------------------------------
    def run(
        self, tool: str, args: dict, *, prior: list | None = None, mode: str | None = None
    ) -> dict:
        """Run a catalogue tool as a child of this call (batch or preview).

        ``mode`` ``"batch"`` admits batchable tools, ``"preview"`` previewable ones
        (default: preview inside ``tx.preview()`` or under a session tool, else batch).
        """
        from nx_mcp.core import catalogue, kernel

        spec = catalogue.get(tool)
        if spec is None:
            fail("NX_UNKNOWN_TOOL", f"Unknown tool {tool}", field="/tool")
        return kernel.run_child(spec, args, self, prior=prior, mode=mode)
