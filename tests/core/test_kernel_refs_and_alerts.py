"""Gate 3r refuses bad refs before the mark; anchors of occurrences and components; NX
feature alerts after a call; failures explained; no-op calls; shared quantities."""

from __future__ import annotations

import itertools
import re
from types import SimpleNamespace as NS
from typing import Annotated, Literal, TypedDict

import pytest

from nx_mcp.core import kernel
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import (
    NXError,
    already_exists,
    article,
    classify,
    counted,
    fail,
    plural,
    singular,
)
from nx_mcp.core.types import (
    REF_PATTERN,
    Items,
    Length,
    RefTo,
    convert_quantity,
    quantity_formula,
)
from nx_mcp.core.validate import validate
from tests.core import tools
from tests.core.fakes import FakePart, FakeServices, Obj

SPECS = tools.specs()
KEYWORDS = ("alpha", "beta", "gamma")
_tags = itertools.count(50_000)


class Count(TypedDict):
    count: int


def nx_edit_features(
    tx,
    *,
    features: Annotated[list[Annotated[str, RefTo("feature")]], Items(1, 10)],
) -> Count:
    """Touch several features."""
    objs = [tx.resolve(f) for f in features]
    tx.part.add("body", "")
    return {"count": len(objs)}


def nx_edit_warned(tx) -> Count:
    """Add a body; NX attaches a warning to the first feature."""
    tx.part.add("body", "")
    first = tx.part.Features[0]
    first.warnings = ["Blend edges have been consumed"]
    return {"count": 1}


def nx_check_volume(tx, *, after: float = 0.0) -> Count:
    """Build a sheet whose volume read-back fails."""
    tx.part.add("body", "SHEET")
    fail(
        "NX_VERIFICATION_FAILED",
        f"NX left the volume 0 -> {after:g}",
        details={"check": "volume", "actual": {"before": 0.0, "after": after}},
    )


def nx_delete_one(tx, *, feature: Annotated[str, RefTo("feature")]) -> Count:
    """Delete one feature of the work part."""
    tx.part.objects.remove(tx.resolve(feature, "feature"))
    return {"count": 1}


def nx_set_nothing(tx) -> Count:
    """Change nothing and say so."""
    tx.part  # noqa: B018 (the handler reads the part)
    tx.warn("NX_NO_EFFECT", "Nothing to change")
    return {"count": 0}


def _spec(fn, **extra):
    return make_spec(
        fn,
        effect="model",
        label=fn.__name__,
        keywords=KEYWORDS,
        domain="modeling",
        requires=("work_part",),
        **extra,
    )


LOCAL = {
    fn.__name__: _spec(fn, example=example)
    for fn, example in (
        (nx_edit_features, {"features": ["f:1"]}),
        (nx_edit_warned, {}),
        (nx_check_volume, {}),
        (nx_set_nothing, {}),
        (nx_delete_one, {"feature": "f:1"}),
    )
}


def _warnable(obj):
    obj.warnings = []
    obj.GetFeatureWarningMessages = lambda: list(obj.warnings)
    return obj


@pytest.fixture
def svc(tmp_path):
    return FakeServices(tmp_path)


def call(svc, name, **args):
    spec = LOCAL.get(name) or SPECS[name]
    return kernel.run(spec, args, kernel.Context(svc))


# -- gate 3r --------------------------------------------------------------------------


def test_a_stale_list_item_is_refused_before_the_mark_with_its_pointer(svc):
    kept = svc.part.add("feature", "A")
    gone = svc.references.row(svc.part.add("feature", "B"), "feature")["id"]
    svc.registry.invalidate_part(svc.part_key(svc.part))  # every handle retired
    good = svc.references.row(kept, "feature")["id"]  # A listed again: a live ref
    marks = len(svc.mark_names)
    out = call(svc, "nx_edit_features", features=[good, gone])
    assert out["mutation_outcome"] == "not_started"
    assert out["error"]["code"] == "NX_STALE_REFERENCE" and out["error"]["field"] == "/features/1"
    assert len(svc.mark_names) == marks  # no undo mark was taken


def test_a_list_item_of_the_wrong_kind_is_refused_with_its_pointer(svc):
    feature = svc.references.row(svc.part.add("feature", "A"), "feature")["id"]
    body = svc.references.row(svc.part.add("body", "B"), "body")["id"]
    out = call(svc, "nx_edit_features", features=[feature, body])
    assert out["mutation_outcome"] == "not_started"
    assert out["error"]["code"] == "NX_WRONG_KIND" and out["error"]["field"] == "/features/1"


def test_refs_that_all_fail_never_fall_back_to_the_work_part(svc, tmp_path):
    other = FakePart(tmp_path / "b.prt")
    svc.extra_parts.append(other)
    ref = svc.references.row(other.add("feature", "X"), "feature")["id"]
    svc.registry.invalidate_part(svc.part_key(other))
    out = call(svc, "nx_delete_one", feature=ref)
    assert out["mutation_outcome"] == "not_started" and "part" not in out
    assert out["error"]["code"] == "NX_STALE_REFERENCE" and out["error"]["field"] == "/feature"
    assert svc.mark_names == []


# -- anchors --------------------------------------------------------------------------


class _Component:
    def __init__(self, jid, prototype, parent):
        self.Tag = next(_tags)
        self.JournalIdentifier = jid
        self.Name = jid.split()[1].upper() if " " in jid else jid
        self.Prototype = prototype
        self.Parent = parent
        self.IsOccurrence = True
        self.children: list = []
        self.found: dict = {}
        if parent is not None:
            parent.children.append(self)

    def GetChildren(self):
        return list(self.children)

    def FindOccurrence(self, proto):
        if proto.Tag not in self.found:
            occurrence = Obj("body", "", None, jid="HANDLE R-626")
            occurrence.IsOccurrence = True
            occurrence.OwningComponent = self
            occurrence.Prototype = proto
            self.found[proto.Tag] = occurrence
        return self.found[proto.Tag]


def _assembly(svc, tmp_path, names=("COMPONENT pin 1", "COMPONENT pin 2")):
    pin = FakePart(tmp_path / "pin.prt")
    svc.extra_parts.append(pin)
    proto = pin.add("body", "", jid="CYLINDER(1)")
    root = _Component("ROOT", svc.part, None)
    svc.part.ComponentAssembly = NS(RootComponent=root)
    components = [_Component(n, pin, root) for n in names]
    for c in components:
        c.FindOccurrence(proto).OwningPart = svc.part
    return proto, components


def test_occurrences_of_one_body_get_distinct_anchors_that_resolve(svc, tmp_path):
    proto, (first, second) = _assembly(svc, tmp_path)
    rows = [svc.references.row(c.FindOccurrence(proto), "body") for c in (first, second)]
    assert [r["anchor"] for r in rows] == [
        "@a.prt:COMPONENT pin 1>CYLINDER(1)#0",
        "@a.prt:COMPONENT pin 2>CYLINDER(1)#0",
    ]
    for row, component in zip(rows, (first, second), strict=True):
        assert re.match(REF_PATTERN, row["anchor"])
        assert svc.references.resolve(row["anchor"], ("body",)) is component.FindOccurrence(proto)


def test_an_anchor_that_matches_two_objects_names_none(svc, tmp_path):
    _assembly(svc, tmp_path, names=("COMPONENT blk 1", "COMPONENT blk 1"))  # a mirrored copy
    with pytest.raises(NXError) as caught:
        svc.references.resolve("@a.prt:COMPONENT blk 1", ("component",))
    assert caught.value.code == "NX_AMBIGUOUS_REFERENCE"
    assert caught.value.details["matches"] == 2


def test_component_anchors_follow_the_component_path(svc, tmp_path):
    _proto, (first, _second) = _assembly(svc, tmp_path)
    first.OwningPart = svc.part
    first.kind = "component"
    row = svc.references.row(first, "component", anchor=True)
    assert row["anchor"] == "@a.prt:COMPONENT pin 1"
    assert svc.references.resolve(row["anchor"], ("component",)) is first


def test_anchors_name_the_new_file_after_a_save_as(svc, tmp_path):
    feature = svc.part.add("feature", "", jid="EXTRUDE(2)")
    assert svc.references.row(feature, "feature")["anchor"] == "@a.prt:EXTRUDE(2)"
    svc.part.FullPath = str(tmp_path / "renamed.prt")
    assert svc.references.row(feature, "feature")["anchor"] == "@renamed.prt:EXTRUDE(2)"


def test_face_anchors_of_nx_create_anchor_resolve_in_ref_fields(svc):
    face = svc.part.add("face", "TOP")
    svc.uf = NS(Tag=NS(AskTagOfHandle=lambda handle: face.Tag if handle == "RMa R1" else 0))
    anchor = "@a.prt:face=RMa R1"
    assert re.match(REF_PATTERN, anchor)
    assert svc.references.resolve(anchor, ("face",)) is face
    with pytest.raises(NXError) as wrong:
        svc.references.resolve(anchor, ("edge",))
    assert wrong.value.code == "NX_WRONG_KIND"
    with pytest.raises(NXError) as gone:
        svc.references.resolve("@a.prt:face=RMa R9", ("face",))
    assert gone.value.code == "NX_STALE_REFERENCE"


# -- NX feature alerts, failures explained ------------------------------------------------


def test_a_new_feature_warning_comes_back_as_a_call_warning_naming_the_feature(svc):
    _warnable(svc.part.add("feature", "BLEND", jid="BLEND(4)"))
    out = call(svc, "nx_edit_warned")
    assert out["ok"], out
    [warning] = [w for w in out["warnings"] if "object" in w]
    assert warning["object"]["name"] == "BLEND" and "edges have been consumed" in warning["message"]
    again = call(svc, "nx_edit_warned")  # the same warning again is not new
    assert not [w for w in again["warnings"] if "object" in w]


def test_a_read_back_that_measured_no_change_is_a_build_failure_to_fix(svc):
    out = call(svc, "nx_check_volume", after=0.0)
    error = out["error"]
    assert error["code"] == "NX_BUILD_FAILED" and error["retry"] == "after_fix"
    assert error["category"] == "geometry_failed" and "0 -> 0" in error["message"]
    assert out["mutation_outcome"] == "rolled_back"
    moved = call(svc, "nx_check_volume", after=5.0)
    assert moved["error"]["code"] == "NX_VERIFICATION_FAILED"


def test_an_update_failure_names_the_features_nx_lists(svc):
    blend = svc.part.add("feature", "BLEND", jid="BLEND(1)")
    info = NS(ErrorObject=blend, Description="Update undo happened.", ErrorCode=670024)
    svc.session.UpdateManager = NS(ErrorList=NS(Length=1, GetErrorInfo=lambda i: info))
    out = call(svc, "nx_create_block", size=1, fail_with="NX_UPDATE_FAILED")
    error = out["error"]
    [failed] = error["details"]["failed_features"]
    assert failed["feature"]["name"] == "BLEND" and failed["nx_code"] == 670024
    assert "BLEND" in error["message"]


# -- outcomes and changes -------------------------------------------------------------------


def test_a_call_that_changed_nothing_and_said_so_is_not_applicable(svc):
    out = call(svc, "nx_set_nothing")
    assert out["ok"] and out["mutation_outcome"] == "not_applicable"
    assert svc.history == [] and svc.marks == {}


def test_a_batch_lists_what_its_steps_deleted_and_made_stale():
    created, modified, deleted, stale = [], [], [], []
    children = [
        {"changes": {"created": [{"id": "f:900"}], "deleted": []}},
        {
            "changes": {
                "deleted": [{"id": "f:5"}, {"id": "f:900"}],
                "modified": [{"id": "x:7"}],
                "stale_refs": ["fc:6", "fc:901"],
            }
        },
    ]
    kernel._merge_children(children, created, modified, deleted, stale, floor=100)
    assert created == [] and modified == [{"id": "x:7"}]
    assert deleted == [{"id": "f:5"}] and stale == ["fc:6"]


def test_the_receipt_keeps_who_sent_the_call(svc):
    spec = SPECS["nx_create_block"]
    out = kernel.run(spec, {"size": 1, "caller": "agent-a"}, kernel.Context(svc))
    assert out["ok"], out
    record = svc.store.get(out["operation_id"])
    assert record["caller"] == "agent-a"


# -- errors, grammar, quantities, validation ------------------------------------------------


def test_known_nx_codes_are_classified_and_others_ask_for_a_fix():
    class Native(Exception):
        def __init__(self, code):
            super().__init__("NX said no")
            self.ErrorCode = code

    assert classify(Native(1050017)).code == "NX_ALREADY_EXISTS"
    assert classify(Native(3270008), default="NX_BUILD_FAILED").code == "NX_INVALID_FORMULA"
    assert classify(Native(1405072)).hint.startswith("Place the view")
    unknown = classify(Native(4242))
    assert unknown.code == "NX_ERROR" and unknown.retry == "after_fix"
    assert classify(ValueError("bug")).retry == "ask_engineer"
    with pytest.raises(NXError) as exists:
        already_exists("SHT1 exists", field="/name", existing={"id": "sh:3"})
    assert exists.value.details["existing"] == {"id": "sh:3"}


def test_one_grammar_helper_for_plurals_and_articles():
    assert [plural(w) for w in ("body", "sketch", "face", "drawing view")] == [
        "bodies",
        "sketches",
        "faces",
        "drawing views",
    ]
    assert counted(1, "body") == "1 body" and counted(24, "body") == "24 bodies"
    assert [article(w) for w in ("explosion", "expression", "edge", "face", "unit")] == [
        "an",
        "an",
        "an",
        "a",
        "a",
    ]
    assert singular("bodies") == "body" and singular("sketches") == "sketch"


def test_quantities_take_more_units_masses_and_unitless_formulas():
    assert convert_quantity("2.5 cm", "length", "mm") == pytest.approx(25.0)
    assert convert_quantity("1 inch", "length", "mm") == pytest.approx(25.4)
    assert convert_quantity("0.1 m", "length", "in") == pytest.approx(100 / 25.4)
    assert convert_quantity("26 lb", "mass", "kg") == pytest.approx(26 * 0.45359237)
    assert convert_quantity("500 g", "mass", "kg") == pytest.approx(0.5)
    assert convert_quantity("90 degrees", "angle", "deg") == pytest.approx(90.0)
    assert quantity_formula("scale/2", "scalar") == "scale/2"
    with pytest.raises(ValueError):
        quantity_formula("total_mass", "mass")  # masses take no formulas
    with pytest.raises(ValueError):
        quantity_formula("10 mm", "mass")
    assert convert_quantity("p1*2", "scalar", "unitless", lambda *a, **k: 4.0) == 4.0


def nx_create_cut(
    tx, *, kind: Literal["blind", "through"] = "blind", depth: Length | None = None
) -> Count:
    """Cut to a depth or through."""
    return {"count": 0}


def test_a_present_but_invalid_field_is_not_also_reported_required():
    spec = _spec(nx_create_cut, example={})
    needs = {"blind": ("depth",)}
    with pytest.raises(NXError) as caught:
        validate(spec.params, {"kind": "blind", "depth": True}, variant="kind", required_by=needs)
    assert caught.value.field == "/depth" and "required when" not in str(caught.value.details)
    with pytest.raises(NXError) as missing:
        validate(spec.params, {"kind": "blind"}, variant="kind", required_by=needs)
    assert "required when kind is 'blind'" in missing.value.message
    with pytest.raises(NXError) as named:
        validate(spec.params, {"kind": "blind"}, variant="kind", required_by=needs,
                 tool="nx_create_cut")  # fmt: skip
    assert named.value.hint == "nx_get_tool_schemas nx_create_cut#blind lists the fields"


def test_a_sheet_named_by_its_name_gives_the_sheet_units(svc):
    svc.add_sheet("mm", current=True)
    inch = svc.part.add("drawing_sheet", "SHEET2")
    inch.units = "in"
    spec = SPECS["nx_create_label"]
    # FakePart has no DrawingSheets: give it the collection NX has.
    svc.part.DrawingSheets = [o for o in svc.part.objects if o.kind == "drawing_sheet"]
    sheet, units = kernel.sheet_target(NS(params=spec.params), {"sheet": "sheet2"}, [], svc)
    assert sheet is inch and units == "in"


def test_a_placeholder_may_leave_out_data_and_a_bad_one_lists_the_keys():
    from nx_mcp.core.validate import Checked

    prior = [{"tool": "nx_create_block", "data": {"feature": {"id": "f:7"}, "size": 1}}]
    checked = Checked(args={"feature": "$0.feature"})
    checked.placeholders.append((("feature",), "$0.feature"))
    kernel._resolve_placeholders(checked, prior)
    assert checked.args["feature"] == "f:7"
    bad = Checked(args={"feature": "$0.sketch"})
    bad.placeholders.append((("feature",), "$0.sketch"))
    with pytest.raises(NXError) as caught:
        kernel._resolve_placeholders(bad, prior)
    assert caught.value.details["available"] == ["feature", "size"]
    assert "feature, size" in caught.value.message


def test_an_unknown_field_of_a_tool_without_arguments_names_the_call_keys():
    from nx_mcp.core.validate import unknown_field

    error = unknown_field([], "prt", [])
    assert "call keys part, operation_id, intent" in error.message
    assert "did you mean 'part'" in error.message


def test_paths_follow_the_sidecar_suffix_rules(svc):
    from nx_mcp.core.types import PathArg

    def gate(mode, suffixes, given, args=None):
        checked = NS(
            paths=[(("path",), PathArg(mode, suffixes))], args={"path": given, **(args or {})}
        )
        kernel._gate_paths(SPECS["nx_create_block"], checked, svc)
        return checked.args["path"]

    assert gate("new", (".txt",), "out/n").endswith("n.txt")  # the only kind
    with pytest.raises(NXError) as several:
        gate("new", (".step", ".stp", ".igs"), "out/m")
    assert several.value.code == "NX_INVALID_ARGUMENT" and several.value.retry == "after_fix"
    with pytest.raises(NXError) as wrong:
        gate("new", (".txt",), "out/n.pdf")
    assert wrong.value.code == "NX_UNSUPPORTED_FILE_TYPE" and wrong.value.retry == "after_fix"
    (svc.root / "out").mkdir(exist_ok=True)
    with pytest.raises(NXError) as folder:
        gate("new", (".txt",), "out/")
    assert folder.value.code == "NX_WRONG_KIND"


def test_a_part_without_components_is_named_when_an_assembly_is_required(svc):
    svc.state = lambda: {"work_part": True, "assembly": False}  # type: ignore[method-assign]
    spec = NS(requires=("assembly",), name="nx_create_component_pattern")
    with pytest.raises(NXError) as caught:
        kernel._gate_requires(spec, svc, explicit=True)
    assert caught.value.field == "/part" and "a.prt has no components" in caught.value.message
