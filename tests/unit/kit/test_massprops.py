"""kit.massprops: mass combination, reference frames, poses, UF Weight assertions,
component groups and the license check (no NX)."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import massprops as mp
from tests.kit.objects import Object, matrix, point


def test_combine_moves_each_inertia_to_the_total_centre():
    a = mp.entry(2.0, [0, 0, 0])
    b = mp.entry(2.0, [10, 0, 0], [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    total = mp.combine([a, b])
    assert total["mass"] == 4 and total["center"] == [5, 0, 0]
    t = total["inertia"]
    assert t[0][0] == pytest.approx(1)  # no arm along x
    assert t[1][1] == pytest.approx(1 + 2 * 25 * 2) and t[2][2] == pytest.approx(101)
    assert t[0][1] == 0 and mp.combine([])["center"] is None
    off = mp.combine([mp.entry(1, [1, 1, 0]), mp.entry(1, [-1, -1, 0])])
    assert off["inertia"][0][1] == pytest.approx(-2)  # products: -sum m x y


def test_frame_axes_coordinates_and_inertia_about_its_origin():
    ref = mp.frame([100, 0, 0], x_axis=[-1, 0, 0], y_axis=[0.3, 1, 0])
    assert ref["axes"][0] == [-1, 0, 0] and ref["axes"][1] == pytest.approx([0, 1, 0])
    assert ref["axes"][2] == pytest.approx([0, 0, -1])  # right-handed
    assert mp.in_frame(ref, [90, 2, 3]) == pytest.approx([10, 2, -3])
    assert mp.direction_in_frame(ref, [1, 0, 0]) == pytest.approx([-1, 0, 0])
    total = mp.combine([mp.entry(2.0, [90, 0, 0])])
    about = mp.inertia_about(total, ref)
    assert about[0][0] == pytest.approx(0) and about[1][1] == pytest.approx(200)
    with pytest.raises(NXError) as bad:
        mp.frame([0, 0, 0], x_axis=[1, 0, 0], y_axis=[2, 0, 0])
    assert bad.value.field == "/reference/y_axis"


def test_principal_moments_of_a_rotated_tensor():
    assert mp.principal_moments([[3, 0, 0], [0, 1, 0], [0, 0, 2]]) == [1, 2, 3]
    r = [[0.6, -0.8, 0], [0.8, 0.6, 0], [0, 0, 1]]
    turned = mp.rotate_inertia([[1, 0, 0], [0, 4, 0], [0, 0, 9]], r)
    assert turned[0][1] != 0
    assert mp.principal_moments(turned) == pytest.approx([1, 4, 9])


def test_pose_round_trip():
    c = NS(GetPosition=lambda: (point(10, 0, 0), matrix((0, 1, 0), (-1, 0, 0), (0, 0, 1))))
    pose = mp.pose_of(c)
    assert mp.to_assembly(pose, [1, 0, 0]) == pytest.approx([10, 1, 0])
    assert mp.to_local(pose, [10, 1, 0]) == pytest.approx([1, 0, 0])


class _Weight:
    """UF Weight over a dict: assertions per tag (millimetres), states as ints."""

    def __init__(self):
        self.data = {}
        self.calls = []

    def AskProps(self, tag, units):
        mass, center = self.data.get(tag, (None, None))
        return NS(
            Mass=mass or 0.0, MassState=2 if mass is not None else 0,
            CenterOfMass=center or [0, 0, 0], CofmState=2 if center is not None else 0,
        )  # fmt: skip

    def AssertCompProps(self, tag, props):
        self.calls.append(("comp", tag, props.Accuracy, props.CacheState))
        self.data[tag] = (props.Mass, props.CenterOfMass if props.CofmState == 2 else None)

    AssertPartProps = AssertCompProps

    def DeleteCompAssertion(self, tag):
        self.data.pop(tag, None)

    DeletePartAssertion = DeleteCompAssertion


def _tx(weight=None, present=True):
    enums = NS(StateType=NS(ASSERTED=2, UNKNOWN=3), UnitsType=NS(UNITS_KMM=6))
    return NS(
        uf=NS(Weight=weight) if weight else NS(),
        nx=NS(UF=NS(Weight=enums)),
        session=NS(LicenseManager=NS(CheckPresence=lambda name: present)),
    )


def test_assert_read_and_clear_weights():
    weight = _Weight()
    tx = _tx(weight)
    obj = Object("ENGINE")
    assert mp.asserted(tx, obj) is None
    mp.assert_weight(tx, obj, "occurrence", 350, [1, 2, 3])
    assert mp.asserted(tx, obj) == {"mass": 350, "center": [1, 2, 3]}
    assert weight.calls[-1][2:] == (mp.ASSERT_ACCURACY, 2)
    mp.assert_weight(tx, obj, "part", 60)
    assert mp.asserted(tx, obj) == {"mass": 60, "center": None}
    mp.clear_weight(tx, obj, "part")
    assert mp.asserted(tx, obj) is None
    assert mp.asserted(_tx(), obj) is None  # no UF Weight: nothing asserted


def test_license_check_refuses_before_any_change():
    mp.require_license(_tx(present=True))
    with pytest.raises(NXError) as missing:
        mp.require_license(_tx(present=False), what="Groups")
    assert missing.value.code == "NX_LICENSE_UNAVAILABLE"
    mp.require_license(NS(session=NS()))  # no license manager: NX decides


def test_groups_skip_system_groups_and_the_root():
    root = NS(Parent=None, Tag=1)
    a = NS(Parent=root, Tag=2)
    system = NS(Name="AllComponents", GetComponents=lambda: [root, a])
    mine = NS(Name="Propulsion", GetComponents=lambda: [a])
    part = NS(ComponentGroups=[system, mine])
    assert [(g.Name, m) for g, m in mp.groups(part)] == [("Propulsion", [a])]
    assert len(mp.groups(part, include_system=True)) == 2
    assert mp.find_group(part, "propulsion") is mine and mp.find_group(part, "x") is None
    assert mp.groups(NS()) == []


def test_material_name_of_occurrences_and_bare_bodies():
    material = NS(Name="STEEL")
    proto = NS(Tag=5, OwningPart=NS(MaterialManager=NS(PhysicalMaterials=NS(
        AskMaterialOfObject=lambda obj: material if obj.Tag == 5 else None))))  # fmt: skip
    assert mp.material_name(NS(IsOccurrence=True, Prototype=proto)) == "STEEL"
    assert mp.material_name(NS(IsOccurrence=False, Tag=6, OwningPart=proto.OwningPart)) is None
    assert mp.material_name(NS(IsOccurrence=True)) is None
