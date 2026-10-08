# Feature tree: suppress, make current, reorder

Handlers: `src/nx_mcp/feature_tree.py` (`FeatureTreeMixin`); schemas:
`src/nx_mcp/feature_tree_server.py`; local tests: `tests/test_feature_tree.py`;
live test: `acceptance/live/feature_tree.json` (NX 2406).

All three tools are model mutations: the executor sets a visible undo mark and
rolls back on any error. Every result contains:

- `timeline`: user-visible features in timeline order, each
  `{feature, name, type, index, suppressed, active}`. Sketch-internal datum
  systems (`SKETCH(n:1B)`, `IsInternal`) are omitted and rejected as arguments.
- `bodies`: fresh IDs for the bodies now in the part.
- `changes` (from the executor): created/deleted objects.

**Stale references.** After each successful call the executor invalidates
every face/edge ID in the part and every object that left the part (for
example a body whose creating feature was suppressed). Feature IDs stay valid:
suppressed and inactive features stay in the timeline, and you need their IDs
to unsuppress them or make them current again. Body IDs stay valid while the
body exists. Verified live: the block body ID survived suppress, unsuppress,
make-current and reorder. Reacquire faces and edges with `nx_list_topology`
/ `nx_find_geometry`. Verified live: an edge ID taken before a suppress
returns `NX_OBJECT_STALE`. A **rejected** call (for example an illegal
reorder) is rolled back and the executor then invalidates **all** IDs of the
part, features and bodies included. Reacquire them with `nx_list_features` /
`nx_list_bodies`.

**Suppressed vs inactive.** NXOpen `Feature.Suppressed` is True both for
user-suppressed features and for features after the current feature. The
handlers read `UF_MODL_ask_suppress_feature`, whose mask is 1 = suppressed by
the user and 16 = inactive (rolled back), plus `Part.CurrentFeature`. So
`suppressed` and `active` are independent flags.

## nx_suppress_features(features, suppress)

Suppresses or unsuppresses 1-100 owned feature IDs (`FeatureCollection.SuppressFeatures`
/ `UnsuppressFeatures`), then updates the model.

- `changed`: every feature whose suppression state changed, with
  `requested` (listed by the caller or implied) and `before`/`after`
  `{suppressed}`.
- `unchanged`: requested features that were already in the requested state.
- `still_suppressed_dependents` (unsuppress only): descendants of the listed
  features that are still suppressed.

NX suppresses all dependents with a parent. **Unsuppress restores only the
listed features**, and NX's dependents stay suppressed. List them too (see
`still_suppressed_dependents`) to restore the full model. Native
unsuppress failures, or a requested feature left in the wrong state, raise
`NX_VERIFICATION_FAILED` and roll back.

```json
{"tool": "nx_suppress_features", "args": {"features": ["<boss feature id>"], "suppress": true}}
```

## nx_make_current_feature(feature=None)

`Feature.MakeCurrentFeature()` followed by a model update. The model is evaluated up to
`feature`, later features become inactive, and new features are inserted after
the current one. With no feature, the last feature in the timeline becomes
current again. Verified through `Part.CurrentFeature`; on a mismatch it raises
`NX_VERIFICATION_FAILED`. Returns `current`, `inactive` (in timeline order, each
with its own `suppressed` flag) and `activity_changed`.

## nx_reorder_feature(feature, target, position)

Moves `feature` directly before or after `target`
(`FeatureCollection.ReorderFeature`, `ReorderType.Before/After`).

- Pre-check: a feature may not move before any direct parent or after any
  direct child. Indirect relations are covered, because an ancestor inside
  the moved range implies a direct parent inside it. A violation raises
  `NX_REORDER_DEPENDENCY` with `details.conflicts` = [{feature, name, relation:
  parent|child}].
- If NX still refuses, the error is `NX_REORDER_REJECTED` with the native `nx_code`.
  NX 2406 refuses, for example, with 670529 "Cannot reorder. Feature to be
  reordered is being referenced."
- The resulting timeline must equal the requested order, otherwise
  `NX_VERIFICATION_FAILED` (rolled back).
- Returns `feature` (with its new `index`), `target`, `timeline_before` and
  `timeline`. A no-op move returns `moved: false`.

NX renumbers features after a reorder: `BLEND(6)` moved before `CYLINDER(5)`
becomes `BLEND(5)` and the cylinder becomes `CYLINDER(6)`. The opaque IDs do not
change, so do not rely on names.

## Verified live (NX 2406, acceptance/live/feature_tree.json)

Part: 60x40x20 block (EXTRUDE(2)), R10 x 15 boss united on top (EXTRUDE(4)),
D8 x 35 cylinder subtract through boss and block (CYLINDER(5)), R2 blend on the
boss top edge (BLEND(6), a child of the boss). Full volume 50901.571 mm3
(analytic 48000 + 940π - 51.526).

| step | result |
|---|---|
| suppress boss | changed = EXTRUDE(4) (requested) + BLEND(6) (dependent); volume 46994.690 = 48000 - 320π |
| old edge ID | `NX_OBJECT_STALE` |
| unsuppress boss | changed = EXTRUDE(4) only; blend in still_suppressed_dependents; volume 50953.097 = 48000 + 940π |
| unsuppress blend | volume 50901.571 (restored) |
| make EXTRUDE(2) current | inactive = SKETCH(3), EXTRUDE(4), CYLINDER(5), BLEND(6); volume 48000.000 |
| make last current | all active; volume 50901.571 |
| reorder blend before cylinder | nx_list_features: ... EXTRUDE(4), BLEND(5), CYLINDER(6); volume unchanged 50901.571 |
| original block body ID | still valid; volume 50901.571 |
| reorder blend before boss | `NX_REORDER_DEPENDENCY` (parent EXTRUDE(4)), `mutation_outcome: rolled_back`; timeline and volume unchanged |
| native illegal reorder (dev probe) | NX raises 670529 "Cannot reorder. Feature to be reordered is being referenced."; order unchanged |

## Known limits

- Only owned features in the work part; no assembly/occurrence features, no
  feature groups (`ReorderType.Into` is not exposed).
- One feature per reorder call.
- Make-current to a suppressed feature is passed to NX unchanged and has not been tested live.
- Unsuppress does not cascade (NX behaviour); use `still_suppressed_dependents`.
