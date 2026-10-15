"""Measured sheet-metal callouts (Sheet Metal PMI) and their refresh.

``nx_create_sheet_metal_callout`` writes the measured thickness or bend data (radius,
angle, neutral factor) as Sheet Metal PMI text. With ``automatic`` the callout is
managed: ``@refresher("annotation")`` re-measures it after every model change and in
``nx_update_annotations``, and rewrites the text when a value changed.

NX 2406 findings encoded here:

- Without the Sheet Metal PMI license, setting ``SheetMetalPMIBuilder.PMIType`` raises
  native 948801, reported as ``NX_LICENSE_UNAVAILABLE`` and rolled back.
- PMI created while a drawing sheet is shown becomes a sheet annotation, so the modeling
  display is shown for the commit and the sheet reopened afterwards.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import refresher, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, FaceRef, Items, Point3
from nx_mcp.domains.sheet_metal import common
from nx_mcp.kit import builders, drawing, geom, model, readback, result, select

MANAGED = "NX_MCP_MEASURED_PMI_V1"
KINDS = {"body": "Body", "bend": "Bend"}


class CalloutData(TypedDict):
    annotations: list[result.Row]
    kind: str
    body: result.Row
    measured: dict[str, Any]
    text: list[str]
    automatic: bool


def measure(tx, kind, body, faces) -> dict:
    if kind == "body":
        return {"thickness": common.thickness(tx, body)}
    return {"bends": [common.bend_data(tx, face) for face in faces]}


def lines(kind, measured, units, automatic) -> list[str]:
    note = "managed measurement" if automatic else "measured snapshot"
    if kind == "body":
        return [f"Sheet metal ({note})", f"Thickness: {measured['thickness']:.3f} {units}"]
    out = [f"Bend data ({note})"]
    for n, bend in enumerate(measured["bends"], 1):
        out.append(
            f"Bend {n}: R {bend['radius']:.3f} {units}; angle {bend['angle']:.3f} deg; "
            f"K {bend['neutral_factor']:.3f}"
        )
    return out


def _handles(tx, objects) -> list[str]:
    return [str(tx.uf.Tag.AskHandleFromTag(o.Tag)) for o in objects]


def _commit(tx, existing, kind, body, faces, text, position):
    """Create (``existing`` None) or rewrite one Sheet Metal PMI; its committed objects."""
    pmi = tx.nx.Annotations.SheetMetalPMIBuilder
    factory = common.manager(tx).CreateSheetMetalPmiBuilder
    with drawing.in_modeling(tx), builders.build(tx, factory, existing) as b:
        b.PMIType = getattr(pmi.Types, KINDS[kind])
        b.SelectedBody.Value = body
        b.SelectedFace.Clear()
        if faces:
            b.SelectedFace.Add(list(faces))
        b.AssociatedObjects.Nxobjects.Clear()
        b.AssociatedObjects.Nxobjects.Add(list(faces) or [body])
        b.Text.TextBlock.SetText(list(text))
        b.Origin.SetInferRelativeToGeometry(True)
        b.Origin.Origin.SetValue(None, None, tx.nx.Point3d(*(float(v) for v in position)))
        made = b.commit(validate=True)
        values = list(b.GetCommittedObjects()) or ([made] if made is not None else [])
    readback.expect(bool(values), "annotations", "NX returned no sheet-metal callout")
    return values


@tool(
    switch="display",
    effect="model",
    label="Create sheet-metal callout",
    summary={"body": "Thickness callout on {body}", "bend": "Bend callout on {faces}"},
    keywords=(
        "sheet metal callout",
        "bend callout",
        "sheet metal pmi",
        "thickness callout",
        "bend note",
        "bend annotation",
        "k factor",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="light",
    invalidates="deleted",
    refresh_annotations=False,
    example={"kind": "body", "body": "b:4", "position": [50, -20, 0]},
)
def nx_create_sheet_metal_callout(
    tx: Tx,
    *,
    kind: Literal["body", "bend"],
    body: BodyRef,
    faces: Annotated[list[FaceRef], Items(1, 50), Doc("Bend faces (kind=bend)")] | None = None,
    position: Point3,
    automatic: Annotated[bool, Doc("Re-measure after model changes")] = True,
) -> CalloutData:
    """Place a Sheet Metal PMI callout with the measured thickness or bend data. Bend data is
    radius, angle and neutral factor; automatic callouts follow later model changes."""
    if kind == "body" and faces:
        fail("NX_INVALID_ARGUMENT", "A body callout takes no faces", field="/faces")
    if kind == "bend" and not faces:
        fail("NX_INVALID_ARGUMENT", "A bend callout needs bend faces", field="/faces")
    with tx.checks():
        target = common.sheet_body(tx, body, "/body")
        selected = select.faces(tx, faces, "/faces") if faces else []
        bends = {int(f.Tag) for f, _ in common.bends(tx, target)}
        for i, face in enumerate(selected):
            if int(face.Tag) not in bends:
                fail("NX_WRONG_GEOMETRY", "Select inner bend faces of the body",
                     field=f"/faces/{i}")  # fmt: skip
    measured = measure(tx, kind, target, selected)
    text = lines(kind, measured, tx.units["length"], automatic)
    values = _commit(tx, None, kind, target, selected, text, position)
    if automatic:
        record = {
            "kind": kind,
            "body": _handles(tx, [target])[0],
            "faces": _handles(tx, selected),
            "measured": measured,
            "position": [float(v) for v in position],
        }
        for value in values:
            value.SetAttribute(MANAGED, json.dumps(record, sort_keys=True))
    model.update(tx)
    return {
        "annotations": [tx.ref(v, "annotation") for v in values],
        "kind": kind,
        "body": tx.ref(target, "body"),
        "measured": measured,
        "text": text,
        "automatic": automatic,
    }


# ---------------------------------------------------------------------------
# Refresh


def _managed(tx, part) -> list:
    """Annotations of the work part that carry a managed measurement record."""
    found = [*getattr(part, "Notes", []), *getattr(part, "Labels", [])]
    collections = getattr(part, "Annotations", None)
    for name in ("PmiLabels", "PmiNotes"):
        try:
            found.extend(getattr(collections, name, None) or [])
        except Exception:  # a collection this NX build cannot enumerate
            continue
    if not found:
        return []
    string = tx.nx.NXObject.AttributeType.String
    out = []
    for obj in {int(o.Tag): o for o in found}.values():
        try:
            if obj.HasUserAttribute(MANAGED, string, -1):
                out.append(obj)
        except Exception:
            continue
    return out


def _live(tx, part, record):
    """``(body, faces)`` named by a managed record, or ``NX_STALE_SOURCE``."""
    tags = tx.uf.Tag
    try:
        body_tag = int(tags.AskTagOfHandle(record["body"]))
        face_tags = [int(tags.AskTagOfHandle(h)) for h in record["faces"]]
        body = next(b for b in part.Bodies if int(b.Tag) == body_tag)
        faces = [next(f for f in body.GetFaces() if int(f.Tag) == t) for t in face_tags]
    except Exception as error:
        fail("NX_STALE_SOURCE", "A managed sheet-metal callout lost its body or bend faces",
             hint="Delete the callout with nx_delete_objects and create it again",
             details={"error": str(error)})  # fmt: skip
    return body, faces


def _unchanged(a, b) -> bool:
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_unchanged(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_unchanged(x, y) for x, y in zip(a, b, strict=True))
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))


@refresher("annotation")
def refresh_callouts(tx, objects):
    """Re-measure managed sheet-metal callouts; rewrite those whose values changed."""
    part = tx.part
    wanted = None if objects is None else {int(o.Tag) for o in objects}
    out = []
    for obj in _managed(tx, part):
        if wanted is not None and int(obj.Tag) not in wanted:
            continue
        raw = obj.GetStringAttribute(MANAGED)
        if raw == "disabled":
            continue
        record = json.loads(raw)
        body, faces = _live(tx, part, record)
        measured = measure(tx, record["kind"], body, faces)
        if _unchanged(measured, record["measured"]) and objects is None:
            continue
        text = lines(record["kind"], measured, tx.units.get("length") or "", True)
        position = geom.xyz(obj.AnnotationOrigin)
        for value in _commit(tx, obj, record["kind"], body, faces, text, position):
            value.SetAttribute(MANAGED, json.dumps({**record, "measured": measured},
                                                   sort_keys=True))  # fmt: skip
            out.append(tx.ref(value, "annotation"))
        tx.checkpoint()
    return out
