"""Visual-material, true-shading and decal extensions of ``display_nx`` (tests/unit/display).

``visual_nx(library_root)`` is a ``display_nx`` whose part has a ``VisualMaterialCollection``
(materials copied from the installed library by exact name, assigned to objects, read back
with UF ``Disp.AskMaterial``), true-shading settings (``SHEDObjs``; a commit with the mode
on puts the work view in Studio style, as NX 2406 does) and decal stickers (``Decals``:
each decal is a visual material on its face, as NX 2406 stores it). ``make_library`` writes
a small SVM library under ``UGPHOTO/SVM/Materials``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

from tests.kit.objects import Expression, NXException, Object, matrix, point
from tests.unit.display.fakes import STYLES, View, display_nx

LIBRARY = {
    "Glasses": ["Clear Glass", "Smoked Glass"],
    "Paints": ["Brush Painted Metal Red", "Brush Painted Metal Blue", "Car Paint Black"],
    "Woods": ["Teak", "Varnished Teak"],
    "Plastics": ["ABS High Gloss White"],
}
SHADING_MATERIALS = {
    "GlobalWashShinyMetal": 0, "GlobalChrome": 15, "GlobalGold": 17, "GlobalSteel": 19,
    "GlobalMetallicPaintBlue": 28, "GlobalRubber": 29, "GlobalGlassClear": 23,
}  # fmt: skip
BACKGROUNDS = {"DarkGraduated": 0, "LightGraduated": 1, "Black": 2, "White": 3,
               "InheritShadedBackground": 6, "PureWhite": 8}  # fmt: skip
ENVIRONMENTS = {"Default": 0, "MetalShiny1": 1, "MetalShiny2": 2, "MetalBrushed1": 3,
                "MetalBrushed2": 4, "Glossy1": 5, "Glossy2": 6, "AutoPhotoStudio": 9}  # fmt: skip


def make_library(root: Path) -> Path:
    """An installation root whose SVM library holds ``LIBRARY``."""
    for category, names in LIBRARY.items():
        folder = root / "UGPHOTO" / "SVM" / "Materials" / category
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / f"{name}.svm").write_text("svm", encoding="utf-8")
            (folder / f"{name}.tif").write_text("tif", encoding="utf-8")
    return root


class Material(Object):
    def __init__(self, name, kind=0):
        super().__init__(name)
        self.Type = kind


class Materials:
    """``part.VisualMaterialCollection``: library names only, exact spelling."""

    def __init__(self, fake):
        self.fake = fake
        self.known = {n for names in LIBRARY.values() for n in names}
        self.items: dict[str, Material] = {}
        self.assigned: dict[int, Material] = {}
        self.fail_assign = None

    def CreateMaterial(self, name):
        if name not in self.items:
            if name not in self.known:
                raise NXException(925122, "Cannot rename a Composites Flat Patterns view")
            self.items[name] = Material(name)
        return self.items[name], name

    def AssignMaterial(self, material, objects):
        if self.fail_assign is not None:
            raise self.fail_assign
        for obj in objects:
            self.assigned[obj.Tag] = material
            self.fake.part.IsModified = True

    def RemoveMaterial(self, objects):
        for obj in objects:
            self.assigned.pop(obj.Tag, None)

    def DeleteMaterial(self, material):
        self.items = {k: v for k, v in self.items.items() if v is not material}
        self.assigned = {k: v for k, v in self.assigned.items() if v is not material}

    def GetMaterialsInPart(self):
        return list(self.items.values()), list(self.items)

    # UF Disp
    def ask(self, tag):
        material = self.assigned.get(tag)
        return (material.Tag, material.Name) if material is not None else (0, "")

    def geometry(self, tag):
        found = [t for t, m in self.assigned.items() if m.Tag == tag]
        return len(found), found


class ShadedView(View):
    """A work view whose style change switches true shading off (NX 2406: any other style
    does; setting Studio again alone leaves plain Studio)."""

    shed = False

    @property
    def RenderingStyle(self):
        return self.__dict__.get("_style", 0)

    @RenderingStyle.setter
    def RenderingStyle(self, value):
        if value != STYLES.Studio:
            self.shed = False
        self.__dict__["_style"] = value


class ShadingBuilder:
    """NX 2406: opening the builder switches true shading on (Studio style), and Destroy
    without a commit leaves it on; a commit with the mode off puts the earlier style back."""

    def __init__(self, fake):
        self.fake = fake
        for key, value in fake.shading.items():
            setattr(self, key, value)
        self.ShedModeToggle = True  # NX 2406: a new builder has the mode on
        self.destroyed = False
        view = fake.work_view
        if not view.shed:
            fake.style_before_shading = view.RenderingStyle
            view.RenderingStyle = STYLES.Studio
            view.shed = True

    def Commit(self):
        if self.fake.shading_error is not None:
            raise self.fake.shading_error
        for key in self.fake.shading:
            self.fake.shading[key] = getattr(self, key)
        view = self.fake.work_view
        if self.ShedModeToggle:
            view.RenderingStyle = STYLES.Studio
            view.shed = True
        else:
            view.RenderingStyle = self.fake.style_before_shading
        self.fake.part.IsModified = True
        return None  # NX 2406: no TrueShading object

    def Destroy(self):
        self.destroyed = True


class StickerBuilder:
    """``DecalStickerBuilder``: a new or edited decal (a visual material on its faces)."""

    def __init__(self, fake, decal):
        self.fake, self.decal = fake, decal
        state = fake.decals.get(decal.Tag) if decal is not None else None
        state = state or {"name": "Decal Sticker 1", "image": "default.png", "faces": [],
                          "width": 52.9, "height": 26.5, "origin": None,
                          "frame": ((1, 0, 0), (0, 1, 0), (0, 0, 1)), "center": (0, 0, 0)}  # fmt: skip
        self.DecalName = state["name"]
        self.ImageName = state["image"]
        self.FaceOrBody = NS(items=list(state["faces"]))
        self.FaceOrBody.Add = self.FaceOrBody.items.append
        self.FaceOrBody.GetArray = lambda: list(self.FaceOrBody.items)
        # NX 2406: every builder makes its own width, height and rotation expressions
        # ("pN"), kept in the part after Destroy; the decal stores plain values
        fake.sticker_expressions = getattr(fake, "sticker_expressions", 500) + 3
        made = [Expression(f"p{fake.sticker_expressions + i}", "0") for i in range(3)]
        fake.part.Expressions.extend(made)
        self.Width, self.Height, self.RotationAngle = made
        self.Width.RightHandSide = str(state["width"])
        self.Height.RightHandSide = str(state["height"])
        for exp in made:
            exp.Value = None  # read through RightHandSide until committed
        self.Origin = state["origin"]
        self.LockAspectRatio = True
        self.ProjectionType = fake.nx.Display.DecalStickerBuilder.ProjectionTypes.Planar
        self.AnchorType = self.ScalingMethodType = None
        self.frame, self.center = state["frame"], state["center"]
        self.destroyed = False

    def SetManipulatorData(self, origin, m):
        self.center = (origin.X, origin.Y, origin.Z)
        self.frame = ((m.Xx, m.Xy, m.Xz), (m.Yx, m.Yy, m.Yz), (m.Zx, m.Zy, m.Zz))

    def GetManipulatorData(self):
        return point(*self.center), matrix(*self.frame)

    def UpdateDecalStickerOnImageDimensionChange(self):
        pass

    def Commit(self):
        if self.fake.decal_error is not None:
            raise self.fake.decal_error
        width = float(self.fake.formulas.get(self.Width.RightHandSide, self.Width.RightHandSide))
        height = float(self.Height.RightHandSide)
        if self.LockAspectRatio and self.fake.image_aspect:
            height = width / self.fake.image_aspect
            self.Height.RightHandSide = repr(height)
        self.Width.Value, self.Height.Value, self.RotationAngle.Value = width, height, 0.0
        decal = self.decal or Material(self.DecalName, kind=1)
        decal.Name = decal.JournalIdentifier = self.DecalName
        materials = self.fake.part.VisualMaterialCollection
        materials.items[self.DecalName] = decal
        for face in self.FaceOrBody.items:
            materials.assigned[face.Tag] = decal
        self.fake.decals[decal.Tag] = {
            "name": self.DecalName, "image": self.ImageName, "faces": list(self.FaceOrBody.items),
            "width": width, "height": height, "origin": self.Origin, "frame": self.frame,
            "center": self.center,
        }  # fmt: skip
        self.fake.part.IsModified = True
        return None  # NX 2406: Commit returns no object

    def Destroy(self):
        self.destroyed = True


def visual_nx(units="mm"):
    nx = display_nx(units)
    nx.shading = {
        "GlobalMaterialType": 28, "BgdTypeEnum": 8, "PlanarShadowToggle": True,
        "PlanarReflectionToggle": True, "EnvironmentMapEnum": 0,
    }  # fmt: skip
    nx.shading_error = nx.decal_error = None
    nx.decals, nx.image_aspect, nx.formulas = {}, None, {}
    materials = Materials(nx)
    nx.part.VisualMaterialCollection = materials
    nx.uf.Disp.AskMaterial = materials.ask
    nx.uf.Disp.AskGeometryOfMaterial = materials.geometry
    style = nx.work_view.RenderingStyle
    nx.work_view.__class__ = ShadedView
    nx.work_view.RenderingStyle = style
    nx.style_before_shading = STYLES.ShadedWithEdges
    nx.part.SHEDObjs = NS(
        CreateTrueShadingBuilder=lambda _null: ShadingBuilder(nx),
        IsTrueShadeEnabled=lambda: nx.work_view.shed,
    )
    nx.part.Decals = NS(CreateDecalStickerBuilder=lambda decal: StickerBuilder(nx, decal))
    nx.nx.Display.TrueShading = NS(Null=None)
    nx.nx.Display.TrueShadingBuilder = NS(
        MaterialType=NS(**SHADING_MATERIALS),
        BgdType=NS(**BACKGROUNDS),
        EnvironmentMapType=NS(**ENVIRONMENTS),
    )
    nx.nx.Display.DecalStickerBuilder = NS(
        ProjectionTypes=NS(Planar=0, Cylindrical=1, Spherical=2),
        AnchorTypes=NS(MiddleCenter=4),
        ScalingMethodTypes=NS(UserDefined=1, ImageSize=0),
    )
    nx.nx.TaggedObject = NS(Null=None)
    nx.uf.Modeling.AskFaceParm = lambda tag, p: _face_parm(nx, tag, p)
    nx.uf.Modeling.AskFaceProps = lambda tag, uv: _face_props(nx, tag, uv)
    nx.uf.Modeling.AskFaceUvMinmax = lambda tag: [0.0, 1.0, 0.0, 1.0]
    nx.uf.Modeling.AskPointContainment = lambda p, tag: 1
    return nx


def _face_parm(nx, tag, p):
    """``(uv, point)``: ``p`` projected onto a planar face; uv carries the point."""
    _kind, origin, normal = nx.face_data[tag]
    d = sum((a - b) * n for a, b, n in zip(p, origin, normal, strict=True))
    on = [a - d * n for a, n in zip(p, normal, strict=True)]
    return on, on


def _face_props(nx, tag, uv):
    """A planar face's properties at ``uv``: the point (here uv itself, or the face center
    for a parameter pair) and the normal at index 5."""
    _kind, origin, normal = nx.face_data[tag]
    at = list(uv) if len(uv) == 3 else list(origin)
    return at, None, None, None, None, list(normal)
