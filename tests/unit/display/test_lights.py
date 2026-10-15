"""Scene lights (domains/display/lights.py) on the shared fake kernel, with a fake light set
shaped like the NX 2406 template part's (scene lights, standard and spare lights)."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import catalogue
from tests.kit import NXException, run
from tests.kit.objects import Object
from tests.unit.display.fakes import STYLES, display_nx

SCENE_NAMES = ["Scene Ambient", "Scene Left Top", "Scene Top", "Scene Right Top", "Scene Front",
               "Scene Left Bottom", "Scene Bottom", "Scene Right Bottom"]  # fmt: skip
SHADOW = {"NotSet": 0, "SoftEdged": 1, "HardEdged": 2}
PRESET_LIGHTS = {0: SCENE_NAMES[:2] + ["Scene Right Top"], 1: SCENE_NAMES[:4]}


class Light(Object):
    def __init__(self, name, kind, scene=-1, intensity=1.0, on=False):
        super().__init__(name)
        self.kind, self.scene, self.Intensity, self.on = kind, scene, intensity, on
        self.color, self.cone, self.shadow, self.mode = (255, 255, 255), 60.0, 0, 1
        self.location, self.to = [0.0, 0.0, 100.0], [0.0, 0.0, 0.0]

    def GetLightType(self):
        return self.kind, self.scene

    def GetEnabledInView(self, _view):
        return self.on

    def SetEnabledInView(self, _view, flag):
        self.on = bool(flag)


class LightBuilder:
    """NX 2406: placement and cone angle commit (and the cone reads) in the Studio style only."""

    def __init__(self, fake, light):
        self.fake, self.light = fake, light
        self.studio = fake.work_view.RenderingStyle == STYLES.Studio
        self.Intensity, self.ConeAngle = light.Intensity, light.cone if self.studio else 0.0
        self.LightShadowType = light.shadow
        self.SourcePosition = self.DestinationPosition = None  # NX 2406 reads None

    def Commit(self):
        if self.fake.light_error is not None:
            raise self.fake.light_error
        light = self.light
        light.Intensity, light.shadow = self.Intensity, self.LightShadowType
        if not self.studio:
            return
        light.cone = self.ConeAngle
        if self.SourcePosition is not None:
            c = self.SourcePosition.Coordinates
            light.location = [c.X, c.Y, c.Z]
        if self.DestinationPosition is not None:
            c = self.DestinationPosition.Coordinates
            light.to = [c.X, c.Y, c.Z]
        self.fake.part.IsModified = True

    def Destroy(self):
        pass


class Lighting:
    """``Display.Lighting``: a preset commit replaces the lights on."""

    def __init__(self, fake):
        self.fake = fake
        self.LightsShadedViewsLightingCollection = fake.preset

    def Commit(self):
        self.fake.preset = self.LightsShadedViewsLightingCollection
        names = PRESET_LIGHTS.get(self.fake.preset, SCENE_NAMES[:1])
        for light in self.fake.part.Lights:
            light.on = light.Name in names

    def Destroy(self):
        pass


def lights_nx():
    nx = display_nx()
    lights = [Light(n, 0 if i == 0 else 6, i, 0.3, on=n in PRESET_LIGHTS[0])
              for i, n in enumerate(SCENE_NAMES)]  # fmt: skip
    lights += [Light("Standard Eye", 2), Light("Standard Z Spot", 4), Light("Point 1", 3, -1, 0.75),
               Light("Distant 1", 1, -1, 0.5)]  # fmt: skip
    nx.part.Lights = lights
    nx.preset, nx.light_error = 0, None
    view = nx.work_view
    view.Tag = 900
    view.Regenerate = lambda: None
    view.UseSceneLights = lambda: [setattr(x, "on", x.Name in SCENE_NAMES) for x in lights]
    view.UseDefaultLights = lambda: [setattr(x, "on", x.Name in SCENE_NAMES[:2]) for x in lights]
    nx.part.Views.CreateLightBuilder = lambda light: LightBuilder(nx, light)
    nx.part.Views.CreateLighting = lambda _view: Lighting(nx)

    def ask(_tag):
        on = [x for x in lights if x.on]
        rows = [NS(LightName=x.Name, LightType=x.kind, LightMode=x.mode, Location=x.location,
                   To=x.to) for x in on]  # fmt: skip
        return NS(LightCount=len(rows), Lights=rows + [NS()] * (8 - len(rows)))

    def edit(name, a):
        light = next(x for x in lights if x.Name == name)
        light.color, light.Intensity = (a.Red, a.Green, a.Blue), a.Intensity
        light.shadow = 2 if a.GenerateShadows else 0  # NX 2406: True gives hard shadows

    nx.uf.View = NS(
        AskViewLight=ask,
        EditViewLight=edit,
        LightAttributes=lambda: NS(
            Red=0, Green=0, Blue=0, Intensity=0.0, IntensityFactor=None, GenerateShadows=False
        ),  # fmt: skip
        LightIntensityFactor=NS(INTENSITY_FACTOR_NONE=0),
    )
    nx.nx.Display.LightBuilder = NS(ShadowType=NS(**SHADOW))
    nx.nx.Display.Lighting = NS(
        LightingCollectionType=NS(**{f"Lighting{i}": i - 1 for i in range(1, 6)}, UserDefined=5)
    )
    return nx


def light(nx, name):
    return next(x for x in nx.part.Lights if x.Name == name)


def test_list_reports_types_scene_positions_and_placement():
    nx = lights_nx()
    light(nx, "Standard Z Spot").on = True
    out = run(nx, "nx_list_lights")
    assert out.ok, out.error
    rows = {r["name"]: r for r in out.data["lights"]}
    assert out.data["count"] == 12 and out.data["preset"] == "lighting_1"
    assert out.data["on"] == ["Scene Ambient", "Scene Left Top", "Scene Right Top",
                              "Standard Z Spot"]  # fmt: skip
    assert rows["Scene Ambient"]["type"] == "scene" and rows["Scene Ambient"]["scene"] == "ambient"
    assert rows["Scene Top"] == {"name": "Scene Top", "type": "scene", "on": False,
                                 "intensity": 0.3, "scene": "top"}  # fmt: skip
    spot = rows["Standard Z Spot"]
    assert spot["position"] == [0.0, 0.0, 100.0] and spot["target"] == [0.0, 0.0, 0.0]
    assert spot["direction"] == [0.0, 0.0, -1.0] and spot["fixed_to"] == "part"
    assert spot["shadows"] is False and "cone_angle" not in spot  # shaded view: unreadable
    nx.work_view.RenderingStyle = STYLES.Studio
    studio = run(nx, "nx_list_lights", on_only=True).data["lights"]
    assert next(r for r in studio if r["name"] == "Standard Z Spot")["cone_angle"] == 60.0
    assert "position" not in rows["Distant 1"]  # off: NX lists no placement
    only = run(nx, "nx_list_lights", on_only=True)
    assert only.data["count"] == 4


def test_list_converts_an_observer_fixed_direction_to_model_coordinates():
    nx = lights_nx()
    distant = light(nx, "Distant 1")
    distant.on, distant.mode = True, 0
    distant.location, distant.to = [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]  # toward the screen
    row = next(r for r in run(nx, "nx_list_lights").data["lights"] if r["name"] == "Distant 1")
    view_z = [nx.work_view.Matrix.Zx, nx.work_view.Matrix.Zy, nx.work_view.Matrix.Zz]
    assert row["fixed_to"] == "view"
    assert row["direction"] == pytest.approx([-v for v in view_z])
    assert "position" not in row


def test_set_turns_on_aims_colours_and_reads_back_a_distant_light():
    nx = lights_nx()
    out = run(nx, "nx_set_light", name="distant 1", on=True, direction=[0, 0, -2],
              color=[255, 0, 0], intensity=0.9, shadows=True)  # fmt: skip
    assert out.ok, out.error
    assert out.mutation_outcome == "committed"
    row = out.data["light"]
    assert row["on"] is True and row["intensity"] == 0.9 and row["shadows"] is True
    assert row["direction"] == [0.0, 0.0, -1.0]
    assert out.data["color"] == [1.0, 0.0, 0.0] and out.data["marks_part_modified"] is True
    distant = light(nx, "Distant 1")
    assert distant.color == (255, 0, 0) and distant.location == [0.0, 0.0, 100.0]
    recolour = run(nx, "nx_set_light", name="Distant 1", color=[0, 0, 1])
    assert recolour.ok and recolour.data["light"]["shadows"] is True  # put back after the UF edit
    assert distant.color == (0, 0, 255) and distant.shadow == 1
    off = run(nx, "nx_set_light", name="Distant 1", on=False)
    assert off.ok and off.data["light"]["on"] is False and "direction" not in off.data["light"]


def test_set_places_a_spot_and_keeps_its_target():
    nx = lights_nx()
    spot = run(nx, "nx_set_light", name="Standard Z Spot", on=True, position=[200, -100, 300],
               target=[50, 30, 10], cone_angle=30)  # fmt: skip
    assert spot.ok, spot.error
    row = spot.data["light"]
    assert row["position"] == [200.0, -100.0, 300.0] and row["target"] == [50.0, 30.0, 10.0]
    assert row["cone_angle"] == 30.0 and light(nx, "Standard Z Spot").cone == 30.0
    assert nx.work_view.RenderingStyle == 0  # held in Studio for the builder, then put back
    moved = run(nx, "nx_set_light", name="Standard Z Spot", position=[0, 0, 500])
    assert moved.ok and moved.data["light"]["target"] == [50.0, 30.0, 10.0]
    point = run(nx, "nx_set_light", name="Point 1", on=True, position=[1, 2, 3])
    assert point.ok and point.data["light"]["position"] == [1.0, 2.0, 3.0]
    assert "direction" not in point.data["light"]


def test_scene_lights_take_on_and_intensity_only():
    nx = lights_nx()
    top = run(nx, "nx_set_light", name="Scene Top", on=True, intensity=0.8)
    assert top.ok and top.data["light"]["on"] is True and top.data["light"]["intensity"] == 0.8
    colour = run(nx, "nx_set_light", name="Scene Top", color=[1, 0, 0])
    assert colour.error["code"] == "NX_INVALID_ARGUMENT" and colour.error["field"] == "/color"
    assert colour.mutation_outcome == "not_started"
    bright = run(nx, "nx_set_light", name="Scene Top", intensity=2)
    assert bright.error["field"] == "/intensity"
    eye = run(nx, "nx_set_light", name="Standard Eye", on=True, color=[0, 0.5, 1])
    assert eye.ok and light(nx, "Standard Eye").color == (0, 128, 255)


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"name": "Distant 1", "direction": [0, 0, -1]}, "/on"),  # off: cannot place
        ({"name": "Distant 1", "on": True, "position": [0, 0, 1]}, "/position"),
        ({"name": "Point 1", "on": True, "direction": [0, 0, 1]}, "/direction"),
        ({"name": "Standard Eye", "shadows": True}, "/shadows"),
        ({"name": "Point 1", "on": True, "target": [0, 0, 0]}, "/target"),
        ({"name": "Point 1", "cone_angle": 20}, "/cone_angle"),
        ({"name": "Distant 1"}, "/on"),
        ({"name": "Distant 1", "color": [300, 0, 0]}, "/color"),
        (
            {"name": "Standard Z Spot", "on": True, "position": [0, 0, 0], "target": [0, 0, 0]},
            "/target",
        ),
    ],
)
def test_set_refuses_settings_the_light_does_not_take(args, field):
    out = run(lights_nx(), "nx_set_light", **args)
    assert out.error["code"] == "NX_INVALID_ARGUMENT", out.error
    assert out.error["field"] == field and out.mutation_outcome == "not_started"


def test_unknown_light_lists_close_names():
    out = run(lights_nx(), "nx_set_light", name="Distant 3", on=True)
    assert out.error["code"] == "NX_NOT_FOUND" and "Distant 1" in out.error["details"]["close"]


def test_a_failed_builder_rolls_back():
    nx = lights_nx()
    nx.light_error = NXException(3, "boom")
    out = run(nx, "nx_set_light", name="Distant 1", on=True, intensity=0.4)
    assert out.error["code"] == "NX_BUILD_FAILED" and out.mutation_outcome == "rolled_back"


def test_presets_replace_the_lights_on():
    nx = lights_nx()
    light(nx, "Distant 1").on = True
    two = run(nx, "nx_set_lighting_preset", preset="lighting_2")
    assert two.ok, two.error
    assert two.data["on"] == SCENE_NAMES[:4] and nx.preset == 1
    scene = run(nx, "nx_set_lighting_preset", preset="scene")
    assert scene.ok and scene.data["on"] == SCENE_NAMES
    default = run(nx, "nx_set_lighting_preset", preset="default")
    assert default.ok and default.data["on"] == SCENE_NAMES[:2]
    assert run(nx, "nx_list_lights").data["preset"] == "lighting_2"


def test_list_shows_a_part_that_is_not_displayed():
    """A part in the background reads no light of its own: the listing shows it for the
    read (gate 4s, display), as nx_get_camera does."""
    spec = catalogue.get("nx_list_lights")
    assert spec.target == "any" and spec.switch_for({}, services=object(), part=None) == "display"


def test_a_colour_with_an_intensity_above_one_keeps_the_intensity():
    """NX 2406: UF EditViewLight refuses an intensity above 1, so the colour goes in at 1
    and the light builder then sets the real intensity."""
    nx = lights_nx()
    edit = nx.uf.View.EditViewLight

    def strict(name, attributes):
        if attributes.Intensity > 1.0:
            raise NXException(1105005, "The second parameter passed in was invalid.")
        edit(name, attributes)

    nx.uf.View.EditViewLight = strict
    out = run(nx, "nx_set_light", name="Distant 1", on=True, color=[255, 0, 0], intensity=2)
    assert out.ok, out.error
    distant = light(nx, "Distant 1")
    assert distant.color == (255, 0, 0) and distant.Intensity == 2.0
    again = run(nx, "nx_set_light", name="Distant 1", color=[0, 255, 0])
    assert again.ok and distant.color == (0, 255, 0) and again.data["light"]["intensity"] == 2.0


def test_compensation_sets_a_light_back():
    nx = lights_nx()
    done = run(nx, "nx_set_light", name="Point 1", on=True, intensity=3, position=[1, 2, 3])
    assert done.ok, done.error
    undo = run(nx, "nx_undo_operation", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    point = light(nx, "Point 1")
    assert point.on is False and point.Intensity == 0.75
    assert undo.data["restored"][0].startswith("light Point 1 back to on=False")


def test_compensation_refuses_a_colour_and_a_light_changed_since():
    nx = lights_nx()
    coloured = run(nx, "nx_set_light", name="Distant 1", on=True, color=[0, 0, 255])
    assert coloured.ok
    refused = run(nx, "nx_undo_operation", mode="compensate")
    assert refused.error["code"] == "NX_UNSUPPORTED_OPTION", refused
    assert "colour" in refused.error["message"] and light(nx, "Distant 1").on is True
    nx2 = lights_nx()
    first = run(nx2, "nx_set_light", name="Point 1", on=True, intensity=2)
    assert first.ok and run(nx2, "nx_set_light", name="Point 1", intensity=4).ok
    later = run(nx2, "nx_undo_operation", operation=first.operation_id, mode="compensate")
    assert later.error["code"] == "NX_DEPENDENCY_ORDER", later
    assert light(nx2, "Point 1").Intensity == 4.0


def test_compensation_switches_the_previous_lights_back_on():
    nx = lights_nx()
    before = run(nx, "nx_list_lights").data["on"]
    scene = run(nx, "nx_set_lighting_preset", preset="scene")
    assert scene.ok and scene.data["previous"] == "lighting_1"
    undo = run(nx, "nx_undo_operation", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert run(nx, "nx_list_lights").data["on"] == before
