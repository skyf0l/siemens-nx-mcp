# NX 2406 capability gap analysis

Date: 2026-10-09. Scope: compares the 227 tools of the current MCP surface
(`capability_manifest.json` merged with `capability_fragments/*.json`, all
`status: tested`) against the NXOpen API installed with NX 2406 and against
what a mechanical engineer does in NX day to day. Nothing was implemented.

## How the evidence was collected

- **MCP inventory.** Read from `nx_mcp.manifest.load_manifest()`. Each tool's
  description and input schema came from the sidecar's FastMCP `list_tools()`.
  Scope limits come from the tool docstrings and from the manifest `scope`
  strings.
- **NX 2406 API.** Live `dir()` introspection was not possible. The live
  bridge refused connections (`NX_BRIDGE_UNAVAILABLE` / WinError 1225) in every
  locked session. The orchestrator then reported NX blocked by a modal "Work in
  Progress" dialog and asked for offline work. Instead, every `Create*Builder`,
  `Create*Creator` and `Create*Importer` symbol name was extracted from the
  installed binaries in `C:\Program Files\Siemens\NX2406\NXBIN\python\NXOpen*.pyd`.
  That gives 3,533 Create-builder symbols across 97 modules. A builder marked
  **[bin]** below is confirmed present in the 2406 binaries but **not confirmed
  live**. The owning collection (for example `part.Features.CreateRibBuilder`)
  is inferred from NXOpen conventions. Confirm it with `dev_python.py` before
  implementing.
- **Licensing.** Licensing was **not** verified, because a live call would
  have been needed (`session.LicenseManager`). Application folders present on
  disk (list below) show what is installed, not what is licensed.

## Findings about this NX installation

- **Installed application folders:** MACH (CAM), SIMULATION and NXCAE_EXTRAS
  (Simcenter), ROUTING, MOLDWIZARD, MOLDCOOLING, STAMPING_TOOLS, UGWELD,
  NXSTRUCTUREDESIGN, MBD, CMM_INSPECTION, UNFOLD, COMPOSITES, PLASTIC_DESIGNER,
  NX_VSA, DESIGNSPACEEXPLORER, NXPLOT, UGPHOTO and RULE (Knowledge Fusion).
- **Translators on disk:** STEP 203/214/242, IGES, DXFDWG, PVTRANS (JT),
  CATIAV5/UGCATIA, NXCREO, VDA, plus `TRANSLATORS/{3dxml, g3d, ifc, nxacis,
  revit, rhino, solidworks, step242, wavefrontobj}`.
- **Exporters in the API** (`NXOpen.DexManager` family): Step, Iges, Stl, Jt,
  Dxfdwg, Parasolid, Acis, Catiav4/v5, Ifc, WavefrontObj, Nxto2d, QuickUsdz,
  ExtendedRealityFile. **No 3MF creator exists**, so STL is the API's route to
  additive manufacturing.
- **Importers in the API:** Step203/214/242, Iges, Stl, Parasolid, Acis,
  Dxfdwg, Catiav4/v5, Proe, Creo, SolidEdge, Rhino, Revit, Ifc, Threedxml, Vrml,
  WavefrontObj, G3d, Cgm, Asc.
- **Surprises:**
  - NXOpen has no break-out section builder. "Breakout" appears only in the UF
    binary (`UF_DRAW`), so a break-out view needs the UF route. This is the same
    situation as `nx_add_ordinate_dimensions`, which had to use UF because
    `OrdinateDimensionBuilder` does not commit on 2406.
  - `NXOpen.Implicit` (implicit modeling: Block, Unite, Subtract, Thicken) is
    present.
  - `NXOpen.Validate` contains additive-manufacturing checks (minimum wall,
    overhang, printable volume, trapped supports), `RequirementBuilder` and a
    Check-Mate-style `ValidationManager`.
  - Some modules imply automation-friendly commands:
    `Drafting.CreateDrawingCreationWizardBuilder`,
    `Drawings.CreateStandardViewsBuilder`,
    `Annotations.CreateRapidDimensionBuilder` (auto-dimensioning) and
    `Drawings.CreateCompareReportBuilder`.
  - Other notable APIs: `NXOpen.CreateLinkedMirrorPartBuilder` (mirrored part),
    `Assemblies.CreateReflectComponentBuilder`, `Assemblies.CreateDeformBuilder`
    and `ShapeSearch` / `Sustainability` modules.
- **Builders the MCP already uses (about 80)** are listed in the source. Every
  `Create*Builder` cited below is not used by any production module today.

## What the MCP covers today

The coverage is strong in these areas:

- Sketching: 30 tools, with modern-solver constraints and dimensions.
- Core features: extrude, revolve, sweep, loft, blend, chamfer, variable blend,
  shell, draft and holes.
- Threads.
- Body operations: boolean, split, trim, mirror, scale, transform.
- Sheet metal: 34 native operations, flat pattern, bend tables, DXF and GEO
  export.
- Assembly placement, constraints, patterns, explosions and reference sets.
- Clearance and interference checks, mass and measurements.
- Basic drafting: base, projected, detail and simple section views; linear,
  angular, radial and ordinate dimensions; notes, centerlines, surface finish,
  weld symbols, BOM, balloons, title-block and revision tables; PDF export.
- PMI datum and feature-control frame (FCF).
- STEP import and export.

Scope limits that matter for the gaps:

| Tool | Stated limit |
|---|---|
| `nx_pattern` | Linear only, along principal axis ±X/±Y/±Z (`nx_circular_pattern` covers circular) |
| `nx_edit_feature` | Extrude distance and linear-pattern count/pitch only (`nx_set_feature_parameters` covers expressions generically) |
| `nx_mirror_body(_about)` | Body mirror only; no feature mirror |
| `nx_edit_faces` | Move/offset/replace/delete-heal; no resize face/blend/chamfer, no pattern/mirror face |
| `nx_set_material` | Density-only custom material; no NX material-library assignment (E, ν, thermal) |
| `nx_import_geometry` / `nx_export_step` | STEP only; no AP or translator option choice |
| `nx_add_projection_view` | Orthographic right/left/top/bottom only; no auxiliary (hinge-line) view |
| `nx_add_section_drawing_view` | Simple one-segment section; no stepped/half/revolved/break-out section |
| `nx_edit_dimension_format` | none/bilateral/symmetric/limits/basic/reference; no ISO limits-and-fits (H7/g6) |
| `nx_pmi_datum` / `nx_pmi_fcf` | 3D PMI only; no drafting-view GD&T, no PMI dimensions/notes/model views |
| `nx_assembly_constraint` | fix, touch, distance, parallel, perpendicular, angle, concentric; no align/lock, fit, bond, center, gear |
| `nx_drawing_table` | Creates title-block/revision tables; no attribute-linked filling |
| `nx_create_part` | mm/inch at creation; no unit conversion, no template |

Entirely absent: object attributes, WAVE and inter-part links, feature
mirror/advanced patterns, rib/emboss/tube/helix, 3D curve operations,
non-STEP translators, hole callouts and hole tables, drawing-view GD&T,
broken/auxiliary views, expression import/export and part families,
Check-Mate validation, and any CAE, CAM, routing or mold work.

## Recommended next wave (top 15, ranked)

Value: H = used weekly by most mechanical engineers or a blocker for a release
package; M = common but role-specific; L = niche. Difficulty reflects the
project's live-verification bar, not just API wiring.

| # | Capability | Value | Difficulty | NXOpen route (2406) | Dependencies |
|---|---|---|---|---|---|
| 1 | **Attributes:** read/write part, body, component and object attributes (string, number, integer, date, boolean), with categories and locks | **H**: part number, description, material, revision and mass feed title blocks, BOMs and PLM; no attribute tool exists today | Easy | `NXObject.SetUserAttribute/GetUserAttributes/DeleteUserAttribute` [bin]; `NXOpen.CreateAttributePropertiesBuilder` [bin] for multi-object edits | None; enables 9 and BOM columns |
| 2 | **Export formats:** STL (for 3D printing and quotes), IGES, Parasolid x_t, JT, DXF/DWG of drawings, STEP AP242 option | **H**: suppliers, quoting, 3D printing and 2D DWG hand-off are daily tasks; only STEP, PDF and planar DXF exist today | Easy–Med | `DexManager.CreateStlCreator`, `CreateIgesCreator`, `CreateParasolidExporter`, `CreateJtCreator`, `CreateDxfdwgCreator`, `CreateStepCreator` [bin] | Workspace validation (exists); licenses for JT and DXFDWG; reuse `nx_export_step` staging |
| 3 | **Import formats:** IGES, Parasolid, STL (as facets), DXF/DWG into a sketch or drawing, STEP242, and SolidWorks/Creo/CATIA when licensed | **H**: legacy supplier data arrives in every format | Med | `CreateIgesImporter`, `CreateParasolidImporter`, `CreateStlImporter`, `CreateDxfdwgImporter`, `CreateStep242Importer`, `CreateSolidEdgeImporter`, `CreateCreoImporter`, `CreateCatiav5Importer` [bin] | Pattern of `nx_import_geometry`; license detection |
| 4 | **Material library assignment:** assign NX library materials (Steel, Aluminum_6061, ...) and read E, ν, density and thermal properties | **H**: mass and CAE are wrong without real materials; today only a density-only custom material exists | Easy | `PhysicalMaterialLibrary` / `part.MaterialManager.PhysicalMaterials.LoadFromNxmatmllibrary` + `PhysicalMaterial.AssignObjects` (core NXOpen, `NXOpen_PhysMat` present) | `nx_material_info` exists for readback |
| 5 | **Feature mirror and advanced patterns:** Mirror Feature, pattern along curve, fill, polygon, spiral, general and reference layouts, mirror pattern, and direction from an edge or datum (not just ±X/Y/Z) | **H**: mirroring and patterning features is constant in part modeling; only body mirror and principal-axis linear patterns exist | Med | `Features.CreateMirrorBuilder`, `CreatePatternFeatureBuilder` [bin] (`PatternService` layouts; "PatternFill" and "Spiral" symbols present in Features/GeometricUtilities) | Builds on `nx_pattern` / `nx_circular_pattern` code |
| 6 | **Hole callouts and hole table on drawings** | **H**: every machined-part drawing needs them; `nx_hole_feature` and `nx_standard_thread` already create the source features | Med | `Annotations.CreateHoleCalloutDimensionBuilder`, `CreateHoleTableBuilder` [bin] | `nx_hole_feature`, drawing views |
| 7 | **GD&T in drawing views:** datum feature symbol, FCF and datum targets attached to view geometry | **H**: 2D release drawings still dominate; GD&T exists only as 3D PMI today | Med | `Annotations.CreateDraftingDatumFeatureSymbolBuilder`, `CreateDraftingFeatureControlFrameBuilder`, `CreateDraftingDatumTargetBuilder` [bin] | Reuse `pmi_fcf` validation and modifiers |
| 8 | **More drawing dimension types and fit tolerances:** chamfer, thickness, arc/curve length, baseline/chain sets, ISO limits-and-fits (H7/g6) on existing dimensions | **H**: chamfer and fit callouts are on nearly every drawing | Med | `Annotations.CreateChamferDimensionBuilder`, `CreateThicknessDimensionBuilder`, `CreateCurveLengthDimensionBuilder` [bin]; fit types through `DimensionStyleBuilder` tolerance (verify enum live) | `nx_add_dimension`, `nx_edit_dimension_format` |
| 9 | **Title block filling and drawing templates:** populate title-block cells from part attributes and edit cell values | **H**: every drawing release needs it; `nx_drawing_table` creates the table but cannot fill it from attributes | Easy–Med | `Annotations.CreateEditTitleBlockBuilder`, `CreateDefineTitleBlockBuilder` (in use) [bin]; attribute references `<W$=@ATTR>` | Item 1 |
| 10 | **More drawing views:** auxiliary (hinge line), broken view, half/stepped/revolved section, break-out section | **H/M**: long shafts and frames need broken views; angled faces need auxiliary views | Med–Hard | `Drawings.CreateProjectedViewBuilder` (hinge vector), `CreateBrokenViewBuilder`, `CreateSectionViewBuilder` + `CreateSectionLineSegmentBuilder` [bin]; break-out only via `UF_DRAW` (no NXOpen builder found) | Existing view tools |
| 11 | **Synchronous edits for imported parts:** resize face (hole or boss diameter), resize blend, resize chamfer, pattern face, mirror face, delete blend chains | **H**: editing dumb STEP geometry is a top use of NX; move/offset/replace/delete already exist | Med | `Features.CreateResizeFaceBuilder`, `CreateResizeBlendBuilder`, `CreateResizeChamferBuilder`, `CreatePatternFaceBuilder`, `CreateMirrorFaceBuilder`, `CreateLabelChamferBuilder` [bin] | `nx_edit_faces`, `nx_recognize_holes` |
| 12 | **Rib/web, tube, helix and emboss** | **M/H**: ribs on castings and plastics, tubes and pipes for frames and harness clearance, helix for springs | Med (tube and helix easy) | `Features.CreateRibBuilder`, `CreateTubeBuilder`, `CreateHelixBuilder`, `CreateEmbossBodyBuilder` [bin] | Sketch/curve IDs |
| 13 | **Assembly completeness:** align/lock, fit, bond, center and distance-to-edge constraints; create a new component in context; make unique; mirror assembly | **H**: top-down design and fixing under-constrained assemblies; only 7 constraint types today | Med | `Positioning` ComponentConstraint types (via `Assemblies.CreatePositioningTaskBuilder` / existing constraint path), `Assemblies.CreateNewComponentBuilder`, `CreateMakeUniquePartBuilder`, `CreateReflectComponentBuilder` [bin] | `nx_assembly_constraint` |
| 14 | **WAVE link and extract geometry:** link body, face, curve, datum or sketch from another component into the work part; extract body or face within a part | **H**: core of NX inter-part, top-down design (for example a cover that follows a housing) | Med–Hard | `Features.CreateWaveLinkBuilder`, `CreateWaveSketchBuilder`, `CreateWaveDatumBuilder`, `CreateExtractFaceBuilder` (used internally by `nx_transform_bodies`) [bin] | Work-part switching in assemblies; item 13 |
| 15 | **Expression import/export (.exp) and a bulk design table** | **M/H**: parametric families and supplier variants are driven from spreadsheets | Easy (.exp) | `part.Expressions.ImportFromFile/ExportToFile` (core NXOpen symbols) [bin] | `nx_set_expression`, workspace validation |

Suggested order inside the wave:

1. Items 1, 2, 4 and 15. These are easy, high-value, mostly read/write
   tools with little geometry risk.
2. Items 3, 9, 6 and 8. These build on item 1 and the existing drafting code.
3. Items 5, 11, 12 and 7.
4. Items 13, 10 and 14. These are the hardest to verify live (multi-part
   context, UF fallback).

## Backlog

| Capability | Value | Difficulty | Route [bin] | Note |
|---|---|---|---|---|
| Model validation (Check-Mate-like rules: unconstrained sketches, unparameterized features, missing material or attributes) | M | Med | `NXOpen.ValidationManager`, `Validate.CreateRequirementBuilder`, `CreateRequirementCheck` | `nx_model_health` covers feature errors only |
| Draft analysis, minimum-radius and AM printability checks | M | Med | `GeometricAnalysis.CreateDraftAnalysisBuilder`, `Validate.CreateCheckMinimumRadiusBuilder`, `CreateCheckMinimumWallThicknessBuilder`, `CreateCheckMaximumOverhangAngleBuilder` | `nx_face_analysis` samples draft only |
| Deviation gauge (scan or import vs. CAD) | M | Med | `GeometricAnalysis.CreateDeviationGaugeBuilder` | Inspection use |
| 3D PMI completeness: PMI linear/radial/angular dimensions, notes, surface finish, thickness, model views | M | Med | `Annotations.CreatePmiLinearDimensionBuilder`, `CreatePmiRadialDimensionBuilder`, `CreatePmiNoteBuilder`, `CreatePmiSurfaceFinishSymbolBuilder`, `CreatePmiThicknessDimensionBuilder` | High for MBD shops |
| Auto-dimension and standard views wizard | M | Med | `Annotations.CreateRapidDimensionBuilder`, `Drawings.CreateStandardViewsBuilder`, `Drafting.CreateDrawingCreationWizardBuilder` | Speeds up drawing creation |
| Primitive solids (block, cylinder, cone, sphere) | M | Easy | `Features.CreateBlockFeatureBuilder`, `CreateCylinderBuilder` (used by `nx_hole`), `CreateConeBuilder`, `CreateSphereBuilder` | Convenience for fixtures and stock |
| Sketch completeness: spline, point, sketch pattern, text, conic | M | Easy–Med | Sketch curve APIs; `Features.CreateTextBuilder` | |
| 3D curves: offset, project, composite, intersection curve, divide curve | M | Med | `Features.CreateOffsetCurveBuilder`, `CreateProjectCurveBuilder`, `CreateCompositeCurveBuilder`, `CreateIntersectionCurveBuilder`, `CreateDivideCurveBuilder` | Needed for sweeps, tubes and routing paths |
| Surfacing: offset surface, extension, N-sided, studio surface, ruled, bounded plane, patch, untrim, styled sweep | M | Med | `Features.CreateOffsetSurfaceBuilder`, `CreateExtensionBuilder`, `CreateNSidedSurfaceBuilder`, `CreateStudioSurfaceBuilder`, `CreateRuledBuilder`, `CreateBoundedPlaneBuilder`, `CreatePatchBuilder`, `CreateStyledSweepBuilder` | Industrial-design users |
| Face blend, delete body, remove parameters, divide face | L/M | Easy–Med | `CreateFaceBlendBuilder`, `CreateDeleteBodyBuilder`, `CreateRemoveParametersBuilder`, `CreateDividefaceBuilder` | |
| Assembly cut, deformable parts, arrangements, simplify/wrap assembly | M | Hard | `Features.CreateAssemblyCutBuilder`, `Assemblies.CreateDeformBuilder`, `ArrangementCollection`, `Assemblies.CreateSimplifyBuilder`, `Features.CreateWrapGeometryBuilder` | Simplify/wrap is useful for envelope export |
| Part families and spreadsheet-driven design | M | Hard | `NXOpen.PartFamily` module, `Annotations.CreatePartFamilyTableBuilder` | Prefer .exp import first (item 15) |
| Feature groups, UDFs, Knowledge Fusion rules | L | Hard | `NXOpen.UserDefinedTemplate`, `RULE` | |
| Unit conversion of a part, template-based part creation | L | Easy–Med | `UF` convert-units symbols | |
| Weld features (beads, fillet, groove, weld points) beyond drafting symbols | M (fabrication) | Med | `NXOpen.Weld.CreateFilletBuilder`, `CreateWeldGrooveBuilder`, `CreateWeldPointBuilder`, `CreateWeldBeadBuilder` | Needs UGWELD license |
| Structure Designer: frames, members, gussets, DSTV export | M (machine frames) | Hard | `Features.CreateStructureDesignMemberBuilder` and related builders | NXSTRUCTUREDESIGN installed |
| CAE hand-off: create FEM/SIM, tet mesh, material, constraints and loads, solve | M | Hard | `CAE` `CreateMesh3dTetBuilder`, `CreateSolutionBuilder`, constraint and load builders | Simcenter license; separate part types |
| CAM: setup, operations, post-processing | L for design engineers | Hard | `CAM` `CreateProgram`, `CreateOperation`, many tool builders | MACH installed |
| Routing (mechanical and electrical), mold wizard, die design, ship design | L | Hard | `Routing`, `MechanicalRouting`, `Tooling`, `Die` | Niche |
| Drawing compare and revision compare | L | Med | `Drawings.CreateCompareReportBuilder` | Review workflows |
| Neutral BOM export to CSV/XLSX from assembly structure (no drawing) | M | Easy | `nx_list_components` + attributes | Sidecar-side formatting after item 1 |

## Already covered (checked, not a gap)

These areas are already covered:

- Interference and clearance: `nx_check_interference`, `nx_check_clearance`,
  `nx_highlight_collisions`, `nx_inspection_report`.
- Exploded views: 9 explosion tools.
- Parts list and balloons.
- Revision table: `nx_drawing_table kind=revision`.
- Measurement: distance, angle, volume, mass, bounding box, wall thickness,
  face and curve analysis, surface continuity.
- Dimension tolerances: bilateral, limits and basic.
- Sheet metal: comprehensive.
- Thread standards.
- Delete face: `nx_edit_faces heal`.
- Extract body: internal to `nx_transform_bodies`, not public.

## Before implementing: live confirmations needed

Run these with `dev_python.py` under the lock, when NX is free:

1. Owning collection and signature of each [bin] builder
   (`dir(part.Features)`, `dir(part.Annotations)`, `dir(part.DraftingViews)`,
   `dir(session.DexManager)`).
2. License availability for JT, DXFDWG, CATIA, Creo and SolidWorks
   translators, UGWELD, Structure Designer and Simcenter. Use the
   `LicenseManager` API or a failed-builder probe in a scratch part.
3. Tolerance enum values for fits on 2406 dimension builders.
4. Whether `HoleCalloutDimensionBuilder` supports modern `HolePackage`
   features created by `nx_hole_feature`.
