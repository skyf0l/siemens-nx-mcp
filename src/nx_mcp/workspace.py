"""Filesystem boundary for NX MCP file operations."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

from nx_mcp.core.types import SAME_KIND, suffix_kinds, suffix_of

__all__ = [
    "SAME_KIND",
    "WORKSPACE_ENV",
    "Workspace",
    "WorkspaceViolation",
    "default_workspace",
    "kinds",
    "relative",
    "scrub",
    "suffix_of",
]

WORKSPACE_ENV = "NX_MCP_WORKSPACE"


def default_workspace() -> Path:
    """The workspace NX and the sidecar share: NX_MCP_WORKSPACE, else <home>/NX_MCP_WORKSPACE.

    Bootstraps that run before nx_mcp is importable use the same stdlib expression,
    ``os.environ.get("NX_MCP_WORKSPACE") or str(Path.home() / "NX_MCP_WORKSPACE")``.
    """
    return Path(os.environ.get(WORKSPACE_ENV) or Path.home() / "NX_MCP_WORKSPACE")


class WorkspaceViolation(ValueError):
    """Raised when a requested path escapes the configured workspace."""


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """Resolve an NX-host absolute or workspace-relative path, never a client path."""
        requested = Path(path)
        windows = PureWindowsPath(path)
        # On POSIX a Windows drive would otherwise become an ordinary directory.
        # On Windows reject drive-relative and root-relative paths (C:foo, \foo).
        if windows.drive and not requested.is_absolute():
            raise WorkspaceViolation("Use an absolute NX-host path inside the workspace")
        if windows.root and not requested.is_absolute():
            raise WorkspaceViolation("Use a complete NX-host path inside the workspace")
        return self.ensure_inside(requested if requested.is_absolute() else self.root / requested)

    def ensure_inside(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise WorkspaceViolation("Path must stay inside the configured workspace")
        if any(part.casefold() == ".nx-mcp" for part in resolved.relative_to(self.root).parts):
            raise WorkspaceViolation("Internal NX MCP state is not a user artifact")
        return resolved


# ---------------------------------------------------------------------------
# Path rules shared by both process boundaries (gate 2): suffixes, folders, and paths
# shown workspace-relative. The sidecar applies them before a call leaves; the kernel
# keeps its own copy of the checks for direct calls.

# Suffix rules (SAME_KIND, kinds, suffix_of) are core.types' own: the kernel applies the
# same ones.
kinds = suffix_kinds


def relative(root, path) -> str:
    """``path`` relative to the workspace ``root`` with ``/`` separators, else its name
    (a host path outside the workspace is never shown)."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix() or "."
    except (ValueError, OSError, TypeError):
        return Path(str(path)).name


def scrub(text: str, root) -> str:
    """``text`` with every absolute path inside the workspace ``root`` written relative to
    it (``<root>/smoke/a.prt`` in either separator -> ``smoke/a.prt``; the root itself
    -> ``.``): error messages and results never show the host's folders."""
    if not isinstance(text, str) or not text or root is None:
        return text
    pattern = _root_pattern(str(root))
    if pattern is None or not pattern.search(text):
        return text

    def tail(match) -> str:
        rest = match.group(1) or ""
        return rest.replace(BACKSLASH, "/") if rest else "."

    return pattern.sub(tail, text)


BACKSLASH = chr(92)
# A path's characters after the root: none of these (quotes, brackets, separators of a
# list), so a path inside a message ends where the message goes on.
_PATH_CHARS = r"""[^'"<>|*?,;()\[\]{}\s]*"""
_PATTERNS: dict = {}


def _root_pattern(root: str):
    import re

    if root not in _PATTERNS:
        try:
            resolved = str(Path(root).resolve())
        except (OSError, ValueError):
            resolved = root
        forms = {f for r in (root, resolved) for f in (r, r.replace(BACKSLASH, "/"))}
        forms = {f.rstrip("/" + BACKSLASH) for f in forms if f.strip("/" + BACKSLASH)}
        if not forms:
            _PATTERNS[root] = None
        else:
            alternatives = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
            separator = "[/" + re.escape(BACKSLASH) + "]+"
            _PATTERNS[root] = re.compile(
                f"(?:{alternatives})(?:{separator}({_PATH_CHARS}))?(?![A-Za-z0-9_.-])",
                re.IGNORECASE,
            )
    return _PATTERNS[root]
