"""Feature tree (suppress, make current, reorder): local contract tests.

Native behaviour is validated live on NX (acceptance/live/feature_tree.json);
these tests cover the reorder dependency rules, argument validation, the
simulated suppression/reorder seam and the public schemas.
"""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.feature_tree import child_map, descendants, expected_order, reorder_conflicts
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace
from tests.fakes import Feature

NEW_TOOLS = {"nx_suppress_features", "nx_make_current_feature", "nx_reorder_feature"}

# Timeline: 1 sketch -> 2 block; 3 sketch -> 4 boss(2,3); 5 hole(4); 6 blend(2)
ORDER = [1, 2, 3, 4, 5, 6]
PARENTS = {2: {1}, 4: {2, 3}, 5: {4}, 6: {2}}
CHILDREN = {1: {2}, 2: {4, 6}, 3: {4}, 4: {5}}


def test_expected_order_moves_one_feature():
    assert expected_order(ORDER, 6, 4, "before") == [1, 2, 3, 6, 4, 5]
    assert expected_order(ORDER, 6, 2, "after") == [1, 2, 6, 3, 4, 5]
    assert expected_order(ORDER, 3, 5, "after") == [1, 2, 4, 5, 3, 6]
    assert expected_order(ORDER, 5, 6, "after") == [1, 2, 3, 4, 6, 5]


@pytest.mark.parametrize(
    ("moved", "target", "position", "conflicts"),
    [
        (6, 4, "before", []),  # blend only depends on the block
        (6, 3, "before", []),
        (6, 2, "after", []),
        (6, 2, "before", [(2, "parent")]),  # blend before its parent block
        (6, 1, "before", [(2, "parent")]),
        (5, 4, "before", [(4, "parent")]),  # hole before its boss
        (5, 4, "after", []),  # already there: no conflict
        (4, 5, "after", [(5, "child")]),  # boss after its hole
        (4, 6, "after", [(5, "child")]),
        (3, 4, "after", [(4, "child")]),  # sketch after its extrude
        (3, 4, "before", []),
        (3, 1, "before", []),
    ],
)
def test_reorder_dependency_rules(moved, target, position, conflicts):
    assert reorder_conflicts(ORDER, PARENTS, CHILDREN, moved, target, position) == conflicts


def test_reorder_rules_reject_bad_arguments():
    with pytest.raises(NXToolError, match="differ"):
        reorder_conflicts(ORDER, PARENTS, CHILDREN, 4, 4, "before")
    with pytest.raises(NXToolError, match="before or after"):
        reorder_conflicts(ORDER, PARENTS, CHILDREN, 4, 5, "inside")


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        ("nx_suppress_features", {"features": [], "suppress": True}, "non-empty list"),
        ("nx_suppress_features", {"features": "obj_1", "suppress": True}, "non-empty list"),
        ("nx_suppress_features", {"features": ["a", "a"], "suppress": True}, "repeat"),
        ("nx_suppress_features", {"features": [""], "suppress": False}, "non-empty feature"),
        ("nx_suppress_features", {"features": ["a"], "suppress": "yes"}, "true or false"),
        (
            "nx_suppress_features",
            {"features": [f"f{i}" for i in range(101)], "suppress": True},
            "at most 100",
        ),
        ("nx_make_current_feature", {"feature": ""}, "feature ID"),
        ("nx_reorder_feature", {"feature": "a", "target": "b", "position": "on"}, "before or"),
        ("nx_reorder_feature", {"feature": "", "target": "b", "position": "after"}, "feature"),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, tool, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute(tool, params)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    assert error.value.details.get("mutation_outcome") in {"rolled_back", "not_started"}


class TreeFeature(Feature):
    def __init__(self, part, name, stamp):
        super().__init__(name)
        self.OwningPart, self.Timestamp, self.Suppressed = part, stamp, False
        self.IsOccurrence = False
        self.parents, self.children = [], []

    def GetParents(self):
        return self.parents

    def GetChildren(self):
        return self.children

    def MakeCurrentFeature(self):
        self.OwningPart.CurrentFeature = self


def _tree(rig):
    """block -> boss -> hole, block -> blend, with simulated native tree ops."""
    part = rig.part
    block, boss, hole, blend = (
        TreeFeature(part, n, i) for i, n in enumerate(["Block", "Boss", "Hole", "Blend"])
    )
    boss.parents, block.children = [block], [boss, blend]
    hole.parents, boss.children = [boss], [hole]
    blend.parents = [block]
    features = part.Features
    features.extend([block, boss, hole, blend])

    def descendants(f):
        out = []
        for c in f.children:
            out += [c, *descendants(c)]
        return out

    def suppress(values):
        for f in values:
            for d in [f, *descendants(f)]:
                d.Suppressed = True

    def unsuppress(values):  # Like NX 2406: only the listed features.
        for f in values:
            f.Suppressed = False
        return []

    def reorder(values, target, kind):
        (moved,) = values
        stamps = [f.Timestamp for f in sorted(features, key=lambda f: f.Timestamp)]
        order = [f for f in sorted(features, key=lambda f: f.Timestamp) if f is not moved]
        at = order.index(target) + (kind == "after")
        order.insert(at, moved)
        for f, s in zip(order, stamps, strict=True):
            f.Timestamp = s

    def ask_suppress(tag):
        """UF mask: 1 = suppressed by the user, 16 = inactive (after current)."""
        ordered = sorted(features, key=lambda f: f.Timestamp)
        current = ordered.index(part.CurrentFeature)
        index, f = next((i, f) for i, f in enumerate(ordered) if f.Tag == tag)
        return int(f.Suppressed) | (16 if index > current else 0)

    part.CurrentFeature = blend
    rig.uf.Modeling = NS(AskSuppressFeature=ask_suppress)
    features.SuppressFeatures = suppress
    features.UnsuppressFeatures = unsuppress
    features.ReorderFeature = reorder
    rig.e.nxopen.Features.FeatureCollection = NS(ReorderType=NS(Before="before", After="after"))
    return {f.Name: rig.ref(f, "feature") for f in (block, boss, hole, blend)}


def test_suppress_reports_dependents_and_unsuppress_restores(rig):
    ids = _tree(rig)
    result = rig.e.execute("nx_suppress_features", {"features": [ids["Boss"]], "suppress": True})
    changed = {c["name"]: c for c in result["changed"]}
    assert set(changed) == {"Boss", "Hole"}
    assert changed["Boss"]["requested"] and not changed["Hole"]["requested"]
    assert changed["Hole"]["before"] == {"suppressed": False}
    assert changed["Hole"]["after"] == {"suppressed": True}
    assert [t["suppressed"] for t in result["timeline"]] == [False, True, True, False]
    again = rig.e.execute("nx_suppress_features", {"features": [ids["Boss"]], "suppress": True})
    assert again["changed"] == [] and again["unchanged"][0]["name"] == "Boss"
    back = rig.e.execute("nx_suppress_features", {"features": [ids["Boss"]], "suppress": False})
    assert [c["name"] for c in back["changed"]] == ["Boss"]
    assert [d["name"] for d in back["still_suppressed_dependents"]] == ["Hole"]
    rest = rig.e.execute(
        "nx_suppress_features", {"features": [ids["Boss"], ids["Hole"]], "suppress": False}
    )
    assert [c["name"] for c in rest["changed"]] == ["Hole"]
    assert [u["name"] for u in rest["unchanged"]] == ["Boss"]
    assert rest["still_suppressed_dependents"] == []


def test_suppress_verification_failure_rolls_back(rig):
    ids = _tree(rig)
    rig.part.Features.SuppressFeatures = lambda values: None  # NX ignores the request
    with pytest.raises(NXToolError) as error:
        rig.e.execute("nx_suppress_features", {"features": [ids["Blend"]], "suppress": True})
    assert error.value.code == "NX_VERIFICATION_FAILED"
    assert error.value.details["mutation_outcome"] == "rolled_back"


def test_reorder_moves_independent_feature_and_rejects_dependency(rig):
    ids = _tree(rig)
    result = rig.e.execute(
        "nx_reorder_feature", {"feature": ids["Blend"], "target": ids["Boss"], "position": "before"}
    )
    assert [t["name"] for t in result["timeline"]] == ["Block", "Blend", "Boss", "Hole"]
    assert [t["name"] for t in result["timeline_before"]] == ["Block", "Boss", "Hole", "Blend"]
    assert result["moved"] is True and result["feature"]["index"] == 1
    with pytest.raises(NXToolError) as error:
        rig.e.execute(
            "nx_reorder_feature",
            {"feature": ids["Hole"], "target": ids["Boss"], "position": "before"},
        )
    assert error.value.code == "NX_REORDER_DEPENDENCY"
    assert error.value.details["conflicts"][0]["name"] == "Boss"
    assert error.value.details["conflicts"][0]["relation"] == "parent"
    assert error.value.details["mutation_outcome"] == "rolled_back"


def test_make_current_reports_inactive_and_restores_last(rig):
    ids = _tree(rig)
    result = rig.e.execute("nx_make_current_feature", {"feature": ids["Block"]})
    assert result["current"]["name"] == "Block" and result["current"]["index"] == 0
    assert [f["name"] for f in result["inactive"]] == ["Boss", "Hole", "Blend"]
    assert all(f["suppressed"] is False for f in result["inactive"])
    assert [t["active"] for t in result["timeline"]] == [True, False, False, False]
    assert {f["name"] for f in result["activity_changed"]} == {"Boss", "Hole", "Blend"}
    last = rig.e.execute("nx_make_current_feature", {})
    assert last["current"]["name"] == "Blend" and last["inactive"] == []
    assert all(t["active"] for t in last["timeline"])


def test_make_current_verification_failure_rolls_back(rig):
    ids = _tree(rig)
    rig.part.Features[0].MakeCurrentFeature = lambda: None  # NX ignores the request
    with pytest.raises(NXToolError) as error:
        rig.e.execute("nx_make_current_feature", {"feature": ids["Block"]})
    assert error.value.code == "NX_VERIFICATION_FAILED"
    assert error.value.details["mutation_outcome"] == "rolled_back"


def test_internal_features_are_rejected(rig):
    ids = _tree(rig)
    rig.part.Features[1].IsInternal = True
    with pytest.raises(NXToolError, match="Internal features") as error:
        rig.e.execute("nx_suppress_features", {"features": [ids["Boss"]], "suppress": True})
    assert error.value.code == "NX_INVALID_ARGUMENT"


def test_reorder_rejects_same_feature(rig):
    ids = _tree(rig)
    with pytest.raises(NXToolError, match="differ"):
        rig.e.execute(
            "nx_reorder_feature",
            {"feature": ids["Boss"], "target": ids["Boss"], "position": "after"},
        )


def test_child_map_uses_parents_because_native_get_children_is_one_sided():
    a, b, c = (NS(Tag=i, parents=[], kids=[]) for i in (1, 2, 3))
    b.parents, c.parents, a.kids = [a], [b], []  # NX: block.GetChildren() == []
    for f in (a, b, c):
        f.GetParents = lambda f=f: f.parents
        f.GetChildren = lambda f=f: f.kids
    children = child_map([a, b, c])
    assert children == {1: {2}, 2: {3}}
    assert descendants(children, [1]) == [2, 3]


def _one_sided(rig):
    """Like NX 2406: the block's GetChildren is empty although Boss and Blend list it."""
    ids = _tree(rig)
    rig.part.Features[0].children = []
    return ids


def test_unsuppress_lists_dependents_missing_from_native_get_children(rig):
    ids = _one_sided(rig)
    rig.e.execute("nx_suppress_features", {"features": [ids["Block"]], "suppress": True})
    rig.part.Features[0].children = []
    for f in rig.part.Features:  # the simulated NX suppresses every dependent
        f.Suppressed = True
    back = rig.e.execute("nx_suppress_features", {"features": [ids["Block"]], "suppress": False})
    assert [c["name"] for c in back["changed"]] == ["Block"]
    assert {d["name"] for d in back["still_suppressed_dependents"]} == {"Boss", "Hole", "Blend"}


def test_reorder_detects_children_missing_from_native_get_children(rig):
    ids = _one_sided(rig)
    with pytest.raises(NXToolError) as error:
        rig.e.execute(
            "nx_reorder_feature",
            {"feature": ids["Block"], "target": ids["Boss"], "position": "after"},
        )
    assert error.value.code == "NX_REORDER_DEPENDENCY"
    assert [c["name"] for c in error.value.details["conflicts"]] == ["Boss"]


def test_suppression_change_of_inactive_feature_is_rejected(rig):
    ids = _tree(rig)
    rig.e.execute("nx_make_current_feature", {"feature": ids["Block"]})
    for suppress in (True, False):  # A rejected call invalidates the part's IDs.
        hole = rig.ref(rig.part.Features[2], "feature")
        with pytest.raises(NXToolError, match="inactive") as error:
            rig.e.execute("nx_suppress_features", {"features": [hole], "suppress": suppress})
        assert error.value.code == "NX_INVALID_ARGUMENT"
        assert error.value.details["inactive"][0]["name"] == "Hole"
    assert not any(f.Suppressed for f in rig.part.Features)


@pytest.mark.asyncio
async def test_new_tools_are_public_typed_and_honestly_classified(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path), surface="full")
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    suppress = tools["nx_suppress_features"].inputSchema
    assert suppress["required"] == ["features", "suppress"]
    assert suppress["additionalProperties"] is False and "operation_id" in suppress["properties"]
    reorder = tools["nx_reorder_feature"].inputSchema
    assert reorder["properties"]["position"]["enum"] == ["before", "after"]
    assert reorder["required"] == ["feature", "target", "position"]
    assert tools["nx_make_current_feature"].inputSchema.get("required", []) == []
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}
