"""Assembly occurrences: direct children, poses, moves, load state, NX error lists.

    t, r = structure.pose(component)        # p_assembly = r p_local + t
    structure.move(tx, component, (t, r))   # absolute pose, read back
    structure.load_state(tx, component)     # {"part_path", "load_state"}
    structure.check(errors)                 # an NX ErrorList: NX_UPDATE_FAILED

Occurrence paths come from ``kit.parts.components`` (depth first, names from the
root). The NX 2406 pivot of ``MoveComponent`` is handled by ``kit.pose.move_args``.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import builders, display, geom, parts, readback
from nx_mcp.kit import pose as kpose


def root(part):
    """The root component of ``part``'s assembly, or None (not an assembly)."""
    return getattr(getattr(part, "ComponentAssembly", None), "RootComponent", None)


def direct_children(part) -> list:
    top = root(part)
    return list(top.GetChildren()) if top is not None else []


def is_direct_child(part, component) -> bool:
    return display.same(getattr(component, "Parent", None), root(part))


def paths(part) -> dict:
    """``{tag: [names from the root]}`` of every occurrence of ``part``."""
    return {int(c.Tag): names for c, names in parts.components(part)}


def pose(component):
    """``(translation, row-major rotation)`` of an occurrence in assembly coordinates."""
    position, matrix = component.GetPosition()
    return geom.xyz(position), [list(map(float, row)) for row in geom.rows(matrix)]


def move(tx, component, target, *, check="pose"):
    """Move ``component`` (a child of the work part's root) to the absolute ``target`` pose
    and read it back; returns the pose NX reports."""
    current = pose(component)
    shift, delta = kpose.move_args(current, target)
    with native(default="NX_BUILD_FAILED"):
        tx.part.ComponentAssembly.MoveComponent(
            component, tx.nx.Vector3d(*shift), builders.matrix(tx, delta)
        )
    got = pose(component)
    expect_pose(got, target, check)
    return got


def expect_pose(got, wanted, check="pose"):
    """Read-back: translation within the scaled tolerance, rotation within 1e-6."""
    readback.expect_close(got[0], wanted[0], f"{check}.translation", tol=geom.tol(wanted[0]))
    flat = [v for row in got[1] for v in row]
    readback.expect_close(flat, [v for row in wanted[1] for v in row], f"{check}.rotation", 1e-6)


def load_state(tx, component) -> dict:
    """``{"part_path", "load_state"}`` without assuming the prototype is loaded."""
    prototype = getattr(component, "Prototype", None)
    path = parts.file_of(prototype) if prototype is not None else ""
    if path:
        state = "fully_loaded" if getattr(prototype, "IsFullyLoaded", True) else "partially_loaded"
        return {"part_path": path, "load_state": state}
    with contextlib.suppress(Exception):
        path = str(tx.uf.Assem.AskComponentData(component.Tag)[0] or "")
    return {"part_path": path or None, "load_state": "unloaded"}


def check(errors, code="NX_UPDATE_FAILED", message="NX reported errors", hint=None):
    """Raise ``code`` when an NX ``ErrorList`` holds entries; always disposes it."""
    if errors is None:
        return
    try:
        count = int(getattr(errors, "Length", 0) or 0)
        if count:
            texts, codes = [], []
            for i in range(count):
                with contextlib.suppress(Exception):
                    text, nx_code = error_text(errors.GetErrorInfo(i))
                    texts.append(text)
                    codes.append(nx_code)
            first = next((c for c in codes if c is not None), None)
            fail(
                code,
                f"{message}: " + ("; ".join(texts) or "NX gave no reason"),
                hint=hint,
                details={"errors": texts},
                nx_code=first,
            )
    finally:
        with contextlib.suppress(Exception):
            errors.Dispose()


def error_text(info) -> tuple[str, int | None]:
    """``(message, NX error code)`` of an ``NXOpen.ErrorInfo`` (its ``str`` is only
    ``<NXOpen.ErrorInfo object at 0x..>``); a plain string passes through."""
    if isinstance(info, str):
        return info, None
    nx_code = None
    with contextlib.suppress(Exception):
        raw = getattr(info, "ErrorCode", None)
        nx_code = int(raw) if raw is not None else None
    text = ""
    for attr in ("Description", "ErrorObjectDescription"):
        with contextlib.suppress(Exception):
            value = getattr(info, attr, None)
            if value and not callable(value):
                text = str(value).strip()
                break
    if not text:
        plain = str(info)
        text = "" if " object at 0x" in plain else plain
    if nx_code is not None:
        text = f"{text} (NX error {nx_code})" if text else f"NX error {nx_code}"
    return text or "NX gave no reason", nx_code
