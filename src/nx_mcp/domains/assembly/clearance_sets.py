"""Persistent clearance analysis sets of an assembly (Assemblies > Clearance Analysis): a named
set of components or bodies, a clearance zone and excluded pairs, stored in the assembly
and re-run after the design moves. ``nx_check_interference`` is the one-off check.

NX 2406 findings (live):

- ``AssemblyManager.CreateClearanceAnalysisBuilder(None)`` creates a set,
  ``(set)`` edits one; a builder on a set reads its settings back. The set's name is its
  ``JournalIdentifier`` (``ClearanceSet.Name`` stays empty) and
  ``ComponentAssembly.ClearanceSets`` lists the sets; NX refuses a duplicate name.
- ``CreateClearanceZoneExpression(rhs)`` makes the zone expression (``p<N>_CZ``, part
  units). Pairs closer than the zone are soft interferences; zone 0 reports none.
- ``PerformAnalysis`` stores the results: ``GetResults`` (counts, version = run number),
  ``GetNextInterference`` cycles the pairs, ``GetInterferenceData`` gives the type, a
  point on each object, whether the pair is new in this run and the penetration depth.
- NX's penetration depth is an estimate along a direction NX picks: two 100 mm cubes
  overlapping by 10 mm give 12.2 mm along (-0.9998, 0.0222, 0). The overlap solid
  (``CreateInterferenceGeometry``) is exact: it is measured under a scratch mark and
  removed again.
- Exceptions (``AddException(True, a, b, note)``) exclude a pair from every run.
"""

from __future__ import annotations

import math
import typing
from typing import Annotated, Literal, TypedDict, cast

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, Name, RefTo, Text
from nx_mcp.domains.assembly import common, structure
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, expr, geom, licenses, measure, readback, result, select
from nx_mcp.kit import parts as kparts

MAX_OBJECTS = 500
MAX_EXCLUDED = 100
MAX_LISTED = 200  # interference pairs per result
MAX_OVERLAPS = 25  # hard pairs whose overlap solid is measured
_NAME_TEXT = next(m for m in typing.get_args(Name)[1:] if isinstance(m, Text))
SetName = Annotated[str, Doc("Clearance set name (any case); nx_list_clearance_sets"), _NAME_TEXT]
ObjectRef = Annotated[str, RefTo("component", "body", doc="Component 'o:N' or body ref")]
Objects = Annotated[list[ObjectRef], Items(1, MAX_OBJECTS)]
Method = Literal["exact", "exact_if_loaded", "lightweight"]
BETWEEN = {"components": "Components", "bodies": "Bodies"}
METHODS = {"exact": "Exact", "exact_if_loaded": "ExactifLoaded", "lightweight": "Lightweight"}
TYPES = {"Soft": "soft", "Touch": "touching", "Hard": "hard", "Cont1In2": "containment",
         "Cont2In1": "containment", "NoInterference": "none"}  # fmt: skip
SEVERITY = {"hard": 0, "containment": 1, "touching": 2, "soft": 3}
OVERLAP_DOC = "Measure hard pairs' exact overlap (volume, box)"


class ObjectRow(result.Row, total=False):
    component: str  # a body's component path


class RunSummary(TypedDict):
    version: int  # run number of the set
    pairs: int
    checked_pairs: int
    excluded_pairs: int
    changed_pairs: int
    interferences: int
    hard: int
    soft: int
    touching: int
    containment: int
    new: int


class SetRow(TypedDict):
    name: str
    between: str
    objects: int | None  # collection one size, None = every object of the assembly
    against: int | None  # collection two size, None = one collection
    clearance: float
    method: str
    last_run: RunSummary | None


class Interference(TypedDict, total=False):
    objects: list[ObjectRow]
    type: str  # hard, soft, touching or containment
    new: bool  # first found in this run
    points: list[list[float]]
    distance: float  # soft: gap between the points
    depth: float  # hard: NX's penetration depth estimate along direction
    direction: list[float]
    contained: str  # containment: first_in_second or second_in_first
    overlap_volume: float
    overlap_box: dict[str, list[float]]
    note: str


class ClearanceData(TypedDict):
    clearance_set: SetRow
    summary: RunSummary | None
    interferences: list[Interference]
    truncated: bool


class ClearanceSetList(TypedDict):
    items: list[SetRow]


class DeletedSet(TypedDict):
    deleted: str
    remaining: list[str]


# ---------------------------------------------------------------------------
# Shared helpers


def _sets(tx) -> list:
    return sorted(tx.part.ComponentAssembly.ClearanceSets, key=lambda s: _name(s).casefold())


def _name(cs) -> str:
    return str(getattr(cs, "JournalIdentifier", "") or getattr(cs, "Name", ""))


def _find(tx, name, field="/name"):
    for cs in _sets(tx):
        if _name(cs).casefold() == str(name).strip().casefold():
            return cs
    fail("NX_NOT_FOUND", f"The assembly has no clearance set {name!r}", field=field,
         details={"available": [_name(s) for s in _sets(tx)][:50]},
         hint="nx_list_clearance_sets lists them")  # fmt: skip


def _license(tx):
    licenses.require(tx, ("assemblies",), "Clearance analysis needs an assemblies license")


def _builder_enum(tx):
    return tx.nx.Assemblies.ClearanceAnalysisBuilder


def _summary(tx, cs) -> RunSummary | None:
    """The counts of the set's last run, None before its first."""
    try:
        r = cs.GetResults()
    except Exception:  # NX: "never run" is an error
        return None
    return {
        "version": int(r.Version),
        "pairs": int(r.NumPairs),
        "checked_pairs": int(r.NumCheckedPairs),
        "excluded_pairs": int(r.NumExcludedPairs),
        "changed_pairs": int(r.NumChangedPairs),
        "interferences": int(r.NumAllInterf),
        "hard": int(r.NumHard),
        "soft": int(r.NumSoft),
        "touching": int(r.NumTouching),
        "containment": int(r.NumContainment),
        "new": int(r.NumNewAllInterf),
    }


def _set_row(tx, cs) -> SetRow:
    """Name, settings (a builder on the set reads them back) and last-run counts."""
    enum = _builder_enum(tx)
    factory = tx.part.AssemblyManager.CreateClearanceAnalysisBuilder
    with builders.build(tx, factory, cs) as b, native():
        between = result.enum_name(b.ClearanceBetween, enum.ClearanceBetweenEntity,
                                   list(BETWEEN.values()))  # fmt: skip
        two = b.TotalCollectionCount == enum.NumberOfCollections.Two
        selected = b.CollectionOneRange == enum.CollectionRange.SelectedObjects
        first = len(b.CollectionOneObjects.GetArray())
        second = len(b.CollectionTwoObjects.GetArray())
        zone = b.GetDefaultClearanceZone()
        clearance = float(expr.value(tx, zone) or 0.0) if zone is not None else 0.0
        method = result.enum_name(b.CalculationMethod, enum.CalculationMethodType,
                                  list(METHODS.values()))  # fmt: skip
    return {
        "name": _name(cs),
        "between": next((k for k, v in BETWEEN.items() if v == between), between),
        "objects": first if selected else None,
        "against": second if two else None,
        "clearance": clearance,
        "method": next((k for k, v in METHODS.items() if v == method), method),
        "last_run": _summary(tx, cs),
    }


def _object_row(tx, obj, names) -> ObjectRow:
    if kbodies.is_component(obj):
        return cast(ObjectRow, common.component_row(tx, obj, names.get(int(obj.Tag)),
                                                    with_pose=False))  # fmt: skip
    row = cast(ObjectRow, tx.ref(obj, kind="body"))
    owner = getattr(obj, "OwningComponent", None)
    if owner is not None:
        row["component"] = "/".join(names.get(int(owner.Tag)) or [str(owner.Name)])
    return row


def _point(p) -> list[float]:
    return geom.xyz(p)


def _pairs(tx, cs) -> list[tuple]:
    """``(first, second, data)`` of every stored interference (at most MAX_LISTED + 1)."""
    out: list[tuple] = []
    with native():
        total = int(cs.GetNumberOfInterferences())
        first = second = None
        for _ in range(min(total, MAX_LISTED + 1)):
            first, second = cs.GetNextInterference(first, second)
            if first is None:
                break
            out.append((first, second, cs.GetInterferenceData(first, second)))
    return out


def _interference(tx, first, second, data, names) -> Interference:
    sets = tx.nx.Assemblies.ClearanceSet
    kind_name = result.enum_name(data[0], sets.InterferenceType, list(TYPES))
    p1, p2 = _point(data[3]), _point(data[4])
    row: Interference = {
        "objects": [_object_row(tx, first, names), _object_row(tx, second, names)],
        "type": TYPES.get(kind_name, kind_name),
        "new": bool(data[1]),
        "points": [p1, p2],
    }
    if row["type"] == "soft":
        row["distance"] = math.dist(p1, p2)
    if kind_name in ("Cont1In2", "Cont2In1"):
        row["contained"] = "first_in_second" if kind_name == "Cont1In2" else "second_in_first"
    depth_ok = data[8] == sets.PenetrationDepthResult.Success or str(data[8]) == str(
        sets.PenetrationDepthResult.Success
    )
    if row["type"] in ("hard", "containment") and depth_ok:
        row["depth"] = result.measured(data[9])
        row["direction"] = geom.xyz(data[10])
    if data[5]:
        row["note"] = str(data[5])
    return row


def _order(row: Interference) -> tuple:
    """Hard pairs first, then containment, touching, soft; then by object path."""
    names = tuple(str(o.get("path") or o.get("component") or "") + "/" + str(o.get("name"))
                  for o in row["objects"])  # fmt: skip
    return SEVERITY.get(row["type"], len(SEVERITY)), names


def _overlaps(tx, cs, hard) -> dict[int, tuple[float, dict]]:
    """Exact overlap volume and box of hard pairs: NX's interference solids, measured
    under a scratch mark that removes them again. Keyed by the pair's index."""
    if not hard:
        return {}
    out: dict[int, tuple[float, dict]] = {}
    with tx.scratch():
        with native():
            cs.CreateInterferenceGeometry([f for _i, f, _s in hard], [s for _i, _f, s in hard])
        for index, first, second in hard:
            with native():
                solids = [b for b in cs.GetInterferenceData(first, second)[2]
                          if getattr(b, "IsSolidBody", False)]  # fmt: skip
            if solids:
                low, high = measure.bounds(tx, solids)
                out[index] = (measure.volume(tx, solids), {"min": low, "max": high})
    return out


def _results(tx, cs, overlap: bool) -> tuple[RunSummary | None, list[Interference], bool]:
    summary = _summary(tx, cs)
    if summary is None:
        return None, [], False
    names = structure.paths(tx.part)
    found = _pairs(tx, cs)
    truncated = len(found) > MAX_LISTED
    found = found[:MAX_LISTED]
    rows = [_interference(tx, f, s, d, names) for f, s, d in found]
    order = sorted(range(len(rows)), key=lambda i: _order(rows[i]))
    found, rows = [found[i] for i in order], [rows[i] for i in order]
    if overlap:
        hard = [(i, f, s) for i, (f, s, _d) in enumerate(found) if rows[i]["type"] == "hard"]
        if len(hard) > MAX_OVERLAPS:
            tx.warn("NX_NOTE", f"Overlap measured for the first {MAX_OVERLAPS} of {len(hard)} "
                    "hard pairs")  # fmt: skip
        for i, (volume, box) in _overlaps(tx, cs, hard[:MAX_OVERLAPS]).items():
            rows[i]["overlap_volume"] = volume
            rows[i]["overlap_box"] = box
    return summary, rows, truncated


def _run(tx, cs) -> None:
    before = _summary(tx, cs)
    with native(default="NX_BUILD_FAILED"):
        cs.PerformAnalysis(tx.nx.Assemblies.ClearanceSet.ReanalyzeOutOfDateExcludedPairs
                           .CustomerDefault)  # fmt: skip
    after = _summary(tx, cs)
    readback.expect(after is not None and (before is None or after["version"] > before["version"]),
                    "run", "NX stored no new clearance results")  # fmt: skip


def _members(tx, refs, between, field) -> list:
    """Components (or bodies; a component stands for its solid occurrence bodies) of the
    work assembly for one collection."""
    assembly = {int(c.Tag): c for c, _names in kparts.components(tx.part)}
    out = []
    for i, ref in enumerate(refs):
        f = f"{field}/{i}"
        obj = select.resolve(tx, ref, "component", "body", field=f)
        if kbodies.is_component(obj):
            if int(obj.Tag) not in assembly:
                fail("NX_WRONG_OWNER", f"{ref} is not a component of the work part", field=f,
                     hint=common.OWNER_HINT)  # fmt: skip
            if kbodies.suppressed(obj):
                fail("NX_INVALID_ARGUMENT", f"{ref} is suppressed; unsuppress it first", field=f)
            if between == "components":
                out.append(obj)
                continue
            found = [b for b in kbodies.occurrences(obj) if getattr(b, "IsSolidBody", False)]
            if not found:
                fail("NX_WRONG_GEOMETRY", f"{ref} has no solid body (is its part loaded?)",
                     field=f)  # fmt: skip
            out.extend(found)
            continue
        if between == "components":
            fail("NX_WRONG_KIND", f"{ref} is a body; a components set takes components",
                 field=f, hint="Pass its component, or between='bodies'")  # fmt: skip
        owner = getattr(obj, "OwningComponent", None)
        if not (owner is not None and int(owner.Tag) in assembly) and not select.owned_by_work_part(
            tx, obj
        ):
            fail("NX_WRONG_OWNER", f"{ref} is not a body of the work assembly", field=f)
        out.append(obj)
    seen: set[int] = set()
    for obj in out:
        if int(obj.Tag) in seen:
            fail("NX_INVALID_ARGUMENT", f"An object is listed twice in {field}", field=field)
        seen.add(int(obj.Tag))
    return out


def _data(tx, cs, *, ran: bool, overlap: bool) -> ClearanceData:
    summary, rows, truncated = _results(tx, cs, overlap) if ran else (None, [], False)
    return {
        "clearance_set": _set_row(tx, cs),
        "summary": summary,
        "interferences": rows,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Tools


@tool(
    effect="model",
    label="Create clearance set",
    summary="Create clearance set {name}",
    keywords=(
        "clearance analysis",
        "clearance set",
        "clearance zone",
        "persistent interference check",
        "clash set",
        "hard soft interference",
    ),  # fmt: skip
    requires=("work_part", "assembly", "no_active_sketch"),
    cost="long",
    example={"name": "ENGINE_ROOM", "clearance": 5, "exclude": [["o:4", "o:7"]]},
)
def nx_create_clearance_set(
    tx: Tx,
    *,
    name: Annotated[str, Doc("Set name, unique in the assembly (any case)"), _NAME_TEXT],
    between: Annotated[
        Literal["components", "bodies"], Doc("bodies: a component stands for its solids")
    ] = "components",
    objects: Annotated[Objects, Doc("Collection one; omitted: every object")] | None = None,
    against: Annotated[Objects, Doc("Check objects only against these")] | None = None,
    clearance: Annotated[Length, Doc("Closer pairs are soft interferences; 0: none")] = 0,
    exclude: Annotated[
        list[tuple[ObjectRef, ObjectRef]], Items(1, MAX_EXCLUDED), Doc("Pairs never checked")
    ]
    | None = None,
    method: Method = "exact",
    ignore_same_part: Annotated[bool, Doc("Skip pairs of two uses of one part")] = False,
    run: Annotated[bool, Doc("Run it now")] = True,
    overlap: Annotated[bool, Doc(OVERLAP_DOC)] = True,
) -> ClearanceData:
    """Create a named clearance analysis set stored in the work assembly, and run it. Reports
    hard (overlap; NX depth estimate), soft (within clearance), touching and containment
    pairs. Re-run after moves with nx_run_clearance_set."""
    zone = float(clearance)  # the kernel converted the quantity to part units
    if zone < 0:
        fail("NX_INVALID_ARGUMENT", "clearance must not be negative", field="/clearance")
    if against is not None and objects is None:
        fail("NX_INVALID_ARGUMENT", "against needs objects (collection one)", field="/objects")
    with tx.checks():
        if any(_name(s).casefold() == name.strip().casefold() for s in _sets(tx)):
            fail("NX_ALREADY_EXISTS", f"A clearance set {name!r} already exists", field="/name",
                 hint="Re-run it with nx_run_clearance_set or pick another name")  # fmt: skip
        first = _members(tx, objects, between, "/objects") if objects is not None else None
        second = _members(tx, against, between, "/against") if against is not None else None
        if first is not None and second is None and len(first) < 2:
            fail("NX_INVALID_ARGUMENT", "One collection needs two objects or more",
                 field="/objects", hint="Omit objects to check every object")  # fmt: skip
        if first is not None and second is not None:
            shared = {int(o.Tag) for o in first} & {int(o.Tag) for o in second}
            if shared:
                fail("NX_INVALID_ARGUMENT", "An object is in both objects and against",
                     field="/against")  # fmt: skip
        pairs = []
        for i, (a, b) in enumerate(exclude or []):
            pair = _members(tx, [a, b], between, f"/exclude/{i}")
            if len(pair) != 2:
                fail("NX_INVALID_ARGUMENT", "An excluded pair names two bodies (or components)",
                     field=f"/exclude/{i}")  # fmt: skip
            pairs.append(pair)
        _license(tx)
    enum = _builder_enum(tx)
    part = tx.part
    with builders.build(tx, part.AssemblyManager.CreateClearanceAnalysisBuilder, None) as b:
        with native(default="NX_BUILD_FAILED"):
            b.ClearanceSetName = name.strip()
            b.ClearanceBetween = getattr(enum.ClearanceBetweenEntity, BETWEEN[between])
            two = second is not None
            b.TotalCollectionCount = (
                enum.NumberOfCollections.Two if two else enum.NumberOfCollections.One
            )
            if first is None:
                b.CollectionOneRange = enum.CollectionRange.AllObjects
            else:
                b.CollectionOneRange = enum.CollectionRange.SelectedObjects
                b.CollectionOneObjects.Add(first)
            if second is not None:
                b.CollectionTwoRange = enum.CollectionRange.SelectedObjects
                b.CollectionTwoObjects.Add(second)
            b.CalculationMethod = getattr(enum.CalculationMethodType, METHODS[method])
            b.IsCalculatePenetrationDepth = True
            b.IsIgnorePairsWithinSamePart = bool(ignore_same_part)
            b.SetDefaultClearanceZone(b.CreateClearanceZoneExpression(expr.rhs(zone)))
            for a, c in pairs:
                b.AddException(True, a, c, "")
        b.commit()
    cs = next((s for s in _sets(tx) if _name(s).casefold() == name.strip().casefold()), None)
    readback.expect(cs is not None, "clearance_set", "NX created no clearance set")
    if run:
        _run(tx, cs)
    data = _data(tx, cs, ran=run, overlap=overlap)
    row = data["clearance_set"]
    readback.expect(row["between"] == between, "between", actual=row["between"])
    readback.expect_close(row["clearance"], zone, "clearance")
    if first is not None:
        readback.expect(row["objects"] == len(first), "objects", actual=row["objects"])
    return data


@tool(
    effect="model",
    label="Run clearance set",
    summary="Run clearance set {name}",
    keywords=(
        "rerun clearance analysis",
        "run clearance set",
        "update clearance results",
        "clearance after move",
        "clash rerun",
    ),  # fmt: skip
    requires=("work_part", "assembly", "no_active_sketch"),
    cost="long",
    example={"name": "ENGINE_ROOM"},
)
def nx_run_clearance_set(
    tx: Tx,
    *,
    name: SetName,
    overlap: Annotated[bool, Doc(OVERLAP_DOC)] = True,
) -> ClearanceData:
    """Run a stored clearance set again and return its interferences, e.g. after a move. new
    marks pairs first found in this run; summary.version counts the runs."""
    with tx.checks():
        cs = _find(tx, name)
        _license(tx)
    _run(tx, cs)
    return _data(tx, cs, ran=True, overlap=overlap)


@tool(
    effect="read",
    label="List clearance sets",
    summary="Clearance sets of the assembly",
    keywords=(
        "clearance sets",
        "list clearance analysis",
        "stored clash sets",
        "clearance results",
    ),
    requires=("work_part", "assembly"),
    cost="light",
    example={},
)
def nx_list_clearance_sets(tx: Tx) -> ClearanceSetList:
    """List the work assembly's clearance sets: settings and the counts of their last run
    (null before the first). nx_run_clearance_set returns the pairs."""
    _license(tx)
    return {"items": [_set_row(tx, cs) for cs in _sets(tx)]}


@tool(
    effect="model",
    label="Delete clearance set",
    summary="Delete clearance set {name}",
    keywords=("delete clearance set", "remove clearance analysis", "drop clash set"),
    requires=("work_part", "assembly", "no_active_sketch"),
    cost="light",
    invalidates="deleted",
    example={"name": "ENGINE_ROOM"},
)
def nx_delete_clearance_set(tx: Tx, *, name: SetName) -> DeletedSet:
    """Delete a clearance set and its stored results from the work assembly."""
    with tx.checks():
        cs = _find(tx, name)
        _license(tx)
    deleted = _name(cs)
    with native(default="NX_BUILD_FAILED"):
        cs.Delete()
    remaining = [_name(s) for s in _sets(tx)]
    readback.expect(deleted.casefold() not in {n.casefold() for n in remaining}, "deleted",
                    "NX kept the clearance set")  # fmt: skip
    return {"deleted": deleted, "remaining": remaining}
