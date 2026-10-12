"""Weight and centre-of-gravity reports for trim and stability (Advanced Assemblies weight
control): ``nx_measure_assembly_mass`` and its CSV export ``nx_export_mass_report``.

Rows: one per component occurrence with its own solid bodies (or an assumed or asserted
mass), one per solid body of the work part itself, one per assumed item (``items``
without a component). A component without bodies and without a mass is reported with
mass 0 and a warning: bought-in engines, tanks and batteries are often placeholders.

A row's mass, by precedence: an ``items`` override of the component, an NX assertion
of the component, then of its part (``nx_set_component_mass``; read with UF
``Weight.AskProps``, never computed with ``Estab*``, which caches into the part), then
the measured solids (``kit.measure.inertia``: material densities, NX's default density
for bodies without a material). An override or assertion of a subassembly covers its
children. Overrides and assertions without a measured body are point masses; with
bodies and no centre given they keep the measured centre and scale its inertia.

Suppressed components are left out (``skipped``) unless ``include_suppressed``: then
their part's solids are measured in the part frame and placed with the occurrence pose
(NX 2406 measures prototype bodies in their own part's coordinates). The report reads
only: totals, groups, the reference frame and what-if changes never change the model.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import ntpath
import re
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    REF_PATTERN,
    Doc,
    Items,
    Json,
    PathTo,
    Point3,
    RefTo,
    Text,
    Vec3,
    forbid_extra,
)
from nx_mcp.domains.inspection import common
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import files, geom, measure, result, select
from nx_mcp.kit import massprops as mp
from nx_mcp.kit import parts as kparts
from nx_mcp.kit import units as kunits

GroupBy = Literal["top_component", "part", "attribute", "component_group", "named"]
ComponentId = Annotated[str, RefTo("component", doc="Component id 'o:N'")]
Label = Annotated[str, Text(64)]
OTHER = "(other)"
ASSUMED = "(assumed items)"
WORK_PART = "(work part)"
NOISE = 1e-12
MAX_SKIPPED = 200
MAX_ROWS = 500  # rows returned; totals, groups and the CSV use them all


class _ItemRequired(TypedDict):
    mass: float  # kg, checked >= 0 in _check_items


class MassItem(_ItemRequired, total=False):
    name: Label
    component: ComponentId
    center: Point3
    frame: Literal["assembly", "component"]
    group: Label


class WeightAdd(TypedDict):
    name: Label
    mass: float  # kg; negative removes weight
    center: Point3


class _NameRequired(TypedDict):
    name: Label


class WeightChange(_NameRequired, total=False):
    component: ComponentId
    item: Label
    offset: Vec3
    move_to: Point3
    remove: bool


class _OriginRequired(TypedDict):
    origin: Point3


class MassReference(_OriginRequired, total=False):
    x_axis: Vec3
    y_axis: Vec3


forbid_extra(MassItem, WeightAdd, WeightChange, MassReference)

# Arguments of the report (docs keep the listed schema short; the module doc has details).
Components = Annotated[list[ComponentId], Items(1, 500), Doc("Only these")]
Excluded = Annotated[list[ComponentId], Items(1, 500), Doc("Not these")]
ItemList = Annotated[
    list[MassItem], Items(1, 200), Doc("Assumed kg: {name, center} or {component}")
]
GroupMap = Annotated[
    dict, Json(max_keys=50, max_chars=8000), Doc("{group: [refs or name patterns]}")
]
Changes = Annotated[
    list[WeightAdd | WeightChange], Items(1, 50),
    Doc("Add {name, mass, center}; move/remove a component or item"),
]  # fmt: skip
Reference = Annotated[MassReference, Doc("LCG/TCG/VCG frame")]
Attribute = Annotated[str, Text(64), Doc("Title (group_by attribute)")]


class MassRow(TypedDict, total=False):
    name: str
    component: result.Row
    body: result.Row
    path: str
    part: str
    group: str
    source: str
    material: str | None
    density: float | None
    volume: float | None
    mass: float
    center_of_gravity: list[float] | None
    reference_cog: list[float]
    percent: float
    bodies: int
    suppressed: bool


class SkippedRow(TypedDict):
    component: result.Row
    reason: str


class GroupRow(TypedDict, total=False):
    name: str
    mass: float
    center_of_gravity: list[float] | None
    reference_cog: list[float]
    percent: float
    rows: int


class Totals(TypedDict, total=False):
    mass: float
    center_of_gravity: list[float] | None
    reference_cog: list[float]
    inertia: list[list[float]]
    principal_moments: list[float]
    reference_inertia: list[list[float]]


class Change(TypedDict, total=False):
    name: str
    mass: float
    rows: int
    shift: list[float]


class WhatIfData(TypedDict, total=False):
    mass: float
    center_of_gravity: list[float] | None
    reference_cog: list[float]
    mass_change: float
    shift: list[float] | None
    reference_shift: list[float]
    changes: list[Change]


class MassReportData(TypedDict, total=False):
    units: dict[str, str]
    total: Totals
    rows: list[MassRow]
    row_count: int
    truncated: bool
    groups: list[GroupRow]
    what_if: WhatIfData
    skipped: list[SkippedRow]


# ---------------------------------------------------------------------------
# Argument checks (no NX)


def _check_items(items) -> None:
    names, overridden = set(), set()
    for i, item in enumerate(items):
        field = f"/items/{i}"
        if not 0 <= float(item["mass"]) <= 1e7:
            fail("NX_INVALID_ARGUMENT", "mass is 0-1e7 kg", field=field + "/mass")
        component = item.get("component")
        if component is None:
            if not item.get("name"):
                fail("NX_INVALID_ARGUMENT", "An added item needs a name", field=field + "/name")
            if item.get("center") is None:
                fail("NX_INVALID_ARGUMENT", "An added item needs its center (CoG)",
                     field=field + "/center")  # fmt: skip
            if item.get("frame") == "component":
                fail("NX_INVALID_ARGUMENT", "frame component needs component",
                     field=field + "/frame")  # fmt: skip
        else:
            if component in overridden:
                fail("NX_INVALID_ARGUMENT", f"{component} is overridden twice",
                     field=field + "/component")  # fmt: skip
            overridden.add(component)
            if item.get("frame") == "component" and item.get("center") is None:
                fail("NX_INVALID_ARGUMENT", "frame applies to center; give center",
                     field=field + "/frame")  # fmt: skip
        name = item.get("name")
        if name:
            key = name.casefold()
            if key in names:
                fail("NX_INVALID_ARGUMENT", f"Item name {name!r} is used twice",
                     field=field + "/name")  # fmt: skip
            names.add(key)


def _check_changes(changes) -> None:
    for i, change in enumerate(changes):
        field = f"/what_if/{i}"
        if change.get("mass") is not None and not abs(float(change["mass"])) <= 1e7:
            fail("NX_INVALID_ARGUMENT", "mass is -1e7 to 1e7 kg", field=field + "/mass")
        target = [k for k in ("component", "item") if change.get(k) is not None]
        moves = [k for k in ("offset", "move_to", "remove") if change.get(k) not in (None, False)]
        if not target:
            if change.get("mass") is None or change.get("center") is None or moves:
                fail(
                    "NX_INVALID_ARGUMENT",
                    "A what-if change adds mass at center, or moves/removes a component or item",
                    field=field,
                    hint="{name, mass, center} or {name, component|item, offset|move_to|remove}",
                )
            continue
        if len(target) > 1:
            fail("NX_INVALID_ARGUMENT", "Give component or item, not both", field=field)
        if len(moves) != 1 or change.get("mass") is not None or change.get("center") is not None:
            fail(
                "NX_INVALID_ARGUMENT",
                "Moving or removing takes exactly one of offset, move_to or remove=true",
                field=field,
            )


def _named_groups(groups) -> list[dict]:
    """``{group: [refs or name patterns]}`` as ``[{name, field, components, match}]``."""
    out, seen = [], set()
    for name, members in groups.items():
        field = f"/groups/{name}"
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            fail("NX_INVALID_ARGUMENT", "Group names are 1-64 characters", field="/groups")
        if name.casefold() in seen:
            fail("NX_INVALID_ARGUMENT", f"Group {name!r} is named twice", field=field)
        seen.add(name.casefold())
        if (
            not isinstance(members, list)
            or not 1 <= len(members) <= 500
            or not all(isinstance(m, str) and 0 < len(m) <= 128 for m in members)
        ):
            fail("NX_INVALID_ARGUMENT", "A group lists 1-500 component refs or name patterns",
                 field=field, hint='{"propulsion": ["ENGINE*", "o:12"]}')  # fmt: skip
        refs = [m for m in members if re.match(REF_PATTERN, m)]
        match = [m.casefold() for m in members if m not in refs]
        out.append({"name": name, "field": field, "components": refs, "match": match})
    return out


def _grouping(group_by, attribute, groups):
    if groups is not None and group_by not in (None, "named"):
        fail("NX_INVALID_ARGUMENT", "groups apply to group_by named", field="/groups")
    if groups is not None:
        group_by = "named"
        if not groups:
            fail("NX_INVALID_ARGUMENT", "groups names at least one group", field="/groups")
    if group_by == "named" and groups is None:
        fail("NX_INVALID_ARGUMENT", "group_by named needs groups", field="/groups")
    if group_by == "attribute" and not attribute:
        fail("NX_INVALID_ARGUMENT", "group_by attribute needs attribute", field="/attribute")
    if attribute and group_by != "attribute":
        fail("NX_INVALID_ARGUMENT", "attribute applies to group_by attribute", field="/attribute")
    return group_by


# ---------------------------------------------------------------------------
# Walking the assembly


class _Walk:
    """State of one report: resolved arguments, the rows found and what was skipped."""

    def __init__(self, tx, a):
        self.tx = tx
        self.a = a
        self.units = kunits.part_units(tx)
        self.to_part = 1.0 / kunits.MM_PER[self.units]
        self.records: list[dict] = []
        self.skipped: list[SkippedRow] = []
        self.part_assertions: dict[int, Any] = {}
        self.materials: dict[int, str | None] = {}
        self.sheets = 0

    # -- resolution ----------------------------------------------------------
    def resolve(self, ref, field):
        c = select.resolve(self.tx, ref, "component", field=field)
        if int(c.Tag) not in self.members:
            fail("NX_WRONG_OWNER", f"{ref} is not a component of the work part's assembly",
                 field=field, hint="Name the assembly with part")  # fmt: skip
        return c

    def prepare(self):
        tx, a = self.tx, self.a
        self.tree = kparts.components(tx.part)
        self.members = {int(c.Tag): c for c, _names in self.tree}
        self.include = (
            {int(self.resolve(r, f"/components/{i}").Tag) for i, r in enumerate(a["components"])}
            if a.get("components") else None
        )  # fmt: skip
        self.exclude = {
            int(self.resolve(r, f"/exclude/{i}").Tag) for i, r in enumerate(a.get("exclude") or [])
        }
        self.overrides: dict[int, tuple[int, dict]] = {}
        for i, item in enumerate(a.get("items") or []):
            if item.get("component") is not None:
                c = self.resolve(item["component"], f"/items/{i}/component")
                self.overrides[int(c.Tag)] = (i, item)
        self.named: list[tuple[str, set[int], list[str]]] = []
        for g in a.get("named") or []:
            tags = {int(self.resolve(r, g["field"]).Tag) for r in g["components"]}
            self.named.append((g["name"], tags, g["match"]))
        self.nx_groups = []
        if a.get("group_by") == "component_group":
            for group, found in sorted(mp.groups(tx.part), key=lambda gm: str(gm[0].Name)):
                self.nx_groups.append((str(group.Name), {int(c.Tag) for c in found}))
            if not self.nx_groups:
                tx.warn("NX_NOTE", "The assembly has no component groups; create them with "
                        "nx_create_component_group")  # fmt: skip

    # -- measuring -----------------------------------------------------------
    def material(self, body):
        occurrence = getattr(body, "IsOccurrence", False)
        prototype = getattr(body, "Prototype", None) if occurrence else body
        key = int((prototype if prototype is not None else body).Tag)
        if key not in self.materials:
            self.materials[key] = mp.material_name(body)
        return self.materials[key]

    def solids(self, bodies):
        found = [b for b in bodies if getattr(b, "IsSolidBody", False)]
        self.sheets += len(bodies) - len(found)
        return found

    def measure(self, bodies, pose=None) -> dict:
        """Mass, volume, centre, inertia and material facts of solids (part frame bodies
        placed with ``pose``)."""
        self.tx.checkpoint()
        physics = measure.inertia(self.tx, bodies)
        center, tensor = physics["center"], physics["inertia"]
        if pose is not None:
            center = mp.to_assembly(pose, center)
            tensor = mp.rotate_inertia(tensor, pose[1])
        names = [self.material(b) for b in bodies]
        known = sorted({n for n in names if n})
        source = "material" if None not in names else ("mixed" if known else "no_material")
        volume = physics["volume"]
        cubic_metres = volume * measure.M_PER[self.units] ** 3
        return {
            "mass": physics["mass"],
            "volume": volume,
            "center": center,
            "inertia": tensor,
            "material": ", ".join(known) or None,
            "source": source,
            "density": physics["mass"] / cubic_metres if cubic_metres > 0 else None,
            "bodies": len(bodies),
        }

    def own_bodies(self, component, suppressed):
        """Solids of one occurrence (not its children); a suppressed one's part solids
        with the pose to place them."""
        prototype = getattr(component, "Prototype", None)
        if suppressed:
            found = self.solids(list(getattr(prototype, "Bodies", None) or []))
            return found, (mp.pose_of(component) if found else None)
        return self.solids(kbodies.occurrences(component)), None

    def subtree_bodies(self, component, suppressed):
        out = []
        stack = [(component, suppressed)]
        while stack:
            node, hidden = stack.pop()
            hidden = hidden or bool(getattr(node, "IsSuppressed", False))
            if hidden and not self.a.get("include_suppressed"):
                continue
            found, pose = self.own_bodies(node, hidden)
            if found:
                out.append((found, pose))
            stack.extend((c, hidden) for c in node.GetChildren())
        return out

    def measured_subtree(self, component, suppressed) -> dict | None:
        parts = [
            self.measure(found, pose) for found, pose in self.subtree_bodies(component, suppressed)
        ]
        if not parts:
            return None
        total = mp.combine(mp.entry(p["mass"], p["center"], p["inertia"]) for p in parts)
        return {"mass": total["mass"], "center": total["center"], "inertia": total["inertia"]}

    # -- NX assertions ---------------------------------------------------------
    def assertion(self, component):
        """``(source, mass, centre in assembly or None)`` of an NX weight assertion."""
        if not self.a.get("asserted", True) or getattr(self.tx.uf, "Weight", None) is None:
            return None
        pose = None
        found = mp.asserted(self.tx, component)
        source = "asserted_component"
        if found is None or found["mass"] is None:
            prototype = getattr(component, "Prototype", None)
            if prototype is None:
                return None
            key = int(getattr(prototype, "Tag", 0) or id(prototype))
            if key not in self.part_assertions:
                self.part_assertions[key] = mp.asserted(self.tx, prototype)
            found, source = self.part_assertions[key], "asserted_part"
        if found is None or found["mass"] is None:
            return None
        center = found["center"]
        if center is not None:
            pose = mp.pose_of(component)
            center = mp.to_assembly(pose, geom.scale(center, self.to_part))
        return source, found["mass"], center

    # -- rows ------------------------------------------------------------------
    def add(self, row, entry, chain=(), item=None):
        self.records.append(
            {"row": row, "entry": entry, "chain": list(chain), "tags": set(chain), "item": item}
        )

    def component_row(self, component, names):
        row: MassRow = {
            "name": str(getattr(component, "Name", "") or names[-1]),
            "component": self.tx.ref(component, "component"),
            "path": "/".join(names),
            "part": _leaf(kparts.prototype_file(component)),
        }
        return row

    def skip(self, component, reason):
        if len(self.skipped) < MAX_SKIPPED:
            self.skipped.append({"component": self.tx.ref(component, "component"),
                                 "reason": reason})  # fmt: skip

    def walk(self):
        part = self.tx.part
        if self.include is None:
            for body in self.solids(list(part.Bodies)):
                facts = self.measure([body])
                row: MassRow = {"name": _body_name(body), "body": self.tx.ref(body, "body"),
                                "part": _leaf(kparts.file_of(part))}  # fmt: skip
                self.add(_with_facts(row, facts), mp.entry(facts["mass"], facts["center"],
                                                           facts["inertia"]))  # fmt: skip
        root = getattr(getattr(part, "ComponentAssembly", None), "RootComponent", None)
        if root is not None:
            for child in root.GetChildren():
                self.visit(child, [], [], self.include is None, False)

    def visit(self, component, names, chain, inside, hidden):
        names = [*names, str(getattr(component, "Name", "") or component.JournalIdentifier)]
        tag = int(component.Tag)
        chain = [*chain, tag]
        inside = inside or (self.include is not None and tag in self.include)
        if tag in self.exclude:
            if inside:
                self.skip(component, "excluded")
            return
        hidden = hidden or bool(getattr(component, "IsSuppressed", False))
        if hidden and not self.a.get("include_suppressed"):
            if inside:
                self.skip(component, "suppressed")
            return
        if inside and self.row_for(component, names, chain, hidden):
            return  # an override or assertion covers the children
        for child in component.GetChildren():
            self.visit(child, names, chain, inside, hidden)

    def row_for(self, component, names, chain, hidden) -> bool:
        """Add the component's row; True when it covers its children."""
        tag = int(component.Tag)
        row = self.component_row(component, names)
        if hidden:
            row["suppressed"] = True
        override = self.overrides.get(tag)
        loaded = getattr(component, "Prototype", None) is not None
        if override is not None:
            self.add_override(component, row, chain, hidden, override)
            return True
        found = self.assertion(component) if loaded else None
        if found is not None:
            source, mass, center = found
            self.add_fixed(component, row, chain, hidden, source, mass, center)
            return True
        if not loaded:
            self.skip(component, "not_loaded")
            return True
        bodies, pose = self.own_bodies(component, hidden)
        if bodies:
            facts = self.measure(bodies, pose)
            self.add(_with_facts(row, facts),
                     mp.entry(facts["mass"], facts["center"], facts["inertia"]), chain)  # fmt: skip
        elif not list(component.GetChildren()):
            row.update(source="none", mass=0.0, center_of_gravity=None, bodies=0)
            self.add(row, None, chain)
        return False

    def add_override(self, component, row, chain, hidden, override):
        index, item = override
        center = item.get("center")
        if center is not None and item.get("frame") == "component":
            center = mp.to_assembly(mp.pose_of(component), [float(v) for v in center])
        if item.get("name"):
            row["name"] = item["name"]
        self.add_fixed(component, row, chain, hidden, "override", item["mass"], center,
                       field=f"/items/{index}/center")  # fmt: skip

    def add_fixed(self, component, row, chain, hidden, source, mass, center, field=None):
        """A row with a given mass: the centre given, else the measured one (inertia
        scaled to the mass), else NX_INVALID_ARGUMENT / the component origin."""
        mass = float(mass)
        tensor = None
        if center is None:
            measured = self.measured_subtree(component, hidden)
            if measured is not None and measured["mass"] > 0:
                center = measured["center"]
                tensor = [[v * mass / measured["mass"] for v in r] for r in measured["inertia"]]
            elif field is not None:
                fail("NX_INVALID_ARGUMENT",
                     f"{row['name']} has no solid bodies to place its mass; give center",
                     field=field)  # fmt: skip
            else:
                center = mp.pose_of(component)[0]
                self.tx.warn("NX_NOTE", f"{row['name']}: the asserted mass has no centre of "
                             "gravity; it is placed at the component origin")  # fmt: skip
        row.update(source=source, mass=mass, center_of_gravity=[float(v) for v in center],
                   material=None, density=None, volume=None)  # fmt: skip
        self.add(row, mp.entry(mass, center, tensor), chain)

    def add_items(self):
        for item in self.a.get("items") or []:
            if item.get("component") is not None:
                continue
            row: MassRow = {"name": item["name"], "source": "assumed", "mass": float(item["mass"]),
                            "center_of_gravity": [float(v) for v in item["center"]]}  # fmt: skip
            self.add(row, mp.entry(item["mass"], item["center"]), item=item)


def _leaf(path) -> str:
    name = ntpath.basename(str(path or ""))
    return name[:-4] if name.lower().endswith(".prt") else name


def _body_name(body) -> str:
    return str(getattr(body, "Name", "") or "") or str(getattr(body, "JournalIdentifier", "body"))


def _with_facts(row, facts):
    row.update(
        source=facts["source"],
        material=facts["material"],
        density=facts["density"],
        volume=facts["volume"],
        mass=facts["mass"],
        center_of_gravity=facts["center"],
        bodies=facts["bodies"],
    )
    return row


# ---------------------------------------------------------------------------
# Groups, reference frame, what-if


def _attribute(tx, obj, title):
    kinds = getattr(getattr(tx.nx, "NXObject", None), "AttributeType", None)
    if kinds is None or obj is None or not hasattr(obj, "HasUserAttribute"):
        return None
    try:
        if obj.HasUserAttribute(title, kinds.Any, -1):
            return str(obj.GetUserAttributeAsString(title, kinds.Any, -1))
    except Exception:
        return None
    return None


def _group_of(walk, record) -> str:
    tx, a, row = walk.tx, walk.a, record["row"]
    how = a.get("group_by")
    item = record["item"]
    if item is not None:
        return item.get("group") or (item["name"] if how == "named" else ASSUMED)
    chain = [walk.members[t] for t in reversed(record["chain"])]  # the component first
    if "component" not in row:
        return WORK_PART
    if how == "top_component":
        return row["path"].split("/")[0]
    if how == "part":
        return row.get("part") or OTHER
    if how == "attribute":
        for node in chain:  # the component, then its parents; each also its part
            for obj in (node, getattr(node, "Prototype", None)):
                value = _attribute(tx, obj, a["attribute"])
                if value:
                    return value
        return OTHER
    if how == "component_group":
        tags = record["tags"]
        return next((name for name, members in walk.nx_groups if members & tags), OTHER)
    tags = record["tags"]
    words = [str(getattr(n, "Name", "")).casefold() for n in chain]
    words += [_leaf(kparts.prototype_file(n)).casefold() for n in chain]
    for name, members, patterns in walk.named:
        if members & tags or any(fnmatch.fnmatchcase(w, p) for p in patterns for w in words):
            return name
    return OTHER


def _summary(entries, ref) -> dict:
    total = mp.combine(e for e in entries if e is not None)
    out: dict[str, Any] = {"mass": total["mass"], "center_of_gravity": total["center"]}
    if ref is not None and total["center"] is not None:
        out["reference_cog"] = mp.in_frame(ref, total["center"])
    return out


def _what_if(walk, changes, ref, baseline) -> WhatIfData:
    entries: list[dict | None] = [dict(r["entry"]) if r["entry"] else None for r in walk.records]
    tx = walk.tx
    report: list[Change] = []
    for i, change in enumerate(changes):
        field = f"/what_if/{i}"
        if change.get("mass") is not None and not abs(float(change["mass"])) <= 1e7:
            fail("NX_INVALID_ARGUMENT", "mass is -1e7 to 1e7 kg", field=field + "/mass")
        if change.get("component") is None and change.get("item") is None:
            entries.append(mp.entry(change["mass"], change["center"]))
            report.append({"name": change["name"], "mass": float(change["mass"]), "rows": 0})
            continue
        if change.get("component") is not None:
            tag = int(walk.resolve(change["component"], field + "/component").Tag)
            chosen = [k for k, r in enumerate(walk.records) if tag in r["tags"]]
        else:
            key = change["item"].casefold()
            chosen = [
                k for k, r in enumerate(walk.records)
                if r["item"] is not None and r["item"]["name"].casefold() == key
            ]  # fmt: skip
        chosen = [k for k in chosen if entries[k] is not None]
        if not chosen:
            fail("NX_NOT_FOUND", "No reported mass belongs to this component or item",
                 field=field, hint="Check exclude/components; items are matched by name")  # fmt: skip
        moved = mp.combine(entries[k] for k in chosen)
        if change.get("remove"):
            for k in chosen:
                entries[k] = None
            report.append({"name": change["name"], "mass": -moved["mass"], "rows": len(chosen)})
            continue
        if change.get("offset") is not None:
            delta = [float(v) for v in change["offset"]]
        else:
            if moved["center"] is None:
                fail("NX_INVALID_ARGUMENT", "The chosen mass is zero; nothing to move", field=field)
            delta = geom.sub([float(v) for v in change["move_to"]], moved["center"])
        for k in chosen:
            moving = entries[k]
            assert moving is not None  # chosen keeps only live entries
            entries[k] = {**moving, "center": geom.add(moving["center"], delta)}
        report.append({"name": change["name"], "mass": 0.0, "rows": len(chosen), "shift": delta})
    tx.checkpoint()
    out: WhatIfData = _summary(entries, ref)  # type: ignore[assignment]
    out["mass_change"] = out["mass"] - baseline["mass"]
    before, after = baseline["center_of_gravity"], out["center_of_gravity"]
    out["shift"] = geom.sub(after, before) if before is not None and after is not None else None
    if ref is not None and out["shift"] is not None:
        out["reference_shift"] = mp.direction_in_frame(ref, out["shift"])
    out["changes"] = report
    return out


def _denoise(matrix):
    largest = max((abs(v) for row in matrix for v in row), default=0.0)
    return [[0.0 if abs(v) <= NOISE * largest else v for v in row] for row in matrix]


# ---------------------------------------------------------------------------
# The report


def report(tx, a) -> dict:
    """The full report (every row) for the arguments ``a`` of either tool."""
    _check_items(a.get("items") or [])
    _check_changes(a.get("what_if") or [])
    a["group_by"] = _grouping(a.get("group_by"), a.get("attribute"), a.get("groups"))
    a["named"] = _named_groups(a["groups"]) if a.get("groups") is not None else []
    reference = a.get("reference")
    ref = (
        mp.frame(reference["origin"], reference.get("x_axis"), reference.get("y_axis"))
        if reference else None
    )  # fmt: skip
    common.note_exploded(tx)
    walk = _Walk(tx, a)
    walk.prepare()
    walk.walk()
    walk.add_items()
    if not walk.records:
        fail("NX_NOT_FOUND", "Nothing to weigh: no solid bodies, components or items",
             hint="Check components/exclude, suppression and reference sets, or give items")  # fmt: skip
    entries = [r["entry"] for r in walk.records]
    total = mp.combine(e for e in entries if e is not None)
    totals: dict[str, Any] = _summary(entries, ref)
    mass = totals["mass"]
    for record in walk.records:
        row = record["row"]
        row["percent"] = 100.0 * row.get("mass", 0.0) / mass if mass > 0 else 0.0
        if ref is not None and row.get("center_of_gravity") is not None:
            row["reference_cog"] = mp.in_frame(ref, row["center_of_gravity"])
    if a.get("inertia") and total["inertia"] is not None:
        totals["inertia"] = _denoise(total["inertia"])
        totals["principal_moments"] = mp.principal_moments(total["inertia"])
        if ref is not None:
            totals["reference_inertia"] = _denoise(mp.inertia_about(total, ref))
    out: dict[str, Any] = {
        "units": {
            "mass": "kg",
            "length": walk.units,
            "density": "kg/m^3",
            "inertia": f"kg*{walk.units}^2",
        },
        "total": totals,
        "skipped": walk.skipped,
    }
    if a.get("group_by"):
        named: dict[str, list] = {}
        for record in walk.records:
            record["row"]["group"] = _group_of(walk, record)
            named.setdefault(record["row"]["group"], []).append(record)
        rows_out = []
        for name, records in named.items():
            g = _summary([r["entry"] for r in records], ref)
            g.update(name=name, rows=len(records),
                     percent=100.0 * g["mass"] / mass if mass > 0 else 0.0)  # fmt: skip
            rows_out.append(g)
        out["groups"] = sorted(rows_out, key=lambda g: -g["mass"])
    if a.get("what_if"):
        out["what_if"] = _what_if(walk, a["what_if"], ref, totals)
    rows = [r["row"] for r in walk.records]
    out["rows"] = rows
    out["row_count"] = len(rows)
    _notes(tx, walk, rows)
    return out


def _notes(tx, walk, rows) -> None:
    bare = [r["name"] for r in rows if r.get("source") in ("no_material", "mixed")]
    if bare:
        tx.warn("NX_NOTE", f"{len(bare)} row(s) weigh bodies without a material at NX's default "
                f"density (e.g. {bare[0]}); assign one with nx_set_material or give items")  # fmt: skip
    empty = [r["name"] for r in rows if r.get("source") == "none"]
    if empty:
        tx.warn("NX_NOTE", f"{len(empty)} component(s) have no solid bodies and no mass (e.g. "
                f"{empty[0]}): give items [{{component, mass, center}}] for bought-in parts")  # fmt: skip
    if walk.sheets:
        tx.warn("NX_NOTE", f"{walk.sheets} sheet bodies have no mass and were left out")
    if walk.a.get("group_by") == "component_group":
        overlap = sum(
            1 for r in walk.records
            if sum(1 for _n, members in walk.nx_groups if members & r["tags"]) > 1
        )  # fmt: skip
        if overlap:
            tx.warn("NX_NOTE", f"{overlap} row(s) are in several component groups; each counts "
                    "once, in the first group by name")  # fmt: skip


def _args(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items() if v is not None}


def _summary_text(a, say) -> str:
    what = common.count_text(a, "components", "component") if a.get("components") else "assembly"
    return f"Weight and CoG of {what}" + (f" by {a['group_by']}" if a.get("group_by") else "")


@tool(
    target="assembly",
    effect="read",
    label="Measure assembly weight and CoG",
    summary=_summary_text,
    keywords=(
        "weight report",
        "centre of gravity",
        "assembly mass",
        "trim stability",
        "lcg tcg vcg",
        "assumed mass",
        "weight groups",
        "what if weight",
    ),  # fmt: skip
    requires=("work_part",),
    cost="heavy",
    example={"items": [{"name": "CREW", "mass": 80, "center": [900, 0, 600]}]},
)
def nx_measure_assembly_mass(
    tx: Tx,
    *,
    components: Components | None = None,
    exclude: Excluded | None = None,
    include_suppressed: bool = False,
    items: ItemList | None = None,
    group_by: GroupBy | None = None,
    attribute: Attribute | None = None,
    groups: GroupMap | None = None,
    reference: Reference | None = None,
    what_if: Changes | None = None,
    inertia: bool = False,
) -> MassReportData:
    """Report weight and centre of gravity per component and in total, for trim and stability.
    Uses NX asserted masses; items add assumed ones. kg, part units."""
    a = _args(components=components, exclude=exclude, include_suppressed=include_suppressed,
              items=items, group_by=group_by, attribute=attribute, groups=groups,
              reference=reference, what_if=what_if, inertia=inertia)  # fmt: skip
    out = report(tx, a)
    out["truncated"] = len(out["rows"]) > MAX_ROWS
    out["rows"] = out["rows"][:MAX_ROWS]
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# CSV export

COLUMNS = (
    "section", "name", "path", "part", "group", "source", "material", "density_kg_m3",
    "volume", "mass_kg", "percent", "cog_x", "cog_y", "cog_z", "ref_x", "ref_y", "ref_z",
    "rows",
)  # fmt: skip


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return format(float(value), ".10g")
    return str(value)


def _line(section, row) -> list[str]:
    cog = row.get("center_of_gravity") or [None] * 3
    ref = row.get("reference_cog") or [None] * 3
    values = {
        "section": section,
        "name": row.get("name", ""),
        "path": row.get("path", ""),
        "part": row.get("part", ""),
        "group": row.get("group", ""),
        "source": row.get("source", ""),
        "material": row.get("material") or "",
        "density_kg_m3": row.get("density"),
        "volume": row.get("volume"),
        "mass_kg": row.get("mass"),
        "percent": row.get("percent"),
        "cog_x": cog[0], "cog_y": cog[1], "cog_z": cog[2],
        "ref_x": ref[0], "ref_y": ref[1], "ref_z": ref[2],
        "rows": row.get("rows"),
    }  # fmt: skip
    return [_cell(values[c]) for c in COLUMNS]


def csv_text(out) -> str:
    """The report as one CSV table; ``section`` tells rows, groups, total and what-if
    apart (Excel: open, then filter on section)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    units = out["units"]
    writer.writerow(COLUMNS)
    for row in out["rows"]:
        writer.writerow(_line("item" if "component" not in row and "body" not in row
                              else "component", row))  # fmt: skip
    for group in out.get("groups") or []:
        writer.writerow(_line("group", group))
    writer.writerow(_line("total", {"name": "TOTAL", **out["total"],
                                    "rows": out["row_count"]}))  # fmt: skip
    what_if = out.get("what_if")
    if what_if:
        for change in what_if["changes"]:
            writer.writerow(_line("what_if_change", {"name": change["name"],
                                                     "mass": change["mass"],
                                                     "rows": change["rows"]}))  # fmt: skip
        writer.writerow(_line("what_if_total", {"name": "WHAT-IF TOTAL", **what_if}))
    writer.writerow([])
    writer.writerow(["units", f"mass kg; lengths {units['length']}; density kg/m^3"])
    return buffer.getvalue()


class ExportData(TypedDict):
    path: str
    size: int
    sha256: str
    row_count: int
    total: Totals
    overwritten: bool


@tool(
    target="assembly",
    switch="none",  # measuring reads only; the CSV is written by the host
    effect="file",
    label="Export weight and CoG report (CSV)",
    summary=lambda a, say: f"Weight report to {say('path')}",
    keywords=("weight report csv", "mass report", "excel weight", "cog report", "export weights"),
    requires=("work_part",),
    cost="heavy",
    example={"path": "reports/weights.csv", "report_args": {"group_by": "top_component"}},
)
def nx_export_mass_report(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("replace", ".csv")],
    report_args: Annotated[dict, Json(), Doc("nx_measure_assembly_mass arguments")] | None = None,
    overwrite: bool = False,
) -> ExportData:
    """Write the weight and CoG report to an Excel-friendly .csv. report_args: the arguments
    of nx_measure_assembly_mass. Lines: rows, groups, total, what-if; the model is not
    changed. overwrite replaces a file."""
    target = Path(path)
    replacing = target.exists()
    if replacing and not overwrite:
        fail("NX_ALREADY_EXISTS", "/path: a file already exists there; set overwrite=true to "
             "replace it", field="/path", details={"path": path})  # fmt: skip
    a = _report_args(report_args or {})
    with tx.checks():  # measuring only: no file yet (not_started on a refusal)
        out = report(tx, a)
    temp = tx.files.stage(target, replace=overwrite)
    temp.write_text(csv_text(out), encoding="utf-8-sig", newline="")
    published = tx.files.publish()[0]
    return {
        **files.metadata(published, tx.workspace.root),
        "row_count": out["row_count"],
        "total": out["total"],
        "overwritten": replacing,
    }


def _report_args(given) -> dict:
    """``report_args`` checked by the report's own schema (gate 1), errors under
    ``/report_args``."""
    spec = catalogue.get("nx_measure_assembly_mass")
    assert spec is not None  # registered above, in this module
    try:
        checked = spec.check(given)
    except NXError as error:
        error.field = "/report_args" + (error.field or "")
        raise
    return {k: v for k, v in checked.args.items() if v is not None}
