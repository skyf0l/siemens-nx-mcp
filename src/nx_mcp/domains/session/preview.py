"""``nx_preview_changes`` and ``nx_finish_preview``: try changes, then keep or drop them
(tool-surface 3.1).

A preview runs 1-25 ``previewable`` catalogue calls (features, expression and
feature edits, sketch operations, component placement) as children under
``tx.preview()``: every kernel gate
runs per child, a ``"$n.path"`` Ref may name an earlier step's result, and the preview's
mark is always undone. The result reports the work part before and after (solid volume,
bounds, object counts, feature health, changed expressions), each step's
created/modified/deleted objects and, with ``capture``, a PNG taken before the rollback.
Refs in the step results are stale; the input refs the steps named are re-issued
(``references``: old -> new) because the rollback invalidates the part's refs.

``nx_finish_preview`` with ``accept`` re-applies exactly the previewed calls under one
undo mark, only on the same work part and while nothing changed the model since the
preview: either nothing changed the session (the services' ``revision`` counter, bumped
once by the preview call's own commit), or the work part still has the same objects,
expressions, volume, bounds and counts as before the preview (a refused call, a rename
or another discarded preview in between bump the counter but change no geometry; the
result then warns). Otherwise ``NX_STALE_PREVIEW`` names what changed. ``discard``
drops the preview and changes nothing (``mutation_outcome`` ``not_applicable``);
discarding an expired preview is a no-op. ``preview_id`` takes the bare 32 hex digits
too. Previews live in the NX host process (the 20 newest); a host reload forgets them;
the last 200 finished ids are remembered so a repeated finish says what happened.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Text
from nx_mcp.domains.session.batch import Step, StepResult, _preflight
from nx_mcp.kit import capture as kcapture
from nx_mcp.kit import display, files, measure

MAX_STEPS = 25
KEEP = 20
REMEMBER = 200  # finished preview ids kept to explain a repeated finish
SHORT_REF = re.compile(r"^[a-z]{1,3}:[1-9][0-9]{0,11}$")
PreviewId = Annotated[
    str,
    Text(64, r"^(preview_)?[0-9a-fA-F]{32}$"),
    Doc("'preview_' + 32 hex, from nx_preview_changes"),
]

# services -> {preview_id: stored preview}, oldest first
_STORES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# services -> {preview_id: how it finished}, oldest first
_FINISHED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_FATE = {
    "accepted": "was already accepted: its changes are in the model",
    "discarded": "was already discarded",
    "stale": "expired: the model changed after it, so accept was refused",
    "evicted": f"expired: only the {KEEP} newest previews are kept",
    "failed": "was consumed by an accept that failed and was rolled back",
}


class ModelState(TypedDict):
    volume: float | None  # solid bodies of the work part, part units^3; None without solids
    bounds: dict[str, list[float]] | None  # {min, max, size}; None without bodies
    counts: dict[str, int]  # bodies, features, sketches
    health: dict[str, Any]  # {healthy, errors, warnings} from feature diagnostics


class ExpressionChange(TypedDict):
    name: str
    before: str | None  # formula
    after: str | None


class PreviewData(TypedDict, total=False):
    preview_id: str
    before: ModelState
    after: ModelState
    delta: dict[str, Any]  # volume, bodies, features, sketches (after - before)
    expressions: list[ExpressionChange]
    results: list[StepResult]  # refs in these rows are stale
    count: int
    references: dict[str, str]  # input refs re-issued after the rollback: old -> new
    image: dict[str, Any] | None  # {path, size, sha256} with capture


class FinishData(TypedDict, total=False):
    preview_id: str
    action: str
    state: str  # applied, discarded, or already_finished (a discard of a finished preview)
    results: list[StepResult]
    count: int


def _store(services) -> OrderedDict:
    return _STORES.setdefault(services, OrderedDict())


def _finish(services, preview_id: str, fate: str) -> None:
    done = _FINISHED.setdefault(services, OrderedDict())
    done[preview_id] = fate
    done.move_to_end(preview_id)
    while len(done) > REMEMBER:
        done.popitem(last=False)


def _canonical(preview_id: str) -> str:
    return "preview_" + preview_id.lower().removeprefix("preview_")


# ---------------------------------------------------------------------------
# State of the work part


def _messages(feature, name) -> list:
    method = getattr(feature, name, None)
    return list(method() or []) if callable(method) else []


def _state(tx) -> ModelState:
    part = tx.part
    with native(default="NX_ERROR"):
        found = list(part.Bodies)
        features = list(part.Features)
        sketches = list(getattr(part, "Sketches", []) or [])
        errors = sum(len(_messages(f, "GetFeatureErrorMessages")) for f in features)
        warnings = sum(len(_messages(f, "GetFeatureWarningMessages")) for f in features)
    solids = [b for b in found if getattr(b, "IsSolidBody", False)]
    volume = measure.volume(tx, solids) if solids else None
    bounds = None
    if found:
        low, high = measure.bounds(tx, found)
        bounds = {"min": low, "max": high, "size": [b - a for a, b in zip(low, high, strict=True)]}
    return {
        "volume": volume,
        "bounds": bounds,
        "counts": {"bodies": len(found), "features": len(features), "sketches": len(sketches)},
        "health": {
            "healthy": errors == 0 and warnings == 0,
            "errors": errors,
            "warnings": warnings,
        },
    }


def _expressions(tx) -> dict[str, str | None]:
    out = {}
    with native(default="NX_ERROR"):
        for e in getattr(tx.part, "Expressions", []) or []:
            out[str(e.Name)] = getattr(e, "RightHandSide", None)
    return out


def _fingerprint(tx, state: ModelState, formulas: dict) -> dict:
    """What accept compares when the session counter moved: the work part's object set
    (kind, tag), expressions and model state."""
    keys = sorted((str(k), str(t)) for k, t in tx.services.snapshot(tx.part))
    return {"objects": keys, "expressions": dict(formulas), "state": state}


def _same_state(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_state(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_state(a[k], b[k]) for k in a)
    return a == b


def _changed(then: dict, now: dict) -> list[str]:
    """Which parts of the fingerprint differ (empty: the model is as previewed)."""
    out = []
    if then["objects"] != now["objects"]:
        out.append("objects")
    if then["expressions"] != now["expressions"]:
        out.append("expressions")
    for key in ("volume", "bounds", "counts", "health"):
        if not _same_state(then["state"].get(key), now["state"].get(key)):
            out.append(key)
    return out


def _delta(before: ModelState, after: ModelState) -> dict:
    out: dict[str, Any] = {
        k: after["counts"][k] - before["counts"][k] for k in ("bodies", "features", "sketches")
    }
    out["volume"] = (
        None
        if before["volume"] is None and after["volume"] is None
        else (after["volume"] or 0.0) - (before["volume"] or 0.0)
    )
    return out


# ---------------------------------------------------------------------------
# Input refs: the rollback invalidates the part's refs, so the objects are kept


def _refs(value, found: list[str]) -> list[str]:
    if isinstance(value, str) and SHORT_REF.match(value):
        found.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _refs(v, found)
    elif isinstance(value, list):
        for v in value:
            _refs(v, found)
    return found


def _live_inputs(tx, operations) -> dict:
    """``{ref: (object, kind)}`` for every short ref the steps name; a stale or unknown
    ref fails here, before anything runs (the child gates report it the same way)."""
    out = {}
    for index, step in enumerate(operations):
        for ref in _refs(step["arguments"], []):
            if ref in out:
                continue
            try:
                obj = tx.resolve(ref)
                out[ref] = (obj, tx.kind_of(obj))
            except NXError as error:
                error.details.setdefault("child_index", index)
                error.field = error.field or f"/operations/{index}/arguments"
                raise
    return out


def _translate(value, mapping: dict):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {k: _translate(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_translate(v, mapping) for v in value]
    return value


def _reissue(tx, objects: dict) -> dict[str, str]:
    """New refs for the kept input objects (they existed before the preview's mark)."""
    out = {}
    for ref, (obj, kind) in objects.items():
        try:
            out[ref] = tx.ref(obj, kind)["id"]
        except Exception as exc:
            raise NXError(
                "NX_STALE_PREVIEW",
                f"{ref} no longer exists; create a fresh preview",
                details={"ref": ref},
            ) from exc
    return out


# ---------------------------------------------------------------------------
# Tools


def _summary(args: dict, say) -> str:
    operations = args.get("operations") or []
    count = len(operations)
    first = operations[0].get("tool") if operations and isinstance(operations[0], dict) else None
    spec = catalogue.get(first) if isinstance(first, str) else None
    head = f"Preview {count} change{'s' if count != 1 else ''}"
    if spec is None:
        return head
    return f"Preview: {spec.label}" + (f" (+{count - 1} more)" if count > 1 else "")


def _capture(tx) -> dict:
    """A viewport PNG of the changed model, fitted when the work part is displayed; the
    engineer's camera is put back afterwards."""
    root = Path(tx.workspace.root)
    target = root / "captures" / f"preview-{uuid.uuid4().hex[:12]}.png"
    temp = tx.files.stage(target)
    part = display.shown_part(tx)
    fit = display.same(part, tx.part)
    if fit:
        tx.display.keep_camera()
    try:
        if fit:
            with native(default="NX_CAPTURE_FAILED"):
                view = part.ModelingViews.WorkView
                view.Fit()
                view.UpdateDisplay()
        kcapture.viewport_png(tx, temp, 1024, 768)
    finally:
        if fit:
            tx.display.restore()
    published = tx.files.publish()[0]
    return dict(files.metadata(published, root))  # PreviewData.image stays a plain object


@tool(
    effect="session",
    label="Preview changes",
    summary=_summary,
    keywords=(
        "preview",
        "try",
        "what if",
        "dry run",
        "temporary change",
        "compare before after",
    ),  # fmt: skip
    requires=("work_part",),
    cost="heavy",
    example={
        "operations": [
            {
                "tool": "nx_create_extrude",
                "arguments": {"sketch": "s:4", "end": {"type": "distance", "value": 10}},
            },
        ]
    },  # fmt: skip
)
def nx_preview_changes(
    tx: Tx,
    *,
    operations: Annotated[list[Step], Items(1, MAX_STEPS)],
    capture: Annotated[bool, Doc("PNG of the changed model before the rollback")] = False,
) -> PreviewData:
    """Apply 1-25 previewable calls temporarily, report what changed, then roll them back.
    Reports volume, bounds, counts, health, each step's changes and an optional image.
    Keep it with nx_finish_preview(accept) while nothing else changed; step refs are stale."""
    services = tx.services
    with tx.checks():  # a refused step changes nothing
        _preflight(operations, mode="preview")
        if capture and not services.state().get("interactive"):
            fail("NX_UNSUPPORTED_OPTION", "capture needs the interactive NX session",
                 field="/capture", hint="Preview without capture")  # fmt: skip
        objects = _live_inputs(tx, operations)
        part = tx.part
        part_key = services.part_key(part)
        before = _state(tx)
        formulas = _expressions(tx)
        fingerprint = _fingerprint(tx, before, formulas)
    total = len(operations)
    tx.checkpoint(done=0, total=total)
    image = None
    with tx.preview():
        for step in operations:
            tx.run(step["tool"], step["arguments"], mode="preview")
        after = _state(tx)
        changed = _expressions(tx)
        if capture:
            image = _capture(tx)
        tx.checkpoint(done=total)
    results = [{"index": i, **child} for i, child in enumerate(tx.children)]
    references = _reissue(tx, objects)
    preview_id = "preview_" + uuid.uuid4().hex
    store = _store(services)
    store[preview_id] = {
        "operations": copy.deepcopy(operations),
        "objects": objects,
        "part": part_key,
        # This call's own commit (Services.after_commit) bumps the counter once.
        "revision": services.revision() + 1,
        "fingerprint": fingerprint,
    }
    while len(store) > KEEP:
        _finish(services, store.popitem(last=False)[0], "evicted")
    names = sorted(set(formulas) | set(changed))
    return {
        "preview_id": preview_id,
        "before": before,
        "after": after,
        "delta": _delta(before, after),
        "expressions": [
            {"name": n, "before": formulas.get(n), "after": changed.get(n)}
            for n in names
            if formulas.get(n) != changed.get(n)
        ],
        "results": results,  # type: ignore[typeddict-item]  # child result dicts
        "count": len(results),
        "references": references,
        "image": image,
    }


@tool(
    effect="model",
    label="Finish preview",
    summary={"accept": "Apply the previewed changes", "discard": "Discard the preview"},
    variants="action",
    keywords=("accept preview", "apply preview", "discard preview", "keep changes"),
    requires=("work_part",),
    cost="heavy",
    refresh_annotations=True,
    example={"preview_id": "preview_" + "0" * 32, "action": "accept"},
)
def nx_finish_preview(
    tx: Tx,
    *,
    preview_id: PreviewId,
    action: Literal["accept", "discard"],
) -> FinishData:
    """Accept re-applies exactly the previewed calls under one undo mark; discard drops them.
    Accept fails with NX_STALE_PREVIEW if the model changed; use the refs it returns."""
    services = tx.services
    store = _store(services)
    preview_id = _canonical(preview_id)
    with tx.checks():
        preview = store.get(preview_id)
        if preview is None:
            return _gone(tx, preview_id, action)
        del store[preview_id]  # finishing consumes the preview, applied or refused
        if action == "discard":
            _finish(services, preview_id, "discarded")
            tx.outcome = "not_applicable"  # nothing in NX changed
            return {"preview_id": preview_id, "action": "discard", "state": "discarded",
                    "results": [], "count": 0}  # fmt: skip
        _fresh(tx, preview_id, preview)
        mapping = _reissue(tx, preview["objects"])
    _finish(services, preview_id, "failed")  # until every step ran
    total = len(preview["operations"])
    tx.checkpoint(done=0, total=total)
    results: list[StepResult] = []
    for index, step in enumerate(preview["operations"]):
        child = tx.run(step["tool"], _translate(step["arguments"], mapping), mode="preview")
        results.append({"index": index, **child})
    tx.checkpoint(done=total)
    _finish(services, preview_id, "accepted")
    return {
        "preview_id": preview_id,
        "action": "accept",
        "state": "applied",
        "results": results,
        "count": total,
    }


def _gone(tx, preview_id: str, action: str) -> FinishData:
    """A preview no longer in the store: discarding an expired or discarded one is a
    no-op; anything else says what happened to it."""
    fate = _FINISHED.get(tx.services, {}).get(preview_id)
    if action == "discard" and fate in ("discarded", "stale", "evicted", "failed"):
        tx.outcome = "not_applicable"
        return {"preview_id": preview_id, "action": "discard", "state": "already_finished",
                "results": [], "count": 0}  # fmt: skip
    if fate == "accepted":
        fail("NX_STALE_PREVIEW", f"{preview_id} {_FATE[fate]}", field="/preview_id",
             hint="Undo it with nx_undo_operation; use the refs from the accept result",
             details={"preview_id": preview_id, "finished": fate})  # fmt: skip
    if fate is not None:
        fail("NX_STALE_PREVIEW", f"{preview_id} {_FATE[fate]}", field="/preview_id",
             hint="Create a fresh preview with nx_preview_changes",
             details={"preview_id": preview_id, "finished": fate})  # fmt: skip
    fail("NX_STALE_PREVIEW",
         f"Unknown preview {preview_id}: not made in this NX session (a host reload "
         "forgets previews)", field="/preview_id",
         hint="Create a fresh preview with nx_preview_changes and use its preview_id")  # fmt: skip


def _fresh(tx, preview_id: str, preview: dict) -> None:
    """Refuse the accept unless the model is as previewed (see the module docstring)."""
    services = tx.services
    if services.part_key(tx.part) != preview["part"]:
        _finish(services, preview_id, "stale")
        fail("NX_STALE_PREVIEW", "The work part changed since the preview",
             field="/preview_id", details={"preview_id": preview_id, "changed": ["work_part"]},
             hint="Make the previewed part the work part and preview again")  # fmt: skip
    if services.revision() == preview["revision"]:
        return
    changed = _changed(preview["fingerprint"], _fingerprint(tx, _state(tx), _expressions(tx)))
    if changed:
        _finish(services, preview_id, "stale")
        fail("NX_STALE_PREVIEW", "The model changed since the preview: " + ", ".join(changed),
             field="/preview_id", hint="Create a fresh preview with nx_preview_changes",
             details={"preview_id": preview_id, "changed": changed})  # fmt: skip
    tx.warn(
        "NX_UPDATE_WARNING",
        "Other calls ran since the preview, but the work part's objects, expressions, "
        "volume and bounds are as previewed: the previewed steps were applied",
    )
