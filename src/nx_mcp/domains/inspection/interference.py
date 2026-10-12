"""Interference and clearance between bodies, and the inspection report ZIP
(tool-surface 3.9).

Pairs are distinct body pairs (component occurrences included, nested ones too). A
bounding-box broad phase skips pairs whose boxes are farther apart than the clearance
without native work, so ``max_pairs`` limits only the native checks; the result reports
how many pairs it skipped that way (``skipped_pairs``, ``counts.skipped`` and
``skipped_reason``). ``counts`` classifies the natively checked pairs only: a skipped
pair is never counted as ``clear``. A pair at zero distance runs NX's
solid interference, whose temporary interference bodies give the overlap volume and
are always rolled back (``tx.scratch``). NX 2406 findings kept here:

- If that rollback fails the call fails as ``NX_ROLLBACK_FAILED`` (the model may
  hold leftover bodies) and the check's own error stays in ``details.original_error``.
- ``NX_TOO_MANY_OBJECTS`` is raised before any native
  pair check; ``details`` gives the pair counts.
"""

from __future__ import annotations

import contextlib
import html
import itertools
import json
import math
import zipfile
from pathlib import Path
from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, classify, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, PathTo, PlaneByNormal, Range, RefTo
from nx_mcp.domains.inspection import common, health
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import capture as kcapture
from nx_mcp.kit import files, geom, measure, result

DEFAULT_PAIRS = 100
MAX_PAIRS = 10000
CONTACT = 1e-7  # part units: at or below this minimum distance a pair touches
MAX_CLOSEUPS = 8
FLAGGED = ("penetration", "contact", "below_clearance")
SKIPPED_REASON = (
    "not checked: their bounding boxes are farther apart than the clearance ({clearance:g}), "
    "so they cannot touch; include_clear=true checks and lists every pair"
)
Objects = Annotated[list[Annotated[str, RefTo("body", "feature", "component")]], Items(2, 500)]
MaxPairs = Annotated[int, Range(ge=1, le=MAX_PAIRS), Doc("Native pair checks allowed")]
Clearance = Annotated[Length, Doc("Minimum clearance, part units; 0 = interference only")]


class Pair(TypedDict, total=False):
    classification: str
    objects: list[result.Row]
    distance: float
    points: list[list[float]]
    overlap_volume: float
    overlap_box: dict[str, list[float]]


class _InterferenceData(TypedDict):
    pairs: list[Pair]
    # pairs per classification, natively checked ones only; skipped: not checked
    counts: dict[str, int]
    bodies: int
    pair_count: int
    checked_pairs: int
    skipped_pairs: int
    clearance: float
    skipped_sheet_bodies: list[result.Row]


class InterferenceData(_InterferenceData, total=False):
    skipped_reason: str  # why skipped pairs were not checked (with skipped_pairs > 0)


def _envelope_gap(a, b):
    return math.sqrt(sum(max(a[i] - b[i + 3], b[i] - a[i + 3], 0.0) ** 2 for i in range(3)))


@contextlib.contextmanager
def temporary(tx):
    """``tx.scratch()`` that keeps the body's own error when the rollback also fails."""
    original = None
    try:
        with tx.scratch():
            try:
                yield
            except BaseException as error:
                original = error
                raise
    except NXError as error:
        if original is None or error is original or error.code != "NX_ROLLBACK_FAILED":
            raise
        own = classify(original)
        error.details = {
            **(error.details or {}),
            "original_error": {"code": own.code, "message": own.message, "nx_code": own.nx_code},
        }
        raise error from original


def _overlap(tx, a, b):
    """``(classification, overlap volume, overlap box)`` of a touching pair (solid
    interference); the box bounds the interference bodies of a penetration."""
    part = tx.part
    outcome = tx.nx.GeometricAnalysis.SimpleInterference.Result
    with temporary(tx):
        with native():
            check = part.AnalysisManager.CreateSimpleInterferenceObject()
        try:
            with native():
                check.InterferenceType = (
                    tx.nx.GeometricAnalysis.SimpleInterference.InterferenceMethod.InterferenceSolid
                )
                check.FirstBody.Value = a
                check.SecondBody.Value = b
                found = check.PerformCheck()
            if found == outcome.InterferenceExists:
                # NX 2406: GetInterferenceResults raises "Cannot finish operation" (820022)
                # unless an interference exists.
                with native():
                    solids = [r for r in check.GetInterferenceResults() if r.IsSolidBody]
                if not solids:
                    return "penetration", 0.0, None
                low, high = measure.bounds(tx, solids)
                return "penetration", measure.volume(tx, solids), {"min": low, "max": high}
            if found == outcome.OnlyEdgesOrFacesInterfere:
                return "contact", 0.0, None
            if found == outcome.NoInterference:
                return "clear", 0.0, None
            fail("NX_INCOMPLETE_RESULT", "NX could not classify this pair")
        finally:
            with contextlib.suppress(Exception):
                check.Reset()
            with contextlib.suppress(Exception):
                check.Destroy()


def analyze(tx, found, pairs, *, clearance, max_pairs, include_clear, include_contact, sheets=()):
    """Classify body pairs: penetration, contact, below_clearance or clear."""
    with native():
        boxes = {
            int(b.Tag): [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(b.Tag)] for b in found
        }
    checked = [
        (a, b)
        for a, b in pairs
        if include_clear
        or _envelope_gap(boxes[int(a.Tag)], boxes[int(b.Tag)]) <= clearance + CONTACT
    ]
    skipped = len(pairs) - len(checked)
    if len(checked) > max_pairs:
        fail(
            "NX_TOO_MANY_OBJECTS",
            f"{len(checked)} of {len(pairs)} body pairs need a native check, more than "
            f"max_pairs={max_pairs}; no pair was checked",
            field="/max_pairs",
            hint=f"Select smaller groups or raise max_pairs (at most {MAX_PAIRS})",
            details={"pairs": len(pairs), "native_pairs": len(checked), "max_pairs": max_pairs},
        )
    # Skipped pairs are not counted as clear: only a native check classifies a pair.
    counts = {"penetration": 0, "contact": 0, "below_clearance": 0, "clear": 0, "skipped": skipped}
    reports = []
    for index, (a, b) in enumerate(checked):
        tx.checkpoint(index, len(checked))
        with native():
            distance, p1, p2, _accuracy = tx.session.Measurement.GetMinimumDistance(a, b)
        distance = result.measured(distance)
        kind, volume, box = _overlap(tx, a, b) if distance <= CONTACT else ("clear", 0.0, None)
        if kind == "clear" and distance < clearance:
            kind = "below_clearance"
        counts[kind] += 1
        if kind == "clear" and not include_clear or kind == "contact" and not include_contact:
            continue
        pair = {
            "classification": kind,
            "objects": [tx.ref(x, kind="body") for x in (a, b)],
            "distance": distance,
            "points": [geom.xyz(p1), geom.xyz(p2)],
        }
        if kind == "penetration":
            pair["overlap_volume"] = volume
            if box is not None:
                pair["overlap_box"] = box
        reports.append(pair)
    out: InterferenceData = {
        "pairs": reports,
        "counts": counts,
        "bodies": len(found),
        "pair_count": len(pairs),
        "checked_pairs": len(checked),
        "skipped_pairs": skipped,
        "clearance": clearance,
        "skipped_sheet_bodies": list(sheets),
    }
    if skipped:
        out["skipped_reason"] = SKIPPED_REASON.format(clearance=clearance)
    return out


def _is_sheet(body) -> bool:
    return not getattr(body, "IsSolidBody", False)


def _groups(tx, objects):
    """``(bodies, pairs, sheets)``: body groups per reference, or every solid body of the
    work part and its loaded components (sheet bodies skipped and returned as rows)."""
    sheets = []  # rows of the sheet bodies a whole-model check skips
    if objects is None:
        everything = kbodies.require(kbodies.scope(tx, "assembly"))
        sheets = [tx.ref(b, kind="body") for b in everything if _is_sheet(b)]
        found = [b for b in everything if not _is_sheet(b)]
        if not found:
            fail(
                "NX_WRONG_GEOMETRY",
                f"Interference needs solid bodies; the model has only {len(sheets)} sheet "
                "body(ies)",
                field="/objects",
                details={"sheet_bodies": sheets[:20]},
            )
        groups = [[b] for b in found]
    else:
        groups = []
        for i, ref in enumerate(objects):
            obj = tx.resolve(ref, *common.TARGET_KINDS)
            group = kbodies.require(kbodies.of(tx, obj), field=f"/objects/{i}")
            bad = [tx.ref(b, kind="body") for b in group if _is_sheet(b)]
            if bad:
                fail(
                    "NX_WRONG_GEOMETRY",
                    f"{ref} gives sheet body {bad[0]['id']}; interference needs solid bodies",
                    field=f"/objects/{i}",
                    hint="Pass solid bodies only, or omit objects: the whole-model check "
                    "skips sheet bodies",
                    details={"sheet_bodies": bad[:20]},
                )
            groups.append(group)
        found = list({int(b.Tag): b for g in groups for b in g}.values())
    pairs, seen = [], set()
    for first, second in itertools.combinations(groups, 2):
        for a in first:
            for b in second:
                key = tuple(sorted((int(a.Tag), int(b.Tag))))
                if key[0] != key[1] and key not in seen:
                    seen.add(key)
                    pairs.append((a, b))
    if not pairs:
        fail(
            "NX_INVALID_ARGUMENT",
            "The objects resolve to one solid body only; interference needs two",
            field="/objects",
        )
    return found, pairs, sheets


@tool(
    target="assembly",
    effect="read",
    label="Check interference",
    summary=lambda a, say: (
        "Interference of " + common.count_text(a, "objects", "object")
        if a.get("objects")
        else "Interference in the assembly"
    ),
    keywords=("clash", "collision", "clearance check", "interference", "overlap", "collisions"),
    requires=("work_part",),
    cost="long",
    example={"objects": ["o:4", "o:5"], "clearance": 1},
)
def nx_check_interference(
    tx: Tx,
    *,
    objects: Objects | None = None,
    clearance: Clearance = 0,
    max_pairs: MaxPairs = DEFAULT_PAIRS,
    include_clear: bool = False,
    include_contact: bool = True,
) -> InterferenceData:
    """Check interference and clearance between the solid bodies of objects. No objects: every
    solid of the work part and loaded components (sheets skipped). Pair points are closest
    points, equal when bodies touch or overlap (see overlap_box)."""
    common.note_exploded(tx)
    clearance = float(clearance)  # the kernel converted the quantity to part units
    if clearance < 0:
        fail("NX_INVALID_ARGUMENT", "clearance must not be negative", field="/clearance")
    found, pairs, sheets = _groups(tx, objects)
    return analyze(
        tx,
        found,
        pairs,
        clearance=clearance,
        max_pairs=max_pairs,
        include_clear=include_clear,
        include_contact=include_contact,
        sheets=sheets,
    )


# ---------------------------------------------------------------------------
# Inspection report


class ReportData(TypedDict):
    path: str
    size: int
    sha256: str
    files: list[str]
    captures: int
    counts: dict[str, int]


WIDTH, HEIGHT = 1280, 800


def _view(tx):
    part = tx.session.Parts.Display
    if part is None:
        fail("NX_NO_WORK_PART", "No display part to capture")
    return part, part.ModelingViews.WorkView


def _capture(tx, folder, name, fit=True):
    _part, view = _view(tx)
    if fit:
        with native():
            view.Fit()
    target = folder / name
    kcapture.studio_png(tx, target, WIDTH, HEIGHT, style="shaded_with_edges")
    return target


def _closeup(tx, folder, name, pair_bodies, everything):
    """Isolate and highlight one pair, capture it; blanking is undone by the scratch."""
    keep = {int(b.Tag) for b in pair_bodies}
    with temporary(tx):
        with native():
            for body in everything:
                if int(body.Tag) not in keep:
                    body.Blank()
        try:
            with native():
                for body in pair_bodies:
                    body.Highlight()
            return _capture(tx, folder, name)
        finally:
            for body in pair_bodies:
                with contextlib.suppress(Exception):
                    body.Unhighlight()


def _section(tx, folder, name, plane):
    part, view = _view(tx)
    types = tx.nx.Display.DynamicSectionTypes
    origin = [float(v) for v in plane["origin"]]
    normal = geom.unit(plane["normal"], "/section_planes")
    with temporary(tx):
        with native():
            builder = part.DynamicSections.CreateSectionBuilder(view)
        try:
            with native():
                builder.Type = types.Type.OnePlane
                builder.CsysType = types.CoordinateSystem.Absolute
                builder.SetNormal(tx.nx.Vector3d(*normal))
                builder.SetOrigin(tx.nx.Point3d(*origin))
                builder.ClipType = types.Clip.Section
                builder.ShowClip = True
                builder.ShowCap = True
                builder.ShowViewer = False
                builder.ShowGrid = False
                section = builder.Commit()
                view.ActiveDynamicSection = section
                view.DisplaySectioningToggle = True
        finally:
            with contextlib.suppress(Exception):
                builder.Destroy()
        try:
            return _capture(tx, folder, name)
        finally:
            with contextlib.suppress(Exception):
                view.DisplaySectioningToggle = False


def _captures(tx, folder, checks, planes, everything, by_id):
    images = [_capture(tx, folder, "overview.png")]
    flagged = [p for p in checks["pairs"] if p["classification"] in FLAGGED][:MAX_CLOSEUPS]
    for i, pair in enumerate(flagged, 1):
        tx.checkpoint()
        pair_bodies = [by_id[row["id"]] for row in pair["objects"]]
        images.append(_closeup(tx, folder, f"pair-{i}.png", pair_bodies, everything))
    for i, plane in enumerate(planes, 1):
        tx.checkpoint()
        images.append(_section(tx, folder, f"section-{i}.png", plane))
    return images


def _write_zip(tx, path, folder, report, images):
    (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    figures = "".join(
        f'<figure><img style="max-width:100%" src="{html.escape(p.name, quote=True)}">'
        f"<figcaption>{html.escape(p.name)}</figcaption></figure>"
        for p in images
    )
    page = (
        '<!doctype html><meta charset="utf-8"><title>NX inspection</title>'
        f"<h1>NX inspection report</h1><p>Units: {html.escape(report['units'])}</p>"
        f"<pre>{html.escape(json.dumps(report['interference'], indent=2))}</pre>{figures}"
    )
    (folder / "index.html").write_text(page, encoding="utf-8")
    members = [folder / "report.json", folder / "index.html", *images]
    manifest = {p.name: files.sha256(p) for p in members}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    members.append(folder / "manifest.json")
    staged = tx.files.stage(path)
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.write(member, member.name)
    return [p.name for p in members]


@tool(
    target="assembly",
    switch="display",
    effect="file",
    label="Export inspection report (ZIP)",
    summary="Inspection report {path}",
    keywords=("inspection report", "clearance report", "clash report", "zip report", "review"),
    requires=("work_part",),
    cost="long",
    example={"path": "reports/check.zip", "clearance": 1},
)
def nx_export_inspection_report(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("new", ".zip")],
    objects: Objects | None = None,
    clearance: Clearance = 0,
    max_pairs: MaxPairs = DEFAULT_PAIRS,
    section_planes: Annotated[list[PlaneByNormal], Items(1, 6)] | None = None,
    capture: bool = True,
) -> ReportData:
    """Write a .zip of interference and clearance results (index.html, report.json, PNG
    captures, SHA-256 manifest). No objects: every solid, sheets skipped."""
    planes = list(section_planes or [])
    if planes and not capture:
        fail("NX_INVALID_ARGUMENT", "section_planes need capture=true", field="/section_planes")
    for i, plane in enumerate(planes):
        geom.unit(plane["normal"], f"/section_planes/{i}/normal")
    clearance = float(clearance)  # the kernel converted the quantity to part units
    if clearance < 0:
        fail("NX_INVALID_ARGUMENT", "clearance must not be negative", field="/clearance")
    with tx.checks():  # lookups, refusals and measurements: no file yet (not_started)
        found, pairs, sheets = _groups(tx, objects)
        checks = analyze(
            tx,
            found,
            pairs,
            clearance=clearance,
            max_pairs=max_pairs,
            include_clear=False,
            include_contact=True,
            sheets=sheets,
        )
    units = tx.units.get("length") or "mm"
    report: dict[str, Any] = {
        "format": "nx-mcp-inspection",
        "version": 2,
        "units": units,
        "summary": health.overview(tx),
        "interference": checks,
    }
    folder = files.temp_path(tx.workspace.root, "report", tx.operation_id).with_suffix("")
    folder.mkdir(parents=True, exist_ok=True)
    images: list[Path] = []
    if capture:
        by_id = {tx.ref(b, kind="body")["id"]: b for b in found}
        everything = kbodies.scope(tx, "assembly")
        tx.display.keep_camera()
        tx.display.keep_highlights()
        tx.display.keep(everything)
        try:
            images = _captures(tx, folder, checks, planes, everything, by_id)
        finally:
            tx.display.restore()
    report["captures"] = [p.name for p in images]
    names = _write_zip(tx, path, folder, report, images)
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    return {
        **meta,
        "files": names,
        "captures": len(images),
        "counts": checks["counts"],
    }
