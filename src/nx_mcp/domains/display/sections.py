"""Dynamic section views of the model view: create, edit, list, delete (tool-surface 3.12).

A section is a one-plane view clip in absolute coordinates: material on the positive
side of the normal (where it points) is hidden, the negative side and its cut face stay;
the model itself is unchanged. The cut face is seen from ``view_from = normal``
(``nx_set_camera(view_from=...)``). A section stays in the part (and is saved with it)
until ``nx_delete_objects`` removes it; ``remove_with`` in the result spells that call
out. NX 2406 findings:
committing a new section makes it active and switches view sectioning on;
``DeleteSections`` leaves sectioning on with no section left, so the deleter switches
it off when no active section remains (``kit.display.delete_sections``). A failed
create or edit puts the previous sections and sectioning state back itself.

Sections are display state, not in the agent's undo history, yet NX's session-wide undo
reverts them (live, 30 agents: a section vanished and an edit came back undone, with no
error). Each create or edit remembers the state it left (``_remembered``); the next
create, edit or listing in that part names a section that is gone or differs
(``NX_NOTE``) instead of silently working on the reverted one.
"""

from __future__ import annotations

import contextlib
import re
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import deleter, lister, tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import REF_PATTERN, Doc, Length, Name, Point3, RefTo, Text, Vec3
from nx_mcp.domains.display.common import choice
from nx_mcp.kit import display, geom, readback, select
from nx_mcp.kit.result import Row

# Principal planes and the normal each one's section gets (its positive side is hidden).
PRINCIPALS = {"xy": (0.0, 0.0, 1.0), "yz": (1.0, 0.0, 0.0), "xz": (0.0, 1.0, 0.0)}
PLANE_ALIASES = {"yx": "xy", "zy": "yz", "zx": "xz", "top": "xy", "front": "xz", "right": "yz"}
Principal = Annotated[str, Text(16), Doc("xy|yz|xz: normal +z|+x|+y; -xz flips")]
# A ref keeps its RefKind so batch placeholders ($1.data.section.id) still resolve.
SectionArg = Annotated[
    Annotated[str, RefTo("section_view", doc="sv:N")] | Annotated[str, Text(64)],
    Doc("Section ref or name"),
]


class SectionPlane(TypedDict, total=False):
    origin: Point3
    normal: Vec3


class SectionData(TypedDict):
    section: Row
    origin: list[float]
    normal: list[float]
    cap: bool
    enabled: bool
    active: bool
    sectioning: bool
    hidden_side: str
    cut_face_view_from: list[float]
    remove_with: dict


def _context(tx):
    part = display.shown_part(tx, work=True)
    return part, display.model_view(tx, part)


def _plane(plane, offset, current=None):
    """``(origin, normal)`` of a section plane given as ``{origin?, normal?}``, a principal
    plane name or nothing (``current`` plane, on edit), moved ``offset``.

    A principal plane lies at ``offset`` along its axis ('xz', offset 30: y = 30); a
    leading '-' only flips the normal (the hidden side). Otherwise ``offset`` moves the
    origin along the unit normal, and a missing origin or normal is the current one
    (edit) or the absolute origin (create; a normal is required)."""
    if isinstance(plane, str):
        text = plane.strip()
        sign = -1.0 if text.startswith("-") else 1.0
        name = re.sub(r"(?i)\s*plane$", "", text.lstrip("+-"))
        try:
            name = choice(name, tuple(PRINCIPALS), "/plane", PLANE_ALIASES)
        except NXError as error:
            fail(
                "NX_INVALID_ARGUMENT",
                str(error),
                field="/plane",
                hint="Name 'xy', 'yz' or 'xz' with offset (a leading '-' flips the normal: '-xz'), "
                "or give {origin: [x, y, z], normal: [x, y, z]}",
            )
        axis = PRINCIPALS[name]
        return [float(offset or 0.0) * v for v in axis], [sign * v for v in axis]
    spec = dict(plane or {})
    if "normal" in spec:
        normal = geom.unit(spec["normal"], field="/plane/normal")
    elif current is not None:
        normal = geom.unit(current["normal"])
    else:
        fail(
            "NX_INVALID_ARGUMENT",
            "The section plane needs a normal",
            field="/plane/normal",
            hint="Send plane {origin:[0,30,0], normal:[0,1,0]}, or plane 'xz' with offset 30",
        )
    origin = spec.get("origin") or (current["origin"] if current else [0.0, 0.0, 0.0])
    origin = [float(v) + float(offset or 0.0) * n for v, n in zip(origin, normal, strict=True)]
    return origin, normal


def _section_of(tx, part, value):
    """A section of the part from its ref or its name (any case; spaces, hyphens and
    underscores alike: 'natural cut' finds NATURAL_CUT)."""
    text = str(value).strip()
    if re.match(REF_PATTERN, text):
        return select.resolve(tx, text, "section_view", field="/section")
    found = display.sections(part)

    def key(name) -> str:
        return re.sub(r"[\s_-]+", "_", str(name).strip()).casefold()

    named = [s for s in found if key(getattr(s, "Name", "")) == key(text)]
    if len(named) == 1:
        return named[0]
    fail(
        "NX_AMBIGUOUS_REFERENCE" if named else "NX_NOT_FOUND",
        f"{len(named)} sections are named {text!r}" if named else f"No section named {text!r}",
        field="/section",
        hint="Pass a section ref (sv:N) from details.sections",
        details={"sections": [tx.ref(s, "section_view") for s in (named or found)]},
    )


def _remembered(tx, part) -> dict:
    """``{section tag: state}`` this NX session's create and edit calls left in ``part``."""
    services = getattr(tx, "services", None)
    if services is None:  # a hook called outside the kernel: nothing remembered
        return {}
    store = services.__dict__.setdefault("_section_states", {})
    return store.setdefault(services.part_key(part), {})


def _remember(tx, part, section, state) -> None:
    keep = {k: state[k] for k in ("origin", "normal", "cap")}
    _remembered(tx, part)[int(section.Tag)] = {**keep, "name": str(getattr(section, "Name", ""))}


def _note_reverts(tx, part, view) -> None:
    """An ``NX_NOTE`` naming sections that are gone or changed since a call set them (an
    undo or an engineer edit reverted them); forgets them, so it is said once."""
    known = _remembered(tx, part)
    if not known or view is None:
        return
    live = {int(s.Tag): s for s in display.sections(part)}
    said = []
    for tag, was in list(known.items()):
        if tag not in live:
            said.append(f"{was['name'] or tag} is gone")
            del known[tag]
            continue
        try:
            now = display.section_state(tx, part, view, live[tag])
        except Exception:
            continue
        moved = not (
            geom.dist(now["origin"], was["origin"]) <= geom.tol(now["origin"], was["origin"])
            and geom.same_direction(geom.unit(now["normal"]), geom.unit(was["normal"]), 1e-6)
        )
        if moved or now["cap"] != was["cap"]:
            said.append(
                f"{was['name'] or tag} is at origin {_rounded(now['origin'])} normal "
                f"{_rounded(now['normal'])} cap {now['cap']} (left at {_rounded(was['origin'])} "
                f"normal {_rounded(was['normal'])} cap {was['cap']})"
            )
            known[tag] = {**known[tag], **{k: now[k] for k in ("origin", "normal", "cap")}}
    if said:
        tx.warn(
            "NX_NOTE",
            "Since an earlier call set them, section " + "; ".join(said) + ": an undo or an "
            "engineer edit reverted them (sections are not in the agent's undo history)",
        )


def _rounded(values) -> list:
    return [round(float(v), 6) for v in values]


def _hidden_side(origin, normal) -> str:
    """The half space a section hides, spelled out: 'y > 30' for an axis normal."""
    n = geom.unit(normal)
    for i, axis in enumerate("xyz"):
        if abs(abs(n[i]) - 1.0) < 1e-9:
            return f"{axis} {'>' if n[i] > 0 else '<'} {origin[i]:.6g}"
    return "where the normal points: (p - origin) . normal > 0"


@contextlib.contextmanager
def guarded(tx, part, view):
    """Put sections and the sectioning state back when the block fails.

    New sections are deleted, edited ones get their plane and cap back, and the
    active section and the view's sectioning flag are restored.
    """
    before = {int(s.Tag): s for s in display.sections(part)}
    states = {tag: display.section_state(tx, part, view, s) for tag, s in before.items()}
    active, toggle = view.ActiveDynamicSection, bool(view.DisplaySectioningToggle)
    tx.display.keep_camera()  # a failure is then reported as rolled back
    try:
        yield
    except BaseException:
        with contextlib.suppress(Exception):
            new = [s for s in display.sections(part) if int(s.Tag) not in before]
            if new:
                part.DynamicSections.DeleteSections(False, new)
        for tag, section in before.items():
            with contextlib.suppress(Exception):
                state = states[tag]
                if display.section_state(tx, part, view, section) != state:
                    display.write_section(
                        tx, part, view, section=section, origin=state["origin"],
                        normal=state["normal"], cap=state["cap"],
                    )  # fmt: skip
        with contextlib.suppress(Exception):
            if active is not None:
                view.ActiveDynamicSection = active
            view.DisplaySectioningToggle = toggle
        raise


def _data(tx, part, view, section) -> SectionData:
    state = display.section_state(tx, part, view, section)
    ref = tx.ref(section, "section_view")
    sectioning = bool(view.DisplaySectioningToggle)
    return {
        "section": ref,
        "origin": state["origin"],
        "normal": state["normal"],
        "cap": state["cap"],
        "enabled": state["active"] and sectioning,  # it cuts the view now
        "active": state["active"],  # the view's current section, cutting or not
        "sectioning": sectioning,
        "hidden_side": _hidden_side(state["origin"], state["normal"]),
        "cut_face_view_from": geom.unit(state["normal"]),
        "remove_with": {"tool": "nx_delete_objects", "arguments": {"objects": [ref["id"]]}},
    }


def _expect_plane(state, origin, normal):
    readback.expect_close(state["origin"], origin, "origin")
    readback.expect_same_direction(geom.unit(state["normal"]), normal, "normal")


def _summary(a, say):
    plane = a.get("plane")
    if isinstance(plane, dict) and plane.get("origin") is not None:
        where = "(" + ", ".join(f"{float(v):.6g}" for v in plane["origin"]) + ")"
    elif isinstance(plane, dict):
        where = str(plane.get("principal") or "the origin")
    else:
        where = str(plane)
    offset = f" offset {a['offset']}" if a.get("offset") is not None else ""
    return f"Section the view at {where}{offset}"


@tool(
    effect="view",
    label="Create section view",
    summary=_summary,
    keywords=("section view", "clipping plane", "cut view", "dynamic section", "cross section"),
    requires=("work_part",),
    cost="light",
    example={"plane": {"origin": [10, 0, 0], "normal": [1, 0, 0]}},
)
def nx_create_section_view(
    tx: Tx,
    *,
    plane: SectionPlane | Principal,
    offset: Length | None = None,
    name: Name | None = None,
    cap: bool = True,
) -> SectionData:
    """Cut the model view with a section plane, {origin, normal} or 'xz' + offset; the model
    is unchanged. The normal's side is hidden (hidden_side). Saved with the part until
    nx_delete_objects (remove_with). One active at a time."""
    origin, normal = _plane(plane, offset)
    with tx.checks():
        part, view = _context(tx)
        _note_reverts(tx, part, view)
        current = view.ActiveDynamicSection
        if current is not None:
            fail(
                "NX_ALREADY_EXISTS",
                "A section is already active in the view",
                hint="Move it with nx_edit_section_view(section=details.section.id, plane=...) "
                "or delete it with nx_delete_objects",
                details={"section": tx.ref(current, "section_view")},
            )
    with guarded(tx, part, view):
        section = display.write_section(
            tx, part, view, origin=origin, normal=normal, cap=cap, name=name or "MCP section"
        )
        display.set_sectioning(view, section, True)
        out = _data(tx, part, view, section)
        _expect_plane(out, origin, normal)
        readback.expect(out["cap"] == cap, "cap", cap=out["cap"])
        readback.expect(out["active"] and out["sectioning"], "active", active=out["active"])
    _remember(tx, part, section, out)
    tx.frame = "display_part_absolute"
    return out


@tool(
    effect="view",
    label="Edit section view",
    summary="Edit section {section}",
    keywords=("move section", "section plane", "enable section", "disable section", "cap"),
    requires=("work_part",),
    cost="light",
    example={"section": "sv:5", "enabled": False},
)
def nx_edit_section_view(
    tx: Tx,
    *,
    section: SectionArg,
    plane: SectionPlane | Principal | None = None,
    offset: Length | None = None,
    enabled: bool | None = None,
    cap: bool | None = None,
) -> SectionData:
    """Move a section plane, switch its cap, or enable/disable it. A plane with only origin
    or normal keeps the other. offset moves the given or current origin along the normal
    (relative); with a plane name it is the absolute position, as on create."""
    if plane is None and offset is None and enabled is None and cap is None:
        fail("NX_INVALID_ARGUMENT", "Give plane, offset, enabled or cap", field="/plane")
    with tx.checks():
        part, view = _context(tx)
        _note_reverts(tx, part, view)
        target = _section_of(tx, part, section)
        moved = plane is not None or offset is not None
        origin = normal = None
        if moved:
            current = display.section_state(tx, part, view, target)
            origin, normal = _plane(plane, offset, current)
    with guarded(tx, part, view):
        if moved or cap is not None:
            display.write_section(tx, part, view, section=target, origin=origin, normal=normal,
                                  cap=cap)  # fmt: skip
        if enabled is not None:
            display.set_sectioning(view, target, enabled)
        out = _data(tx, part, view, target)
        if moved:
            _expect_plane(out, origin, normal)
        if cap is not None:
            readback.expect(out["cap"] == cap, "cap", cap=out["cap"])
        if enabled is not None:
            on = out["active"] and out["sectioning"]
            readback.expect(on == enabled, "enabled", active=out["active"],
                            sectioning=out["sectioning"])  # fmt: skip
    _remember(tx, part, target, out)
    tx.frame = "display_part_absolute"
    return out


# ---- hooks: nx_list_objects(kind=section_view), nx_delete_objects ----------------------------


def _listing_view(tx, part):
    """``(view, displayed)``: the model view a listing reads sections through.

    The displayed part: its work view (refused while a sheet is shown). A part in the
    background (a read bound to it, gate 4s): NX 2406 refuses its ``WorkView`` ("only on
    a displayed part"), so the modeling view holding an active section (the work view it
    was made in), else the first one; any view reads a section's plane."""
    if display.same(tx.session.Parts.Display, part):
        return display.model_view(tx, part), True
    views = list(getattr(part, "ModelingViews", []) or [])
    for view in views:
        if getattr(view, "ActiveDynamicSection", None) is not None:
            return view, False
    return (views[0] if views else None), False


@lister("section_view")
def list_sections(tx, owner, owner_kind):
    """Rows with the plane, cap and state. ``sectioning`` is None for a part in the
    background: NX 2406 reads its views' sectioning toggle as off until it is displayed
    again (the toggle comes back then)."""
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "Section views are listed for the whole part",
             field="/owner")  # fmt: skip
    part = tx.part
    if _remembered(tx, part):
        with contextlib.suppress(NXError):  # a shown sheet: no view to compare through
            _note_reverts(tx, part, _listing_view(tx, part)[0])
    found = display.sections(part)
    if not found:
        return []
    view, displayed = _listing_view(tx, part)
    out = []
    for section in found:
        state = display.section_state(tx, part, view, section)
        details = {k: state[k] for k in ("origin", "normal", "cap", "active")}
        details["sectioning"] = (
            bool(view.DisplaySectioningToggle) and state["active"] if displayed else None
        )
        out.append((section, "section_view", {"type": "one_plane", "details": details}))
    return out


@deleter("section_view")
def delete_sections(tx, objects):
    part = tx.part
    display.delete_sections(tx, part, display.model_view(tx, part), objects)
    known = _remembered(tx, part)
    for obj in objects:
        known.pop(int(obj.Tag), None)
