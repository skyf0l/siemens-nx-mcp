"""NX 2606 integration: explicit frames, guarded mutations and inspectable results.

NXOpen is accessed only by the existing main-thread dispatcher.
"""

from __future__ import annotations

import contextlib
import inspect
import uuid

from nx_mcp import (
    assembly_documentation_server,
    documentation_editing_server,
    freeform_server,
    manufacturing_server,
    side_effects,
)
from nx_mcp.advanced_authoring import AdvancedAuthoringMixin
from nx_mcp.annotation_updates import AnnotationUpdatesMixin
from nx_mcp.assembly_documentation import AssemblyDocumentationMixin
from nx_mcp.authoring import AuthoringMixin
from nx_mcp.authoring_server import NON_MODEL as AUTHORING_NON_MODEL
from nx_mcp.authoring_server import READ_ONLY as AUTHORING_READ_ONLY
from nx_mcp.datum_features import DatumFeaturesMixin
from nx_mcp.documentation_editing import DocumentationEditingMixin
from nx_mcp.drawing_preferences import DrawingPreferencesMixin
from nx_mcp.engineering import EngineeringMixin
from nx_mcp.exploded_views import ExplodedViewsMixin
from nx_mcp.feature_groups import server_modules as feature_group_modules
from nx_mcp.freeform import FreeformMixin
from nx_mcp.host import dispatch
from nx_mcp.inspection import InspectionMixin
from nx_mcp.inventory import compact_reference

# Vector math lives in nx_mcp.kit.geom; these names stay importable from here.
from nx_mcp.kit.geom import (  # noqa: F401
    IDENTITY,
    add,
    cross,
    dot,
    matmul,
    matvec,
    rows,
    transpose,
    xyz,
)
from nx_mcp.legacy_kit import vector
from nx_mcp.manufacturing import ManufacturingMixin
from nx_mcp.nx_bridge import NXOpenExecutor
from nx_mcp.recovery import OperationStore, timestamp
from nx_mcp.reference_geometry import ReferenceGeometryMixin
from nx_mcp.release_engineering import ReleaseEngineeringMixin
from nx_mcp.review_tools import ReviewToolsMixin
from nx_mcp.runtime import NXToolError, api_error
from nx_mcp.visual_tools import VisualToolsMixin

READ_ONLY = {
    "nx_list_datums",
    "nx_list_sketches",
    "nx_list_features",
    "nx_list_bodies",
    "nx_list_topology",
    "nx_capabilities",
}
# Files and session lifecycle cannot be reversed by a model undo mark.
NON_MODEL: set[str] = set()


READ_ONLY.update(
    AUTHORING_READ_ONLY
    | freeform_server.READ_ONLY
    | manufacturing_server.READ_ONLY
    | assembly_documentation_server.READ_ONLY
    | documentation_editing_server.READ_ONLY
)
NON_MODEL.update(
    AUTHORING_NON_MODEL
    | freeform_server.NON_MODEL
    | manufacturing_server.NON_MODEL
    | assembly_documentation_server.NON_MODEL
    | documentation_editing_server.NON_MODEL
)
for _group in feature_group_modules():
    READ_ONLY.update(_group.READ_ONLY)
    NON_MODEL.update(_group.NON_MODEL)


def _not_implemented(name):
    def handler(**_params):
        raise NXToolError("NX_NOT_IMPLEMENTED", f"{name} has a schema but no handler yet")

    return handler


class HardenedExecutor(
    DatumFeaturesMixin,
    DrawingPreferencesMixin,
    ReleaseEngineeringMixin,
    ReferenceGeometryMixin,
    DocumentationEditingMixin,
    AnnotationUpdatesMixin,
    FreeformMixin,
    ManufacturingMixin,
    AssemblyDocumentationMixin,
    ExplodedViewsMixin,
    EngineeringMixin,
    AdvancedAuthoringMixin,
    AuthoringMixin,
    ReviewToolsMixin,
    VisualToolsMixin,
    InspectionMixin,
    NXOpenExecutor,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = uuid.uuid4().hex
        self.objects.session_id = self.session_id
        self._part_generations = {}
        self._history = []
        self._checkpoints = {}
        self.store = OperationStore(self.workspace.root)
        self.store.recover(self.session_id)
        self._current_operation = None
        for module in [freeform_server, manufacturing_server, assembly_documentation_server]:
            for name in vars(module):
                if name.startswith("nx_"):
                    self._handlers[name] = getattr(self, "_" + name[3:])
        for module in feature_group_modules():
            for name in vars(module):
                if name.startswith("nx_"):
                    # A schema without its handler yet must not stop the host from
                    # starting; test_feature_groups requires every handler to exist.
                    self._handlers[name] = getattr(self, "_" + name[3:], _not_implemented(name))
        for name, fn in vars(documentation_editing_server).items():
            if name.startswith("nx_") and inspect.isfunction(fn):
                self._handlers[name] = getattr(self, "_" + name[3:])
        self._handlers.update(
            {
                "nx_list_datums": self._list_datums,
                "nx_list_expressions": self._list_expressions,
                "nx_list_topology": self._list_topology,
                "nx_capabilities": self._capabilities,
            }
        )

    @property
    def session(self):
        # Every NX object a handler can change starts here (side_effects.py).
        side_effects.touch()
        return self._nx_session

    @session.setter
    def session(self, value):
        self._nx_session = value

    def _part_id(self, part):
        tag = int(part.Tag)
        if tag not in self._part_generations:
            self._part_generations[tag] = uuid.uuid4().hex
        return "part_" + self.session_id + "_" + self._part_generations[tag]

    def _reference(self, value, kind, part, fallback):
        ref = super()._reference(value, kind, part, fallback)
        ref.update(
            session_id=self.session_id,
            generation_id=self._part_generations[int(part.Tag)],
            owner_part_path=part.FullPath,
            journal_id=str(getattr(value, "JournalIdentifier", "")),
            display_name=str(getattr(value, "Name", "")),
        )
        return ref

    @staticmethod
    def _name(value, fallback):
        return str(
            getattr(value, "Name", "") or getattr(value, "JournalIdentifier", "") or fallback
        )

    def _units(self):
        return (
            "mm"
            if self._work_part().PartUnits == self.nxopen.BasePart.Units.Millimeters
            else "inch"
        )

    def execute(self, method, params):
        # Catalogue tools run through the kernel (architecture.md section 4);
        # every other tool keeps the legacy path below.
        if (spec := dispatch.catalogue_spec(method)) is not None:
            return dispatch.run(self, spec, params)
        # Legacy tools keep the part-ownership rule during migration (gate 6).
        return dispatch.run_legacy(self, method, params)

    def _execute_legacy(self, method, params):
        params = dict(params)
        params.pop("intent", None)  # Agent's note for the NX panel; not a handler argument.
        supplied_id = params.pop("operation_id", None)
        mutable = method not in READ_ONLY
        op_id = supplied_id or ("op_" + uuid.uuid4().hex)
        handler = self._handlers.get(method)
        if handler is None:
            raise NXToolError("NX_TOOL_NOT_FOUND", method)
        self._check_version_gate(method)
        try:
            inspect.signature(handler).bind(**params)
        except TypeError as e:
            raise NXToolError("NX_INVALID_ARGUMENT", str(e)) from e
        record = None
        mark = None
        part = self._work_part(required=False)
        part_id = self._part_id(part) if part else None
        if mutable:
            try:
                fingerprint = self.store.fingerprint(method, params)
            except (ValueError, TypeError) as error:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Arguments must be finite JSON values"
                ) from error
            existing = self.store.get(op_id)
            if "fingerprint" in existing:
                if existing["fingerprint"] != fingerprint:
                    raise NXToolError(
                        "NX_IDEMPOTENCY_CONFLICT",
                        "operation_id was already used with different arguments",
                        details={"operation_id": op_id},
                    )
                if existing["state"] == "committed":
                    result = dict(existing["result"])
                    result["replayed"] = True
                    if existing.get("reverted_by"):
                        result["warnings"] = result.get("warnings", []) + [
                            "This operation was subsequently reverted by "
                            + existing["reverted_by"]
                            + "; replay does not recreate geometry."
                        ]
                    if existing["session_id"] != self.session_id:
                        result["warnings"] = result.get("warnings", []) + [
                            "Receipt is from an earlier NX session; reacquire object references."
                        ]
                    return result
                raise NXToolError(
                    "NX_OPERATION_" + existing["state"].upper(),
                    "Request was already recorded; inspect nx_get_operation before proceeding",
                    details=existing,
                )
            record = {
                "operation_id": op_id,
                "method": method,
                "fingerprint": fingerprint,
                "session_id": self.session_id,
                "part_id": part_id,
                "state": "running",
                "mutation_outcome": "unknown",
                "started_at": timestamp(),
            }
            self.store.put(record)
        before = {}
        have_before = False  # An empty part has an empty, but real, snapshot.
        previous = self._current_operation
        self._current_operation = op_id
        observer = side_effects.SideEffectObserver().start()
        try:
            if mutable and method not in NON_MODEL and part:
                before = self._snapshot(part)
                have_before = True
            if mutable and method not in NON_MODEL:
                mark = self.session.SetUndoMark(
                    self.nxopen.Session.MarkVisibility.Visible, "NX MCP: " + method
                )
            self._active_mark = mark
            result = handler(**params)
            if mark is not None and part is not None:
                refreshed = self._refresh_annotations()
                if refreshed["updated_count"]:
                    result["refreshed_annotations"] = refreshed["updated"]
            if result.get("status") == "error":
                raise NXToolError(
                    result.get("code", result.get("error_code", "NX_OPERATION_FAILED")),
                    result.get("message", "Operation failed"),
                )
            result = {
                "status": "success",
                **result,
                "operation_id": op_id,
                "session_id": self.session_id,
                "mutation_outcome": "committed" if mutable else "not_applicable",
                "warnings": result.get("warnings", []),
            }
            if part:
                result.setdefault(
                    "units", self._units() if self._work_part(required=False) else None
                )
            if mark is not None:
                if part and have_before:
                    self._drop_inherited_feature_errors(part, before)
                after = self._snapshot(part) if part else {}
                result["changes"] = {
                    "created": [v for k, v in after.items() if k not in before],
                    "deleted": [v for k, v in before.items() if k not in after],
                    "modified": result.get("modified"),
                    "modified_tracking": "explicit only; null means not fully tracked",
                }
                self._invalidate_deleted(before, after, invalidate_topology=True)
                self._history.append(
                    {"mark": mark, "part_id": part_id, "operation_id": op_id, "method": method}
                )
        except Exception as error:
            if mutable:
                self._review_epoch = getattr(self, "_review_epoch", 0) + 1
            outcome = "not_started" if mark is None else "partial"
            references = "invalidated"
            if mark is not None:
                # A call that failed before creating or deleting anything (for
                # example an argument check) keeps every reference: undo restores
                # the same NX objects. Anything structural, or any doubt,
                # invalidates the whole part as before.
                try:
                    unchanged = have_before and self._snapshot(part).keys() == before.keys()
                except Exception:
                    unchanged = False
                try:
                    self.session.UndoToMark(mark, None)
                    if unchanged:
                        references = "preserved"
                    else:
                        self.objects.invalidate_part(part_id)
                    self.session.DeleteUndoMark(mark, None)
                    self._clear_update_errors()
                    outcome = "rolled_back"
                except Exception as rollback_error:
                    error = NXToolError(
                        "NX_ROLLBACK_FAILED",
                        str(rollback_error),
                        details={"operation_error": str(error)},
                    )
            elif mutable and method in NON_MODEL:
                # No undo mark: "not_started" only when the observer proves that
                # nothing NX, file or process related ran before the failure.
                outcome = "unknown" if observer.touched else "not_started"
            err = error if isinstance(error, NXToolError) else api_error(error)
            if mark is None:
                # Inspections may create temporary native geometry. Preserve a
                # handler's explicit cleanup outcome when no outer rollback ran.
                outcome = err.details.get("mutation_outcome", outcome)
            err.details.update(operation_id=op_id, mutation_outcome=outcome)
            if outcome == "rolled_back":
                err.details.setdefault("references", references)
            if record:
                record.update(
                    state="failed",
                    mutation_outcome=outcome,
                    error=err.as_dict(),
                    finished_at=timestamp(),
                )
                self.store.put(record)
            raise err from error
        finally:
            observer.stop()
            self._current_operation = previous
        if mutable:
            self._review_epoch = getattr(self, "_review_epoch", 0) + 1
        if record:
            record.update(
                state="committed",
                mutation_outcome="committed",
                result=result,
                finished_at=timestamp(),
            )
            # Receipt persistence is outside the rollback block: a persistence failure must
            # never undo an already reported commit. A running receipt becomes unknown on restart.
            self.store.put(record)
        return result

    @side_effects.lookup
    def _resolve(self, ref, kinds=None, part=None):
        part = part or self._work_part()
        if ref.startswith("obj_"):
            try:
                value = self.objects.resolve(ref, part_id=self._part_id(part))
            except NXToolError as error:
                if error.code == "NX_OBJECT_NOT_FOUND":
                    raise NXToolError(
                        "NX_OBJECT_STALE", "Reference is not live in this NX session"
                    ) from error
                raise
            entry = self.objects._objects[ref]
            if kinds and entry.reference.kind not in kinds:
                raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "Unsupported object kind")
            return value
        pools = {
            "feature": list(part.Features),
            "body": list(part.Bodies),
            "sketch": list(part.Sketches),
            "component": [c for c, _ in self._walk_components(part)],
            "section": list(part.DynamicSections),
        }
        candidates = []
        for kind, values in pools.items():
            if kinds and kind not in kinds:
                continue
            for v in values:
                if ref.casefold() in {
                    self._name(v, "").casefold(),
                    str(getattr(v, "JournalIdentifier", "")).casefold(),
                } and all(int(c.Tag) != int(v.Tag) for c in candidates):
                    candidates.append(v)
        if len(candidates) != 1:
            raise NXToolError(
                "NX_AMBIGUOUS_REFERENCE" if candidates else "NX_NOT_FOUND",
                "Use an opaque ID; name did not resolve uniquely: " + ref,
            )
        return candidates[0]

    def _resolve_body(self, body, part):
        return self._resolve(body, {"body"}, part)

    def _find_sketch(self, name):
        return self._resolve(name, {"sketch"})

    def _activate_part(self, part, work=True, display=True):
        if part.startswith("obj_"):
            target = self.objects.resolve(part, expected_kind="part")
        else:
            matches = [
                p
                for p in self.session.Parts
                if part.casefold() in {p.FullPath.casefold(), p.Name.casefold()}
            ]
            if len(matches) != 1:
                raise NXToolError("NX_NOT_FOUND", "Loaded part must resolve uniquely")
            target = matches[0]
        if display:
            _, status = self.session.Parts.SetDisplay(target, False, False)
            if status:
                status.Dispose()
        if work:
            self.session.Parts.SetWork(target)
        return {
            "part": self._reference(target, "part", target, "Part"),
            "work": self.session.Parts.Work == target,
            "display": self.session.Parts.Display == target,
            "message": "Activated loaded part" if work or display else "Resolved loaded part",
        }

    def _sketch_frame(self, sketch):
        m = sketch.Orientation.Element
        return {
            "origin": xyz(sketch.Origin),
            "x_axis": [m.Xx, m.Xy, m.Xz],
            "y_axis": [m.Yx, m.Yy, m.Yz],
            "normal": [m.Zx, m.Zy, m.Zz],
            "coordinate_frame": "part",
        }

    def _nx_matrix(self, m):
        matrix = self.nxopen.Matrix3x3()
        for i, prefix in enumerate("XYZ"):
            for j, suffix in enumerate("xyz"):
                setattr(matrix, prefix + suffix, m[j][i])
        return matrix

    def _walk_components(self, part):
        root = part.ComponentAssembly.RootComponent

        def walk(parent, path):
            for c in parent.GetChildren():
                p = path + [self._name(c, "Component")]
                yield c, p
                yield from walk(c, p)

        return list(walk(root, [])) if root else []

    def _list_topology(self, body, face=None, include_adjacency=False, compact=False):
        b = self._resolve(body, {"body"})
        part = self._work_part()
        faces = list(b.GetFaces())
        if face is not None:
            selected = self._resolve(face, {"face"})
            if selected not in faces:
                raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "face must belong to body")
            faces = [selected]
        edges = (
            list({int(e.Tag): e for f in faces for e in f.GetEdges()}.values())
            if face
            else list(b.GetEdges())
        )

        def ref(value, kind):
            r = self._reference(value, kind, part, kind.title())
            return compact_reference(r) if compact else r

        result = {
            "body": ref(b, "body"),
            "faces": [ref(f, "face") for f in faces],
            "edges": [ref(e, "edge") for e in edges],
            "solid": b.IsSolidBody,
            "face_count": len(faces),
            "edge_count": len(edges),
        }
        if include_adjacency:
            result["face_edges"] = [
                {
                    "face": ref(f, "face")["id"],
                    "edges": [ref(e, "edge")["id"] for e in f.GetEdges()],
                }
                for f in faces
            ]
            result["edge_faces"] = [
                {
                    "edge": ref(e, "edge")["id"],
                    "faces": [ref(f, "face")["id"] for f in e.GetFaces()],
                }
                for e in edges
            ]
        return result

    def _geometry(self, ref=None, scope="auto"):
        part = self._work_part()
        if scope not in {"auto", "part", "assembly"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "scope must be auto, part or assembly")
        values = []
        if ref:
            obj = self._resolve(ref, {"body", "face", "edge", "component", "feature"})
            if hasattr(obj, "FindOccurrence"):
                selected = [obj] + [
                    c for c, _ in self._walk_components(part) if self._descendant(c, obj)
                ]
                for c in selected:
                    values.extend(self._occurrence_bodies(c))
            elif hasattr(obj, "GetBodies") and not hasattr(obj, "IsSolidBody"):
                values = list(obj.GetBodies())
            else:
                values = [obj]
        else:
            values = list(part.Bodies)
            if scope == "assembly" or (scope == "auto" and part.ComponentAssembly.RootComponent):
                for c, _ in self._walk_components(part):
                    values.extend(self._occurrence_bodies(c))
        if not values:
            raise NXToolError(
                "NX_NO_TARGET_BODY",
                "No included geometry; inspect suppression, loading and reference sets",
            )
        return values

    @staticmethod
    def _descendant(c, parent):
        while c.Parent:
            c = c.Parent
            if c == parent:
                return True
        return False

    def _occurrence_bodies(self, c):
        p = c
        while p:
            if p.IsSuppressed:
                return []
            p = p.Parent
        bodies = []
        for b in c.Prototype.Bodies:
            occurrence = c.FindOccurrence(b)
            if occurrence is not None:
                bodies.append(occurrence)
        return bodies

    def _get_bounding_box(self, body=None, scope="auto", precision="conservative"):
        import NXOpen.UF

        part = self._work_part()
        uf = NXOpen.UF.UFSession.GetUFSession()
        objects = self._geometry(body, scope)
        if precision not in {"conservative", "exact"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "precision must be conservative or exact")
        if precision == "exact":
            wcs = rows(part.WCS.CoordinateSystem.Orientation.Element)
            if any(abs(wcs[i][j] - IDENTITY[i][j]) > 1e-8 for i in range(3) for j in range(3)):
                raise NXToolError(
                    "NX_UNSUPPORTED_FRAME",
                    "Exact absolute bounds currently require axis-aligned WCS",
                )
        result = []
        for item in objects:
            if precision == "exact":
                low, directions, lengths = uf.ModlGeneral.AskBoundingBoxExact(item.Tag, 0)
                if len(directions) == 9:
                    directions = [directions[i : i + 3] for i in range(0, 9, 3)]
                corners = [
                    [
                        low[i]
                        + sum(directions[j][i] * lengths[j] * ((mask >> j) & 1) for j in range(3))
                        for i in range(3)
                    ]
                    for mask in range(8)
                ]
                box = [min(p[i] for p in corners) for i in range(3)] + [
                    max(p[i] for p in corners) for i in range(3)
                ]
            else:
                box = list(uf.ModlGeneral.AskBoundingBox(item.Tag))
            result.append(
                {
                    "body": self._reference(item, "body", part, "Body"),
                    "box": box,
                    "solid": bool(item.IsSolidBody),
                }
            )
        low = [min(row["box"][i] for row in result) for i in range(3)]
        high = [max(row["box"][i + 3] for row in result) for i in range(3)]
        return {
            "min": low,
            "max": high,
            "dimensions": [b - a for a, b in zip(low, high, strict=False)],
            "units": self._units(),
            "coordinate_frame": "work_part",
            "bounds_type": precision,
            "bodies": result,
            "body_count": len(result),
            "scope": scope,
            "message": "Native UF "
            + precision
            + " bounds of included body occurrences; suppression and reference sets honored",
        }

    @staticmethod
    def _validate_rotation(matrix):
        if len(matrix) != 3:
            raise NXToolError("NX_INVALID_ARGUMENT", "rotation_matrix must be 3x3")
        m = [vector(r) for r in matrix]
        if (
            any(
                abs(dot(m[i], m[j]) - (1 if i == j else 0)) > 1e-8
                for i in range(3)
                for j in range(3)
            )
            or abs(dot(m[0], cross(m[1], m[2])) - 1) > 1e-8
        ):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "rotation_matrix must be a right-handed orthonormal matrix"
            )
        return m

    def _capabilities(self, tool=None, prefix=None):

        from nx_mcp.manifest import load_manifest, scope_to_version

        manifest = load_manifest()
        part = self._work_part(required=False)
        manifest.update(
            session_id=self.session_id,
            # The manifest's own nx_version is the evidence baseline, not this session.
            manifest_baseline_nx_version=manifest.get("nx_version"),
            nx_version=self.nx_version,
            actual_nx_version=self.nx_version,
            execution=(
                "serialized NX batch journal thread"
                if self.session.IsBatch
                else "serialized NX UI thread; visible model viewport"
            ),
            interactive=not self.session.IsBatch,
            api_detection={
                "native_display_modification": hasattr(
                    self.session.DisplayManager, "NewDisplayModification"
                ),
                "dynamic_sections": bool(part and hasattr(part, "DynamicSections")),
                "sketch_solver_status": hasattr(self.nxopen.Sketch, "CalculateStatus"),
                "step_import": hasattr(self.session.DexManager, "CreateStep214Importer"),
                "native_pattern": bool(
                    part and hasattr(part.Features, "CreatePatternFeatureBuilder")
                ),
                "minimum_distance": hasattr(self.session.Measurement, "GetMinimumDistance"),
                "native_component_pattern": bool(
                    part and hasattr(part.ComponentAssembly, "CreateComponentPatternBuilder")
                ),
                "sketch_dimensions": hasattr(self.nxopen.Sketch, "CreateDimension"),
            },
            coordinate_conventions={
                "lengths": "work-part units unless explicitly named mm3",
                "angles": "degrees",
                "rotation_order": "Rz * Ry * Rx",
                "matrix": "row-major 3x3; p_parent = R*p_local + t",
                "legacy_rotation_list": "NX axis vectors X,Y,Z, not row-major",
                "sketch_XZ_normal": [0, -1, 0],
            },
        )
        scope_to_version(manifest, self.nx_version)
        if tool is not None and prefix is not None:
            raise NXToolError("NX_INVALID_ARGUMENT", "Use tool or prefix, not both")
        total = len(manifest["tools"])
        if tool is not None:
            if tool not in manifest["tools"]:
                raise NXToolError("NX_NOT_FOUND", "Tool is not in the capability manifest")
            manifest["tools"] = {tool: manifest["tools"][tool]}
        elif prefix is not None:
            manifest["tools"] = {k: v for k, v in manifest["tools"].items() if k.startswith(prefix)}
        manifest.update(tool_count=len(manifest["tools"]), total_tool_count=total, units=None)
        return manifest

    def _clear_update_errors(self):
        """Drop NX update errors left by a rolled-back commit.

        Otherwise NX attaches the failed operation's message to the next
        feature, and model health reports an error that no longer exists.
        """
        manager = getattr(self.session, "UpdateManager", None)
        clear = getattr(manager, "ClearErrorList", None)
        if clear is not None:
            with contextlib.suppress(Exception):
                clear()

    def _child_handler(self, method, params):
        """Gate a nested call (batch, preview) exactly like execute() gates a tool.

        Applies the version gate and binds the arguments; the parent call keeps
        its own single undo mark and receipt.
        """
        handler = self._handlers.get(method)
        if handler is None:
            raise NXToolError("NX_TOOL_NOT_FOUND", method)
        self._check_version_gate(method)
        if not isinstance(params, dict):
            raise NXToolError("NX_INVALID_ARGUMENT", f"{method} parameters must be an object")
        try:
            inspect.signature(handler).bind(**params)
        except TypeError as exc:
            raise NXToolError("NX_INVALID_ARGUMENT", f"{method} parameters: {exc}") from exc
        return handler

    def _call_child(self, method, params):
        return self._child_handler(method, params)(**params)

    def _check_version_gate(self, method):
        """Refuse a tool disabled for this NX version before it can touch NX."""
        from nx_mcp.manifest import load_manifest, unavailable_reason

        if getattr(self, "_manifest_tools", None) is None:
            self._manifest_tools = load_manifest()["tools"]
        reason = unavailable_reason(self._manifest_tools.get(method, {}), self.nx_version)
        if reason:
            raise NXToolError(
                "NX_UNAVAILABLE_ON_VERSION",
                f"{method} is not available on NX {self.nx_version}: {reason}",
                details={"nx_version": self.nx_version, "tool": method},
            )

    def _drop_inherited_feature_errors(self, part, before):
        """Update new features that report errors once, dropping inherited messages.

        NX 2406 keeps the error of a rolled-back feature and attaches it to the next
        feature created under the same name (for example CHAMFER(3)), although that
        feature built fine; model health then reports an error that does not exist.
        Updating the feature drops the stale message; a real error remains.
        """
        manager = getattr(self.session, "UpdateManager", None)
        if not hasattr(manager, "LogForUpdate"):
            return
        with contextlib.suppress(Exception):
            new = [
                f
                for f in part.Features
                if ("feature", int(f.Tag)) not in before
                and list(getattr(f, "GetFeatureErrorMessages", list)())
            ]
            if new:
                for feature in new:
                    manager.LogForUpdate(feature)
                manager.DoUpdate(self._active_mark)

    def _snapshot(self, part):
        groups = [
            ("explosion", self._explosions(part)),
            ("modeling_view", self._modeling_views(part)),
            ("assembly_constraint", self._assembly_constraints(part)),
            ("drawing_sheet", getattr(part, "DrawingSheets", [])),
            ("drawing_view", getattr(part, "DraftingViews", [])),
            ("dimension", getattr(part, "Dimensions", [])),
            ("annotation", self._documentation_annotations(part)),
            ("traceline", getattr(part, "Tracelines", [])),
            (
                "component_pattern",
                self._component_patterns(part),
            ),
            ("expression", getattr(part, "Expressions", [])),
            ("body", part.Bodies),
            ("feature", part.Features),
            ("curve", part.Curves),
            ("point", getattr(part, "Points", [])),
            ("sketch", part.Sketches),
            ("section", getattr(part, "DynamicSections", [])),
            ("component", [c for c, _ in self._walk_components(part)]),
        ]
        return {
            (kind, int(v.Tag)): self._reference(v, kind, part, kind.title())
            for kind, values in groups
            for v in values
        }

    def _invalidate_deleted(self, before, after, invalidate_topology=True):
        removed = {v["id"] for k, v in before.items() if k not in after}
        for key, entry in list(self.objects._objects.items()):
            if key in removed or (invalidate_topology and entry.reference.kind in {"face", "edge"}):
                self.objects._objects.pop(key, None)
                self.objects._stale_ids.add(key)
        self.objects._identities = {
            k: v for k, v in self.objects._identities.items() if v in self.objects._objects
        }

    def _measure_volume(self, body=None, scope="auto"):
        part = self._work_part()
        bodies = self._geometry(body, scope)
        units = [
            part.UnitCollection.FindObject(n)
            for n in ["SquareMilliMeter", "CubicMilliMeter", "Kilogram", "MilliMeter", "Newton"]
        ]
        result = []
        for b in bodies:
            if not b.IsSolidBody:
                raise NXToolError("NX_NOT_SOLID", "Volume requires solid bodies")
            props = part.MeasureManager.NewMassProperties(units, 0.999, [b])
            try:
                props.InformationUnit = self.nxopen.MeasureBodies.AnalysisUnit.KilogramMillimeter
                result.append(
                    {
                        "body": self._reference(b, "body", part, "Body"),
                        "volume_mm3": float(props.Volume),
                    }
                )
            finally:
                props.Dispose()
        return {
            "bodies": result,
            "body_count": len(result),
            "volume_mm3": sum(r["volume_mm3"] for r in result),
            "volume_units": "mm^3",
            "semantics": "sum_of_included_bodies",
            "scope": scope,
            "warnings": [
                "Overlapping bodies are counted separately; this is not union volume or a mass estimate."
            ],
        }
