"""Response snapshots (``result_id``) and the ``nx-artifact://`` workspace resource.

:class:`ResultStore` keeps the full result of a compacted catalogue call
(``proxy._project``); ``nx_get_result`` and ``nx_delete_results``
(``domains/session/results.py``) read and prune it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import unquote

from nx_mcp.kit.snapshots import LOCK, maintain, settings

ARTIFACT_LIMIT = 8 * 1024 * 1024


class ResultStore:
    """Durable inspection snapshots, separate from authoritative mutation receipts."""

    def __init__(self, root):
        self.root = Path(root) / ".nx-mcp" / "agent-results"
        settings()  # Validate configuration before any mutation can be dispatched.

    def put(self, payload):
        with LOCK:
            return self._put(payload)

    def _put(self, payload):
        if len(json.dumps(payload).encode("utf-8")) > settings()["max_bytes"]:
            raise OSError("Response exceeds snapshot storage limit")
        self.root.mkdir(parents=True, exist_ok=True)
        result_id = "result_" + uuid.uuid4().hex
        path = self.root / (result_id + ".json")
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        maintain(self.root, apply=True, protect=result_id)
        return result_id

    def get(self, result_id):
        if not re.fullmatch(r"result_[a-f0-9]{32}", result_id):
            raise ValueError("Invalid result_id")
        return json.loads((self.root / (result_id + ".json")).read_text(encoding="utf-8"))


def install(mcp, workspace) -> ResultStore:
    """Register the artifact resource on ``mcp``; returns the snapshot store."""

    @mcp.resource("nx-artifact://workspace/{path}", mime_type="application/octet-stream")
    def artifact(path: str) -> bytes:
        """Read a workspace artifact (8 MiB limit); keep binary content outside model text."""
        file = workspace.resolve(unquote(path))
        if ".nx-mcp" in {p.casefold() for p in file.relative_to(workspace.root).parts}:
            raise ValueError("Internal service state is not an artifact")
        if file.stat().st_size > ARTIFACT_LIMIT:
            raise ValueError("Resource exceeds 8 MiB; use programmatic nx_download_file chunks")
        return file.read_bytes()

    return ResultStore(workspace.root)
