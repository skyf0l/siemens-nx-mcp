"""NX-process command executor and manual bridge lifecycle."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nx_mcp.bridge import (
    BRIDGE_PROTOCOL_VERSION,
    BridgeDescriptor,
    BridgeServer,
    MainThreadDispatcher,
    ObjectRegistry,
    default_descriptor_path,
)
from nx_mcp.runtime import NXToolError, ObjectKind, api_error
from nx_mcp.workspace import Workspace


class NXOpenExecutor:
    """Executes the certified bridge commands against one live NX session."""

    _MODEL_MUTATIONS: set[str] = set()

    def __init__(
        self,
        session: Any,
        nxopen: Any,
        nx_version: str,
        workspace: Workspace,
    ) -> None:
        self.session = session
        self.nxopen = nxopen
        self.nx_version = nx_version
        self.workspace = workspace
        self.objects = ObjectRegistry()
        self._undo_marks: list[Any] = []
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "nx_status": self._status,
            "nx_list_sketches": lambda: self._list_objects("Sketches", "sketch"),
            "nx_list_bodies": lambda: self._list_objects("Bodies", "body"),
            "nx_list_features": lambda: self._list_objects("Features", "feature"),
            "nx_undo": self._undo,
        }

    def execute(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if k != "intent"}
        handler = self._handlers.get(method)
        if handler is None:
            raise NXToolError("NX_TOOL_NOT_FOUND", f"Unsupported bridge command: {method}")
        undo_mark = None
        try:
            if method in self._MODEL_MUTATIONS:
                undo_mark = self.session.SetUndoMark(
                    self.nxopen.Session.MarkVisibility.Visible,
                    f"NX MCP: {method}",
                )
            result = handler(**params)
            if undo_mark is not None:
                self._undo_marks.append(undo_mark)
            return result
        except NXToolError as error:
            if undo_mark is not None:
                self._rollback(undo_mark, error)
            raise
        except Exception as error:
            if undo_mark is not None:
                self._rollback(undo_mark, error)
            raise api_error(error) from error

    def _rollback(self, undo_mark: Any, original_error: Exception) -> None:
        try:
            self.session.UndoToMark(undo_mark, None)
        except Exception as rollback_error:
            raise NXToolError(
                "NX_ROLLBACK_FAILED",
                f"Operation failed and rollback also failed: {rollback_error}",
                details={"operation_error": str(original_error)},
            ) from rollback_error

    def _work_part(self, *, required: bool = True) -> Any | None:
        part = getattr(self.session.Parts, "Work", None)
        if part is None and required:
            raise NXToolError(
                "NX_NO_WORK_PART",
                "No work part is open.",
                suggestion="Open or create a part with nx_open_part or nx_create_part.",
            )
        return part

    @staticmethod
    def _name(value: Any, fallback: str) -> str:
        name = getattr(value, "Name", fallback)
        return str(name() if callable(name) else name)

    @staticmethod
    def _part_id(part: Any) -> str:
        native_id = getattr(part, "Tag", None)
        return f"part_{native_id if native_id is not None else id(part)}"

    def _reference(self, value: Any, kind: ObjectKind, part: Any, fallback: str) -> dict[str, Any]:
        return self.objects.register(
            value,
            kind=kind,
            name=self._name(value, fallback),
            part_id=self._part_id(part),
        ).model_dump()

    def _status(self) -> dict[str, Any]:
        part = self._work_part(required=False)
        return {
            "connected": True,
            "nx_version": self.nx_version,
            "bridge_protocol": BRIDGE_PROTOCOL_VERSION,
            "active_part": self._reference(part, "part", part, "Part") if part else None,
        }

    def _list_objects(self, collection_name: str, kind: ObjectKind) -> dict[str, Any]:
        part = self._work_part()
        collection = getattr(part, collection_name)
        values = list(collection)
        return {
            "objects": [self._reference(value, kind, part, kind.title()) for value in values],
            "message": f"Found {len(values)} {kind}(s).",
        }

    def _undo(self) -> dict[str, Any]:
        if not self._undo_marks:
            raise NXToolError("NX_UNDO_UNAVAILABLE", "No NX MCP operation is available to undo")
        part = self._work_part()
        self.session.UndoToMark(self._undo_marks.pop(), None)
        self.objects.invalidate_part(self._part_id(part))
        return {"message": "Undo successful"}

    def _find_sketch(self, name: str) -> Any:
        part = self._work_part()
        for sketch in part.Sketches:
            sketch_name = self._name(sketch, "Sketch")
            if sketch_name == name or sketch_name.endswith(name):
                return sketch
        raise NXToolError("NX_NOT_FOUND", f"Sketch not found: {name}")

    def _resolve_body(self, value: str, part: Any) -> Any:
        try:
            return self.objects.resolve(
                value,
                expected_kind="body",
                part_id=self._part_id(part),
            )
        except NXToolError:
            pass
        bodies = list(part.Bodies)
        for index, body in enumerate(bodies, start=1):
            names = {
                self._name(body, f"body_{index}"),
                f"body_{index}",
                str(getattr(body, "JournalIdentifier", "")),
            }
            if value in names:
                return body
        raise NXToolError("NX_NOT_FOUND", f"Body not found: {value}")


@dataclass
class BridgeRuntime:
    server: BridgeServer
    dispatcher: MainThreadDispatcher
    descriptor: BridgeDescriptor
    descriptor_path: Path

    def stop(self) -> None:
        self.dispatcher.stop()
        self.server.stop()
        try:
            current = BridgeDescriptor.read(self.descriptor_path)
            if current.token == self.descriptor.token:
                self.descriptor_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


_runtime: BridgeRuntime | None = None


def _detect_nx_version(session: Any) -> str:
    try:
        value = session.GetEnvironmentVariableValue("UGII_VERSION")
        if value:
            return str(value)
    except Exception:
        pass
    return "NX build unknown"


def start_bridge(
    workspace_root: str | Path,
    *,
    descriptor_path: str | Path | None = None,
    allow_unverified_threading: bool = False,
) -> BridgeDescriptor:
    """Start the Python feasibility bridge from inside NX.

    NXOpen threading must be validated on the target build before this gate is
    enabled for a pilot. If it fails, the agreed fallback is a minimal C# bridge.
    """
    global _runtime
    if _runtime is not None:
        return _runtime.descriptor
    if (
        not allow_unverified_threading
        and os.environ.get("NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE") != "1"
    ):
        raise RuntimeError(
            "Python bridge execution has not been verified on this NX build. "
            "Set NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE=1 only for the real-NX feasibility test."
        )

    import NXOpen

    session = NXOpen.Session.GetSession()
    from nx_mcp.hardened import HardenedExecutor

    executor = HardenedExecutor(
        session,
        NXOpen,
        _detect_nx_version(session),
        Workspace(workspace_root),
    )
    token = secrets.token_hex(32)
    dispatcher = MainThreadDispatcher(executor.execute)
    server = BridgeServer(
        dispatcher.call,
        token=token,
        result_directory=Path(workspace_root) / ".nx-mcp" / "bridge-results",
    )
    server.start()
    descriptor = BridgeDescriptor.create(
        server.port,
        executor.nx_version,
        token=token,
    )
    destination = Path(descriptor_path) if descriptor_path else default_descriptor_path()
    descriptor.write(destination)
    _runtime = BridgeRuntime(server, dispatcher, descriptor, destination)
    return descriptor


def pump_bridge(timeout: float = 0.1) -> int:
    """Run pending bridge calls on the NX journal's main thread."""
    if _runtime is None:
        return 0
    return _runtime.dispatcher.drain(timeout=timeout)


def stop_bridge() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.stop()
        _runtime = None
