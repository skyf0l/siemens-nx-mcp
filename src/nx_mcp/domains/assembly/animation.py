"""Self-contained HTML animation of an explosion (tool-surface 3.8).

Frames interpolate every unsuppressed component from its assembled to its exploded
pose (linear translation, shortest-arc rotation), refresh managed traces and are
rendered with NX Studio Image Capture from the current camera. The poses are set
inside ``tx.scratch()`` (always undone); the view's explosion is put back and the
exploded and assembled poses are read back unchanged afterwards.

- ``fit`` frames the whole motion: NX fits the assembled solids (``kit.display.fit``),
  then the scale shrinks by the ratio of their screen extents to those of the assembled
  and exploded boxes together (geometry queries answer at the assembled pose, so the
  exploded boxes are the assembled ones moved by each component's explosion). The
  camera is put back afterwards.
- Datums, sketches and the WCS triad are hidden in the frames (``hide_datums``).
- A component an assembly cut promoted shows no solid of its own (the cut result is an
  assembly body that never moves): frames of such components show nothing, so an
  explosion whose moved components show no solid is refused and a partial one warned.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import shutil
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, PathTo, Range
from nx_mcp.domains.assembly import common, exploded, structure
from nx_mcp.kit import bodies, capture, display, drawing, files, geom, measure, readback
from nx_mcp.kit import pose as kpose

PAGE = (
    '<!doctype html><meta charset="utf-8"><title>NX explosion animation</title>'
    "<style>body{font:16px system-ui;background:#eee;margin:2rem}"
    "img{display:block;max-width:100%;margin:auto}button,input{margin:1rem}input{width:60%}"
    '</style><h1>Assembly explosion</h1><img id="frame" alt="NX assembly animation frame">'
    '<button id="play">Pause</button><input id="seek" type="range" min="0" max="@LAST@" '
    'value="0"><span id="label"></span><script>const frames=@FRAMES@;let i=0,playing=true;'
    'const image=document.getElementById("frame"),seek=document.getElementById("seek"),'
    'button=document.getElementById("play");function show(){image.src=frames[i];seek.value=i;'
    'document.getElementById("label").textContent=(i+1)+" / "+frames.length}'
    'button.onclick=()=>{playing=!playing;button.textContent=playing?"Pause":"Play"};'
    "seek.oninput=()=>{i=Number(seek.value);show()};"
    "setInterval(()=>{if(playing){i=(i+1)%frames.length;show()}},@DELAY@);show();</script>"
)


FIT_MARGIN = 0.95  # a little room around the framed motion


class Frame(TypedDict):
    index: int
    fraction: float
    size: int
    sha256: str


class AnimationData(TypedDict):
    path: str
    size: int
    sha256: str
    frame_count: int
    fps: int
    width: int
    height: int
    trace_count: int
    frames: list[Frame]


def html(images, fps) -> str:
    return (
        PAGE.replace("@LAST@", str(len(images) - 1))
        .replace("@FRAMES@", json.dumps(images))
        .replace("@DELAY@", repr(1000.0 / fps))
    )


def _solids(component) -> list:
    return [b for b in bodies.occurrences(component) if getattr(b, "IsSolidBody", False)]


def _check_solids(tx, entries) -> None:
    """Refuse an explosion whose moved components show no solid (warn for some)."""
    moved = [
        (child, c)
        for child, c, _n, _h in entries
        if not kpose.close(exploded.exploded_pose(child), structure.pose(c), 1e-9)
    ]
    empty = [c for _child, c in moved if not _solids(c)]
    if not moved or not empty:
        return
    names = ", ".join(str(c.Name) for c in empty[:5])
    if len(empty) == len(moved):
        fail(
            "NX_WRONG_GEOMETRY",
            f"No moved component shows a solid ({names}): the frames would show none",
            field="/explosion",
            hint="An assembly cut promotes its components into the assembly (the cut result "
            "does not explode); an empty reference set or blanked bodies hide them too",
        )
    tx.warn("NX_UPDATE_WARNING", f"Moved components that show no solid (they stay invisible "
            f"in the frames): {names}")  # fmt: skip


def _corners(low, high) -> list:
    return [[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1])
            for z in (low[2], high[2])]  # fmt: skip


def _motion_points(tx, entries) -> list:
    """Box corners of every component's solids at its assembled and exploded poses."""
    points: list = []
    for child, c, _n, _h in entries:
        solids = _solids(c)
        if not solids:
            continue
        box = _corners(*measure.bounds(tx, solids))
        t, r = kpose.compose(exploded.exploded_pose(child), kpose.inverse(structure.pose(c)))
        points.extend(box)
        points.extend(geom.add(geom.matvec(r, p), t) for p in box)
    return points


def _extents(points, axes) -> list[float]:
    out = []
    for axis in (axes["right"], axes["up"]):
        along = [geom.dot(p, axis) for p in points]
        out.append(max(along) - min(along))
    return out


def _fit_motion(tx, view, points) -> None:
    """Frame the assembled model, then zoom out until ``points`` fit too."""
    shown = display.shown_bodies(tx)
    if not shown or not points:
        return
    display.fit(tx, view)
    cam = display.camera(tx, view)
    base = _corners(*measure.bounds(tx, shown))
    every = base + points
    axes = display.view_axes(cam["rotation"])
    ratios = [
        a / b for a, b in zip(_extents(base, axes), _extents(every, axes), strict=True) if b > 1e-9
    ]
    low = [min(p[i] for p in every) for i in range(3)]
    high = [max(p[i] for p in every) for i in range(3)]
    centre = [(a + b) / 2.0 for a, b in zip(low, high, strict=True)]
    scale = cam["scale"] * min([1.0, *ratios]) * FIT_MARGIN
    display.set_camera(tx, view, cam["rotation"], display.origin_for(cam["rotation"], centre),
                       scale)  # fmt: skip


@contextlib.contextmanager
def _clean_view(tx, part, view, hide):
    """Datums, sketches and the WCS hidden for the block (the scratch undo restores the
    blanking too)."""
    clutter = display.capture_clutter(tx, part) if hide else []
    wcs = getattr(part, "WCS", None) if hide else None
    shown_wcs = False
    try:
        for obj in clutter:
            with contextlib.suppress(Exception):
                obj.Blank()
        if wcs is not None:
            with contextlib.suppress(Exception):
                shown_wcs = bool(wcs.Visibility)
                wcs.Visibility = False
        if clutter or shown_wcs:
            with contextlib.suppress(Exception):
                view.UpdateDisplay()
        yield
    finally:
        for obj in clutter:
            with contextlib.suppress(Exception):
                obj.Unblank()
        if shown_wcs:
            with contextlib.suppress(Exception):
                wcs.Visibility = True


def _state(tx, ex) -> dict:
    return {
        tag: (exploded.exploded_pose(child), structure.pose(component))
        for tag, (child, component, _n, _h) in exploded.tree(ex).items()
    }


@tool(
    effect="file",
    label="Export explosion animation",
    summary="Animate {explosion} to HTML {path}",
    keywords=(
        "explosion animation",
        "exploded animation",
        "html animation",
        "assembly motion",
        "exploded view movie",
        "assembly video",
        "animate explosion",
    ),
    requires=("work_part", "no_active_sketch", "assembly", "interactive"),
    cost="long",
    example={"explosion": "ex:3", "path": "out/explode.html", "frames": 8},
)
def nx_export_explosion_animation(
    tx: Tx,
    *,
    explosion: common.ExplosionId,
    path: Annotated[str, Doc("New .html file (no GIF/MP4)"), PathTo("new", ".html")],
    frames: Annotated[int, Range(ge=2, le=30), Doc("Frames from assembled to exploded, 2-30")] = 12,
    fps: Annotated[int, Range(ge=1, le=60), Doc("Playback frames per second")] = 12,
    width: Annotated[int, Range(ge=128, le=1600), Doc("Frame width in pixels")] = 960,
    height: Annotated[int, Range(ge=128, le=1600), Doc("Frame height in pixels")] = 600,
    fit: Annotated[bool, Doc("Frame the whole motion; false: the current camera")] = True,
    hide_datums: Annotated[bool, Doc("Hide datums, sketches and the WCS in the frames")] = True,
) -> AnimationData:
    """Write a self-contained HTML page animating an explosion from assembled to exploded,
    with its trace lines. fit frames the assembled and exploded states from the current
    view direction (camera put back after); moved components must show solids. The model
    is left unchanged."""
    with tx.checks():
        part = common.explosion_context(tx)
        if drawing.current(part) is not None or drawing.work_view_is_drawing(part):
            fail("NX_WRONG_APPLICATION", "A drawing sheet is shown",
                 hint="Show the model with nx_set_explosion_display or close the sheet")  # fmt: skip
        ex = common.explosion(tx, explosion)
        entries = [e for e in exploded.tree(ex).values() if not e[3]]
        if not entries:
            fail("NX_INVALID_ARGUMENT", "The explosion has no unsuppressed component",
                 field="/explosion")  # fmt: skip
        _check_solids(tx, entries)
    points = _motion_points(tx, entries) if fit else []
    trace_count = len(exploded.traces(tx, part, ex))
    poses = [
        (int(c.Tag), structure.pose(c), exploded.exploded_pose(child))
        for child, c, _n, _h in entries
    ]
    before = _state(tx, ex)
    target = tx.files.stage(path)
    folder = target.parent / (target.stem + "_frames")
    folder.mkdir(parents=True, exist_ok=True)
    view = part.ModelingViews.WorkView
    api = exploded.uf(tx)
    original = int(api.AskViewExplosion(view.Tag) or 0)
    camera = display.camera(tx, view) if points else None
    images: list[str] = []
    report: list[Frame] = []
    try:
        with tx.scratch(), _clean_view(tx, part, view, hide_datums):
            with native(default="NX_BUILD_FAILED"):
                api.SetViewExplosion(view.Tag, ex.Tag)
            for index in range(frames):
                tx.checkpoint(done=index, total=frames)
                fraction = index / (frames - 1)
                exploded.apply(
                    tx, ex, [(tag, kpose.interpolate(a, b, fraction)) for tag, a, b in poses]
                )
                mark = tx.session.SetUndoMark(
                    tx.nx.Session.MarkVisibility.Invisible, "NX MCP: animation frame"
                )
                exploded.refresh_traces(tx, ex, mark)
                exploded.regenerate(tx)
                if index == 0 and points:
                    _fit_motion(tx, view, points)
                frame = folder / f"{index:03d}.png"
                capture.studio_png(tx, frame, width, height, style="shaded_with_edges")
                data = frame.read_bytes()
                images.append("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
                report.append({"index": index, "fraction": fraction, "size": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()})  # fmt: skip
    finally:
        try:
            api.SetViewExplosion(view.Tag, original)
            exploded.regenerate(tx)
            if camera is not None:
                with contextlib.suppress(Exception):
                    display.set_camera(tx, view, camera["rotation"], camera["origin"],
                                       camera["scale"])  # fmt: skip
            view.UpdateDisplay()
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    after = _state(tx, ex)
    readback.expect(set(after) == set(before), "restored", "The explosion changed membership")
    for tag, (was_exploded, was_assembled) in before.items():
        now_exploded, now_assembled = after[tag]
        readback.expect(
            kpose.close(was_exploded, now_exploded, 1e-7)
            and kpose.close(was_assembled, now_assembled, 1e-7),
            "restored", "A pose was not restored after the animation",
        )  # fmt: skip
    readback.expect(int(api.AskViewExplosion(view.Tag) or 0) == original, "view")
    target.write_text(html(images, fps), encoding="utf-8")
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    return {
        **meta,
        "frame_count": frames,
        "fps": fps,
        "width": width,
        "height": height,
        "trace_count": trace_count,
        "frames": report,
    }
