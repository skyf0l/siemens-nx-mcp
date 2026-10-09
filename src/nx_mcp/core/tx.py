"""``Tx``: a handler's only route to NX and to the services (architecture.md 3.2).

Handlers never set undo marks, write receipts or touch the panel. ``tx.session``
and ``tx.part`` mark the transaction *touched*; a handler failure while
untouched reports ``not_started``.

``Services`` documents what the kernel needs from the NX side. In production it
is ``nx_mcp.host.legacy_services.LegacyServices`` (the existing executor's
operation store, object registry, undo marks, snapshots and workspace); unit
tests may supply their own implementation.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path

from nx_mcp.core.errors import WARNING_CODES, NXError, fail

COST_BUDGET_SECONDS = {"instant": 30.0, "light": 30.0, "heavy": 300.0, "long": 1800.0}


class Services:
    """The NX-side services seam. Every member is required unless noted."""

    nx = None  # the NXOpen module
    uf = None  # the UFSession (may be None where UF is not used)
    session = None  # the NX session
    workspace = None  # nx_mcp.workspace.Workspace-like: root, resolve(path)
    build: str | None = None  # NX version string
    dev = False  # NX_MCP_DEV=1
    session_id = ""
    store = None  # OperationStore-like: get, put, path
    references = None  # core.references.References over this object

    # parts and units
    def work_part(self): ...
    def parts(self) -> list: ...
    def part_key(self, part) -> str: ...
    def part_path(self, part) -> str: ...
    def owning_part(self, obj): ...
    def part_units(self, part) -> str: ...  # "mm" or "in"
    def sheet_units(self) -> str | None: ...
    def infer_kind(self, obj) -> str: ...

    # legacy registry seam for core.references
    def legacy_register(self, obj, kind: str, part) -> str: ...
    def legacy_resolve(self, legacy_id: str): ...
    def legacy_live(self, legacy_id: str) -> bool: ...
    def legacy_kind(self, legacy_id: str) -> str | None: ...
    def legacy_name(self, legacy_id: str) -> str | None: ...

    # state for `requires` and ownership
    def state(self) -> dict: ...
    def owns(self, part) -> bool: ...
    def own(self, part) -> None: ...
    def modified_flags(self) -> dict: ...

    # undo, snapshots, invalidation
    def observe(self): ...  # context manager -> object with .touched
    def set_mark(self, name: str, visible: bool = True): ...
    def undo_to(self, mark) -> None: ...
    def delete_mark(self, mark) -> None: ...
    def snapshot(self, part) -> dict: ...  # {key: entry}; keys compare structure
    def describe(self, entry, live: bool) -> dict: ...  # snapshot entry -> result row
    def feature_errors(self, part, before: dict, mark) -> list: ...
    def clear_update_errors(self) -> None: ...
    def invalidate(self, before: dict, after: dict, topology: bool) -> None: ...
    def invalidate_parts(self, parts) -> None: ...
    def refresh_annotations(self, spec) -> list: ...
    def record_history(self, mark, part, operation_id: str, tool: str) -> None: ...
    def after_commit(self, spec) -> None: ...
    def after_failure(self, spec) -> None: ...


class Files:
    """Atomic workspace writes: stage, then publish (architecture.md 3.2)."""

    def __init__(self, tx: Tx) -> None:
        self._tx = tx
        self._staged: list[tuple[Path, Path]] = []
        self.published = False

    def stage(self, path) -> Path:
        """A temporary path to write; ``publish()`` moves it to ``path``."""
        target = Path(path)
        root = Path(self._tx.workspace.root)
        folder = root / ".nx-mcp" / "staging" / (self._tx.operation_id or uuid.uuid4().hex)
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f"{len(self._staged)}_{target.name}"
        self._tx.touched = True
        self._staged.append((temp, target))
        return temp

    def publish(self) -> list[Path]:
        out = []
        for temp, target in self._staged:
            if not temp.exists():
                fail("NX_EXPORT_FAILED", f"Nothing was written for {target.name}")
        for temp, target in self._staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, target)
            self.published = True
            out.append(target)
        self._staged = []
        return out

    def discard(self) -> bool:
        """Remove unpublished staged files; True when nothing remains."""
        clean = True
        for temp, _target in self._staged:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                clean = False
        self._staged = []
        return clean


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
        self.units = {"length": None, "angle": "deg"}
        self.warnings: list[dict] = []
        self.non_undoable_reason: str | None = None
        self.mark = None
        self.outcome: str | None = None  # "previewed" after tx.preview()
        self.files = Files(self)
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

    def kind_of(self, obj) -> str:
        """The exact reference kind of a live NX object (``feature``, ``face``, ...)."""
        return self._services.infer_kind(obj)

    def ref(self, obj, kind: str | None = None, *, anchor: bool = False, part=None) -> dict:
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
        if root.receipts is not None and (done is not None or total is not None):
            root.receipts.progress(root.record, done, total)
        if root.receipts is not None and root.receipts.cancel_requested(root.operation_id):
            raise NXError(
                "NX_CANCELLED", "Cancelled by nx_cancel_operation", details={"reason": "cancel"}
            )
        budget = COST_BUDGET_SECONDS.get(root.spec.cost, 300.0)
        if time.monotonic() - root.started > budget:
            raise NXError(
                "NX_CANCELLED",
                f"Exceeded the {budget:g} s budget of a {root.spec.cost} operation",
                details={"reason": "budget", "budget_seconds": budget},
            )

    def non_undoable(self, reason: str) -> None:
        """Declare a step that undo cannot revert; a later failure reports partial."""
        self.non_undoable_reason = str(reason)

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
            yield
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
        """Apply children temporarily under a visible mark that is always undone."""
        services = self._services
        part = services.work_part()
        before = services.snapshot(part) if part is not None else {}
        self.touched = True
        mark = services.set_mark("NX MCP: preview")
        previous, self.mark, self.mode = self.mark, mark, "preview"
        try:
            yield
        finally:
            self.mark, self.mode = previous, None
            try:
                services.undo_to(mark)
                services.delete_mark(mark)
                services.clear_update_errors()
                after = services.snapshot(part) if part is not None else {}
            except Exception as error:
                raise NXError(
                    "NX_ROLLBACK_FAILED", f"The preview was not undone: {error}"
                ) from error
            if set(after) != set(before) or self.children:
                services.invalidate_parts(services.parts())
            self.outcome = "previewed"

    # -- nested calls -----------------------------------------------------------------
    def run(self, tool: str, args: dict, *, prior: list | None = None) -> dict:
        """Run a catalogue tool as a child of this call (batch or preview)."""
        from nx_mcp.core import catalogue, kernel

        spec = catalogue.get(tool)
        if spec is None:
            fail("NX_UNKNOWN_TOOL", f"Unknown tool {tool}", field="/tool")
        return kernel.run_child(spec, args, self, prior=prior)
