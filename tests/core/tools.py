"""Throw-away catalogue tools that exercise every kernel gate on ``FakeServices``."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import fail
from nx_mcp.core.types import (
    Angle,
    Doc,
    Items,
    Length,
    Name,
    PathTo,
    Ref,
    RefTo,
    SheetLength,
    SheetPoint,
    Text,
    Units,
)


class Count(TypedDict):
    count: int


class Made(TypedDict):
    feature: dict
    size: float


class Written(TypedDict):
    path: str


class Children(TypedDict):
    results: list


def nx_get_count(tx, *, kind: Literal["feature", "body"] = "feature") -> Count:
    """Count objects of one kind in the work part."""
    return {"count": sum(1 for o in tx.part.objects if o.kind == kind)}


def nx_create_block(
    tx,
    *,
    size: Length,
    angle: Angle = 0,
    name: Name | None = None,
    fail_with: Annotated[str, Text(40), Doc("Code to raise after the change")] | None = None,
    error_feature: bool = False,
) -> Made:
    """Create a block feature with one body."""
    if size <= 0:
        fail("NX_INVALID_ARGUMENT", "size must be positive", field="/size")
    feature = tx.part.add("feature", name or "BLOCK")
    tx.part.add("body", "")
    if error_feature:
        feature.errors.append("inherited nonsense")
    if fail_with:
        fail(fail_with, "requested failure")
    return {"feature": tx.ref(feature), "size": size}


def nx_delete_first(tx, *, feature: Annotated[str, RefTo("feature")]) -> Count:
    """Delete one feature."""
    obj = tx.resolve(feature, "feature")
    tx.part.objects.remove(obj)
    return {"count": len(tx.part.objects)}


def nx_export_note(
    tx,
    *,
    path: Annotated[str, PathTo("new", ".txt")],
    fail_after: bool = False,
    publish_then_fail: bool = False,
) -> Written:
    """Write a text file to the workspace."""
    temp = tx.files.stage(path)
    temp.write_text("hello", encoding="utf-8")
    if fail_after:
        fail("NX_EXPORT_FAILED", "translator failed")
    tx.files.publish()
    if publish_then_fail:
        fail("NX_EXPORT_FAILED", "late failure")
    return {"path": path}


def nx_run_blocks(
    tx,
    *,
    sizes: Annotated[list[Length], Items(1, 10)],
    then_delete: bool = False,
) -> Children:
    """Create several blocks under one mark."""
    out = []
    for size in sizes:
        out.append(tx.run("nx_create_block", {"size": size}))
    if then_delete:
        out.append(tx.run("nx_delete_first", {"feature": "$0.data.feature"}))
    return {"results": out}


def nx_preview_blocks(tx, *, sizes: Annotated[list[Length], Items(1, 10)]) -> Children:
    """Preview several blocks and roll them back."""
    out = []
    with tx.preview():
        for size in sizes:
            out.append(tx.run("nx_create_block", {"size": size}))
    return {"results": out}


class Placed(TypedDict):
    origin: list
    height: float
    units: dict
    sheet: str


def nx_create_label(
    tx,
    *,
    origin: SheetPoint,
    height: SheetLength = 3.5,
    sheet: Annotated[str, RefTo("drawing_sheet")] | None = None,
    view: Annotated[str, RefTo("drawing_view")] | None = None,
) -> Placed:
    """Place a label on a drawing sheet."""
    return {
        "origin": list(origin),
        "height": height,
        "units": dict(tx.units),
        "sheet": getattr(tx.sheet, "Name", None),
    }


class NewSheet(TypedDict):
    width: float
    units: dict


def nx_create_sheet(tx, *, width: SheetLength, units: Units = "mm") -> NewSheet:
    """Create a drawing sheet in its own units."""
    return {"width": width, "units": dict(tx.units)}


class Looked(TypedDict):
    count: int


def nx_set_look(
    tx,
    *,
    objects: Annotated[list[Ref], Items(1, 10)],
    fail_with: Annotated[str, Text(40), Doc("Code to raise after the change")] | None = None,
    keep: bool = True,
) -> Looked:
    """Hide objects, recolour them, zoom and highlight them."""
    objs = [tx.resolve(r) for r in objects]
    if keep:
        tx.display.keep(objs, appearance=True)
        tx.display.keep_camera()
        tx.display.keep_highlights()
    tx.session  # noqa: B018 - the change starts here (touched)
    services = tx.services
    for obj in objs:
        obj.blanked, obj.color = True, 1
    services.camera["scale"] = 9.0
    services.highlighted = list(objs)
    if fail_with:
        fail(fail_with, "requested failure")
    return {"count": len(objs)}


def nx_create_steps(tx, *, steps: Annotated[int, Doc("Steps")] = 3) -> Count:
    """Create features one by one with progress checkpoints."""
    for i in range(steps):
        tx.part.add("feature", f"STEP{i}")
        tx.checkpoint(done=i + 1, total=steps)
    return {"count": steps}


def nx_save_other(tx, *, part: Annotated[str, RefTo("part")]) -> Written:
    """Save a named part."""
    target = tx.resolve(part, "part")
    tx.require_owned(target, field="/part")
    target.IsModified = False
    return {"path": target.FullPath}


def nx_import_block(
    tx,
    *,
    target: Literal["work_part", "new_part"] = "work_part",
    new_part_path: Annotated[str, PathTo("new", ".prt")] | None = None,
) -> Count:
    """Import a block into the work part or into a new part."""
    if new_part_path is not None:
        from pathlib import Path

        from tests.core.fakes import FakePart

        previous = tx.services.part
        tx.services.extra_parts.append(previous)
        tx.services.part = FakePart(Path(new_part_path))
        tx.services.own(tx.services.part)  # the host owns every part a call loads
    tx.part.add("body", "IMPORTED")
    return {"count": len(tx.part.objects)}


KEYWORDS = ("alpha", "beta", "gamma")


def specs():
    return {
        "nx_get_count": make_spec(
            nx_get_count, effect="read", label="Count", keywords=KEYWORDS, domain="inspection"
        ),
        "nx_create_block": make_spec(
            nx_create_block,
            effect="model",
            label="Create block",
            summary="Block {size}",
            keywords=KEYWORDS,
            requires=("work_part", "no_active_sketch"),
            batchable=True,
            previewable=True,
            domain="modeling",
            example={"size": 10},
        ),
        "nx_delete_first": make_spec(
            nx_delete_first,
            effect="model",
            label="Delete feature",
            keywords=KEYWORDS,
            batchable=True,
            domain="modeling",
            example={"feature": "f:1"},
        ),
        "nx_import_block": make_spec(
            nx_import_block,
            effect="model",
            label="Import block",
            keywords=KEYWORDS,
            domain="parts_files",
            new_part="new_part_path",
        ),
        "nx_export_note": make_spec(
            nx_export_note,
            effect="file",
            label="Export note",
            keywords=KEYWORDS,
            domain="parts_files",
            example={"path": "x.txt"},
        ),
        "nx_run_blocks": make_spec(
            nx_run_blocks,
            effect="model",
            label="Run blocks",
            keywords=KEYWORDS,
            domain="session",
            cost="heavy",
            example={"sizes": [1]},
        ),
        "nx_create_label": make_spec(
            nx_create_label,
            effect="model",
            label="Create label",
            summary="Label {height} at {origin}",
            keywords=KEYWORDS,
            domain="drawing",
            example={"origin": [10, 10]},
        ),
        "nx_create_sheet": make_spec(
            nx_create_sheet,
            effect="model",
            label="Create sheet",
            summary="Sheet {width} wide",
            keywords=KEYWORDS,
            domain="drawing",
            example={"width": 297},
        ),
        "nx_set_look": make_spec(
            nx_set_look,
            effect="view",
            label="Set look",
            keywords=KEYWORDS,
            domain="display",
            restorable=True,
            example={"objects": ["b:1"]},
        ),
        "nx_create_steps": make_spec(
            nx_create_steps,
            effect="model",
            label="Create steps",
            keywords=KEYWORDS,
            domain="modeling",
            example={},
        ),
        "nx_save_other": make_spec(
            nx_save_other,
            effect="file",
            label="Save other part",
            keywords=KEYWORDS,
            domain="parts_files",
            example={"part": "p:1"},
        ),
        "nx_preview_blocks": make_spec(
            nx_preview_blocks,
            effect="session",
            label="Preview blocks",
            keywords=KEYWORDS,
            domain="session",
            example={"sizes": [1]},
        ),
    }
