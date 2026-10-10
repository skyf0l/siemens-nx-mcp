"""Self-contained HTML animation of an explosion (tool-surface 3.8).

Frames interpolate every unsuppressed component from its assembled to its exploded
pose (linear translation, shortest-arc rotation), refresh managed traces and are
rendered with NX Studio Image Capture from the current camera. The poses are set
inside ``tx.scratch()`` (always undone); the view's explosion is put back and the
exploded and assembled poses are read back unchanged afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, ExplosionRef, PathTo, Range
from nx_mcp.domains.assembly import common, exploded, structure
from nx_mcp.kit import capture, drawing, files, readback
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
    frames: list[Frame]


def html(images, fps) -> str:
    return (
        PAGE.replace("@LAST@", str(len(images) - 1))
        .replace("@FRAMES@", json.dumps(images))
        .replace("@DELAY@", repr(1000.0 / fps))
    )


def _state(tx, ex) -> dict:
    return {
        tag: (exploded.exploded_pose(child), structure.pose(component))
        for tag, (child, component, _n, _h) in exploded.tree(ex).items()
    }


@tool(
    effect="file",
    label="Export explosion animation",
    summary="Animate {explosion} to {path}",
    keywords=("explosion animation", "exploded animation", "html animation", "assembly motion"),
    requires=("work_part", "no_active_sketch", "assembly", "interactive"),
    cost="long",
    example={"explosion": "ex:3", "path": "out/explode.html", "frames": 8},
)
def nx_export_explosion_animation(
    tx: Tx,
    *,
    explosion: ExplosionRef,
    path: Annotated[str, PathTo("new", ".html")],
    frames: Annotated[int, Range(ge=2, le=30), Doc("Frames from assembled to exploded")] = 12,
    fps: Annotated[int, Range(ge=1, le=60), Doc("Playback frames per second")] = 12,
    width: Annotated[int, Range(ge=128, le=1600), Doc("Frame width in pixels")] = 960,
    height: Annotated[int, Range(ge=128, le=1600), Doc("Frame height in pixels")] = 600,
) -> AnimationData:
    """Write a self-contained HTML animation of an explosion, assembled to exploded. It renders
    from the current camera, so frame the motion first; the model is left unchanged."""
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
    images: list[str] = []
    report: list[Frame] = []
    try:
        with tx.scratch():
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
        "frames": report,
    }
