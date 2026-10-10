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
"""

from __future__ import annotations

import contextlib
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import deleter, lister, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Name, PlaneByNormal, RefTo
from nx_mcp.kit import display, geom, readback, select
from nx_mcp.kit.result import Row


class SectionData(TypedDict):
    section: Row
    origin: list[float]
    normal: list[float]
    cap: bool
    active: bool
    sectioning: bool
    hidden_side: str
    cut_face_view_from: list[float]
    remove_with: dict


def _context(tx):
    part = display.shown_part(tx, work=True)
    return part, display.model_view(tx, part)


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
    return {
        "section": ref,
        "origin": state["origin"],
        "normal": state["normal"],
        "cap": state["cap"],
        "active": state["active"],
        "sectioning": bool(view.DisplaySectioningToggle),
        "hidden_side": "positive",  # the half space the normal points into
        "cut_face_view_from": geom.unit(state["normal"]),
        "remove_with": {"tool": "nx_delete_objects", "arguments": {"objects": [ref["id"]]}},
    }


def _expect_plane(state, origin, normal):
    readback.expect_close(state["origin"], origin, "origin")
    readback.expect_same_direction(geom.unit(state["normal"]), normal, "normal")


@tool(
    effect="view",
    label="Create section view",
    summary=lambda a, say: (
        "Section the view at (" + ", ".join(f"{float(v):.6g}" for v in a["plane"]["origin"]) + ")"
    ),
    keywords=("section view", "clipping plane", "cut view", "dynamic section", "cross section"),
    requires=("work_part",),
    cost="light",
    example={"plane": {"origin": [10, 0, 0], "normal": [1, 0, 0]}},
)
def nx_create_section_view(
    tx: Tx, *, plane: PlaneByNormal, name: Name | None = None, cap: bool = True
) -> SectionData:
    """Cut the model view with a section plane; the model is unchanged. The side the normal
    points to is hidden; the cut face shows from cut_face_view_from. It stays (also saved
    with the part) until nx_delete_objects (remove_with) deletes it. One active at a time."""
    normal = geom.unit(plane["normal"], field="/plane/normal")
    origin = [float(v) for v in plane["origin"]]
    with tx.checks():
        part, view = _context(tx)
        current = view.ActiveDynamicSection
        if current is not None:
            fail(
                "NX_ALREADY_EXISTS",
                "A section is already active in the view",
                hint="Edit it with nx_edit_section_view or delete it with nx_delete_objects",
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
    section: Annotated[str, RefTo("section_view")],
    plane: PlaneByNormal | None = None,
    enabled: bool | None = None,
    cap: bool | None = None,
) -> SectionData:
    """Move a section plane, switch its cap, or enable/disable it (enabling makes it the
    active section). The normal's side is hidden. Delete sections with nx_delete_objects."""
    if plane is None and enabled is None and cap is None:
        fail("NX_INVALID_ARGUMENT", "Give plane, enabled or cap", field="/plane")
    normal = geom.unit(plane["normal"], field="/plane/normal") if plane else None
    origin = [float(v) for v in plane["origin"]] if plane else None
    with tx.checks():
        part, view = _context(tx)
        target = select.resolve(tx, section, "section_view", field="/section")
    with guarded(tx, part, view):
        if plane is not None or cap is not None:
            display.write_section(tx, part, view, section=target, origin=origin, normal=normal,
                                  cap=cap)  # fmt: skip
        if enabled is not None:
            display.set_sectioning(view, target, enabled)
        out = _data(tx, part, view, target)
        if plane is not None:
            _expect_plane(out, origin, normal)
        if cap is not None:
            readback.expect(out["cap"] == cap, "cap", cap=out["cap"])
        if enabled is not None:
            on = out["active"] and out["sectioning"]
            readback.expect(on == enabled, "enabled", active=out["active"],
                            sectioning=out["sectioning"])  # fmt: skip
    tx.frame = "display_part_absolute"
    return out


# ---- hooks: nx_list_objects(kind=section_view), nx_delete_objects ----------------------------


@lister("section_view")
def list_sections(tx, owner, owner_kind):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "Section views are listed for the whole part",
             field="/owner")  # fmt: skip
    part = tx.part
    found = display.sections(part)
    if not found:
        return []
    view = display.model_view(tx, part)
    out = []
    for section in found:
        state = display.section_state(tx, part, view, section)
        details = {k: state[k] for k in ("origin", "normal", "cap", "active")}
        details["sectioning"] = bool(view.DisplaySectioningToggle) and state["active"]
        out.append((section, "section_view", {"type": "one_plane", "details": details}))
    return out


@deleter("section_view")
def delete_sections(tx, objects):
    part = tx.part
    display.delete_sections(tx, part, display.model_view(tx, part), objects)
