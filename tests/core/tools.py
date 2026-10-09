"""Throw-away catalogue tools that exercise every kernel gate on ``FakeServices``."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import fail
from nx_mcp.core.types import Angle, Doc, Items, Length, Name, PathTo, RefTo, Text


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


def nx_delete_first(tx, *, feature: RefTo("feature")) -> Count:  # type: ignore[valid-type]
    """Delete one feature."""
    obj = tx.resolve(feature, "feature")
    tx.part.objects.remove(obj)
    return {"count": len(tx.part.objects)}


def nx_export_note(
    tx, *, path: PathTo("new", ".txt"), fail_after: bool = False, publish_then_fail: bool = False
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
        "nx_preview_blocks": make_spec(
            nx_preview_blocks,
            effect="session",
            label="Preview blocks",
            keywords=KEYWORDS,
            domain="session",
            example={"sizes": [1]},
        ),
    }
