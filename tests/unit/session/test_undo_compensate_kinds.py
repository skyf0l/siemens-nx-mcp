"""The compensating undo of sketches, curves and points (domains/session/compensate.py):
a created sketch goes with its feature and geometry, curves and points outside any
sketch are deleted, an open sketch and an in-sketch edit are refused (deleting sketch
geometry could make NX ask about the sketch's dimensions)."""

from __future__ import annotations

from typing import TypedDict

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.tx import Tx
from nx_mcp.kit.result import Row
from tests.kit import kernel as kit_kernel
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Collection, Curve, Feature, Object, Sketch
from tests.unit.session.test_undo_multipart import _tools, block, counts  # noqa: F401
from tests.unit.session.test_undo_multipart import two as two_parts


class Point(Object):
    """A point object (NXOpen.Point): ``infer_kind`` reads its class name."""


class Made(TypedDict):
    made: Row


def nx_create_test_sketch_with_line(tx: Tx) -> Made:
    """Create a sketch, its feature and one line in it in the work part."""
    part = tx.part
    sketch = Sketch(tx.session, "SKETCH")
    feature = Feature("SKETCH(1)")
    line = Curve("LINE")
    for obj in (sketch, feature, line):
        obj.OwningPart, obj.IsOccurrence = part, False
    feature.Suppressed = False
    sketch.Feature = feature  # NXOpen.Sketch.Feature
    sketch.AddGeometry(line)
    part.Sketches.append(sketch)
    part.Features.append(feature)
    part.Curves.append(line)
    return {"made": tx.ref(sketch, "sketch")}


def nx_create_test_curve(tx: Tx, *, into_sketch: bool = False) -> Made:
    """Create a line in the work part (into its first sketch when asked)."""
    part = tx.part
    line = Curve("LINE")
    line.OwningPart, line.IsOccurrence = part, False
    part.Curves.append(line)
    if into_sketch:
        part.Sketches[0].AddGeometry(line)
    return {"made": tx.ref(line, "curve")}


def nx_create_test_point(tx: Tx) -> Made:
    """Create a point in the work part."""
    part = tx.part
    point = Point("POINT")
    point.OwningPart, point.IsOccurrence = part, False
    part.Points.append(point)
    return {"made": tx.ref(point, "point")}


SPECS = [
    make_spec(
        fn,
        effect="model",
        label=label,
        keywords=("a", "b", "c"),
        domain="modeling",
        requires=("work_part",),
    )  # fmt: skip
    for fn, label in (
        (nx_create_test_sketch_with_line, "Test sketch with a line"),
        (nx_create_test_curve, "Test curve"),
        (nx_create_test_point, "Test point"),
    )
]


def two():
    """``a.prt`` and ``b.prt`` (test_undo_multipart.two) with point collections."""
    nx, a, b = two_parts()
    a.Points, b.Points = Collection(), Collection()
    return nx, a, b


@pytest.fixture(autouse=True)
def _kinds(monkeypatch):
    catalogue.load()
    for spec in SPECS:
        monkeypatch.setitem(catalogue._REGISTRY, spec.name, spec)
    # the fake's snapshot lists points of parts that have them (NX: Part.Points)
    monkeypatch.setattr(kit_kernel, "KINDS", (*kit_kernel.KINDS, ("Points", "point")))


def test_a_created_sketch_is_undone_by_deletion_with_its_feature_and_geometry():
    nx, a, b = two()
    made = run(nx, "nx_create_test_sketch_with_line", operation_id="op_a_sketch_1", part="a")
    assert made.ok, made
    created = {r["kind"] for r in made.changes["created"]}
    assert created == {"sketch", "feature", "curve"}, made.changes
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", operation_id="op_a_undo_1", part="a")
    assert undo.ok and undo.data["compensated"] is True, undo
    assert (len(a.Sketches), len(a.Features), len(a.Curves)) == (0, 0, 0)
    assert counts(b) == [(1, 1)]  # B untouched
    assert {r["kind"] for r in undo.changes["deleted"]} == {"sketch", "feature", "curve"}
    redo = run(nx, "nx_redo_operation", part="a")
    assert redo.ok and len(a.Sketches) == 1, redo


def test_an_open_sketch_is_never_deleted_by_an_undo():
    nx, a, b = two()
    assert run(nx, "nx_create_test_sketch_with_line", part="a").ok
    block(nx, "b", "op_b_block_1")
    nx.session.ActiveSketch = a.Sketches[0]
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART", refused
    assert "is still open: finish it with nx_finish_sketch" in refused.error["message"]
    assert len(a.Sketches) == 1 and refused.mutation_outcome == "not_started"


def test_loose_curves_and_points_are_deleted_and_in_sketch_curves_refused():
    nx, a, b = two()
    assert run(nx, "nx_create_test_curve", operation_id="op_a_curve_1", part="a").ok
    assert run(nx, "nx_create_test_point", operation_id="op_a_point_1", part="a").ok
    block(nx, "b", "op_b_block_1")
    depth = run(nx, "nx_list_checkpoints", part="a").data["undo_depth"]
    assert depth == 2  # both qualify for an undo by deletion
    undo = run(nx, "nx_undo_operation", part="a", steps=2)
    assert undo.ok and undo.data["compensated"], undo
    assert undo.data["undone"] == ["op_a_curve_1", "op_a_point_1"]
    assert list(a.Curves) == [] and list(a.Points) == [] and counts(b) == [(1, 1)]
    assert {r["kind"] for r in undo.changes["deleted"]} == {"curve", "point"}
    # a line added to a sketch the operation did not create is an in-sketch edit
    assert run(nx, "nx_create_test_sketch_with_line", operation_id="op_a_sketch_1", part="a").ok
    assert run(nx, "nx_create_test_curve", operation_id="op_a_curve_2", part="a",
               into_sketch=True).ok  # fmt: skip
    block(nx, "b", "op_b_block_2")
    # the depth reads receipts only: the in-sketch edit is found when the undo plans it
    assert run(nx, "nx_list_checkpoints", part="a").data["undo_depth"] == 2
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART", refused
    assert "lies in a sketch it did not create" in refused.error["message"]
    assert len(a.Curves) == 2 and len(a.Sketches) == 1
    both = run(nx, "nx_undo_operation", part="a", steps=2)  # with the sketch: it goes along
    assert both.ok and both.data["compensated"] and list(a.Curves) == [], both
    assert services(nx).history[-1]["compensation"] is True
