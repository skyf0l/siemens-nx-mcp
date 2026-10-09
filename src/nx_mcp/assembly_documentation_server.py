"""Assembly documentation contracts with native associations and artifact delivery."""

from __future__ import annotations

READ_ONLY: set[str] = set()
NON_MODEL = {"nx_export_explosion_animation"}


def nx_explosion_trace(
    explosion: str,
    start_edge: str,
    end_edge: str,
    start_percent: float = 50.0,
    end_percent: float = 50.0,
    start_direction: list[float] | None = None,
    end_direction: list[float] | None = None,
):
    """Create a native automatic explosion trace anchored to two unsuppressed component-occurrence edges. Percentages are 0..100 along edge arc length. Directions are in assembly coordinates and default to +Z; NX infers the trace routing from them. Trace belongs to the named explosion and can appear in its associated drawing views. Native component/edge handles persist; MCP explosion edits/show/animation refresh their exploded endpoints. After manual NX changes, call nx_show_explosion to refresh. Returns trace and endpoint IDs without moving assembled components."""


def nx_export_explosion_animation(
    explosion: str,
    path: str,
    frames: int = 12,
    fps: int = 12,
    width: int = 960,
    height: int = 600,
):
    """Render a self-contained HTML animation from assembled to current exploded poses, with native PNG frames, playback and a scrubber. Requires interactive NX with a modeling view active and an unused workspace .html path. Uses linear translation and quaternion rotation, 2..30 frames, 1..60 fps, dimensions 128..1600. Keeps the current camera fixed: frame the full motion before export. Runs serially on the NX thread and restores model/view state under an explicit temporary undo mark. Returns checksum and per-frame metadata; retrieve with nx_download_file. This is a presentation animation, not a collision-certified disassembly path."""
