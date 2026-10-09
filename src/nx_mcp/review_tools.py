"""Model summaries, reproducible views, inspection artifacts and reversible previews."""

from __future__ import annotations

import contextlib
import uuid

from nx_mcp.authoring import page
from nx_mcp.inspection import with_cleanup_failure
from nx_mcp.legacy_kit import finite, vector
from nx_mcp.runtime import NXToolError


class ReviewToolsMixin:
    def _set_camera(self, rotation, origin, scale):
        view = self._visual_part().ModelingViews.WorkView
        matrix = self._validate_rotation(rotation)
        point = vector(origin, "origin")
        value = finite(scale, "scale", True)
        self._require_api(view, "SetRotationTranslationScale")
        view.SetRotationTranslationScale(
            self._nx_matrix(matrix), self.nxopen.Point3d(*point), value
        )
        view.UpdateDisplay()
        return self._view_info()

    def _restore_camera(self, camera):
        self._set_camera(camera["rotation"], camera["origin"], camera["scale"])
        styles = {
            "shaded": "Shaded",
            "shaded_with_edges": "ShadedWithEdges",
            "wireframe": "StaticWireframe",
        }
        if camera["rendering_style"] in styles:
            self._work_part().ModelingViews.WorkView.RenderingStyle = getattr(
                self.nxopen.View.RenderingStyleType, styles[camera["rendering_style"]]
            )

    @contextlib.contextmanager
    def _temporary_view(self):
        camera = self._view_info()
        snapshot_keys = set(getattr(self, "_display_snapshots", {}))
        mark = self.session.SetUndoMark(
            self.nxopen.Session.MarkVisibility.Invisible, "NX MCP temporary presentation"
        )
        try:
            yield
        finally:
            try:
                self.session.UndoToMark(mark, None)
                self.session.DeleteUndoMark(mark, None)
            except Exception as exc:
                raise NXToolError(
                    "NX_ROLLBACK_FAILED",
                    "Temporary presentation cleanup failed: " + str(exc),
                    details={"mutation_outcome": "partial"},
                ) from exc
            finally:
                for key in set(getattr(self, "_display_snapshots", {})) - snapshot_keys:
                    self._display_snapshots.pop(key, None)
                self._restore_camera(camera)
                self._clear_highlights()

    def _locator(self, obj, kind):
        return {
            "kind": kind,
            "journal_id": str(obj.JournalIdentifier),
            "owner_part": self._work_part().FullPath,
        }

    def _locate(self, locator):
        part = self._work_part()
        if locator["owner_part"].casefold() != part.FullPath.casefold():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Saved reference belongs to another part")
        kind = locator["kind"]
        if kind == "expression":
            candidates = list(part.Expressions)
        elif kind == "component":
            candidates = [c for c, _ in self._walk_components(part)]
        elif kind == "feature":
            candidates = list(part.Features)
        elif kind == "sketch":
            candidates = list(part.Sketches)
        elif kind in {"face", "edge"}:
            bodies = self._geometry(scope="assembly")
            candidates = [
                v for b in bodies for v in (b.GetFaces() if kind == "face" else b.GetEdges())
            ]
        elif kind in {"curve", "body"}:
            candidates = list(part.Curves if kind == "curve" else part.Bodies)
        else:
            candidates = []
        matches = [c for c in candidates if c.JournalIdentifier == locator["journal_id"]]
        if len(matches) == 1:
            return matches[0]
        # Occurrence faces/bodies have assembly-context journal identifiers.
        try:
            obj = part.FindObject(locator["journal_id"])
        except Exception as exc:
            raise NXToolError(
                "NX_SAVED_REFERENCE_STALE",
                "Saved geometry no longer resolves: " + locator["journal_id"],
            ) from exc
        if obj is None:
            raise NXToolError("NX_SAVED_REFERENCE_STALE", "Saved reference is unavailable")
        valid = {
            "body": self.nxopen.Body,
            "face": self.nxopen.Face,
            "edge": self.nxopen.Edge,
            "sketch": self.nxopen.Sketch,
        }
        if kind in valid and not isinstance(obj, valid[kind]):
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "Saved reference changed kind")
        return obj

    def _model_summary(self, section="overview", offset=0, limit=50):
        part = self._work_part()
        if section not in {"overview", "components", "features", "expressions", "sketches"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported summary section")
        if section == "expressions":
            return self._list_expressions(offset=offset, limit=limit)
        if section == "components":
            rows = [
                {
                    "object": self._reference(c, "component", part, "Component"),
                    "name": c.Name,
                    "occurrence_path": path,
                    "depth": len(path) - 1,
                    "suppressed": bool(c.IsSuppressed),
                }
                for c, path in self._walk_components(part)
            ]
            return page(rows, offset, limit)
        if section == "features":
            result = page(list(part.Features), offset, limit)
            result["items"] = [
                {
                    "object": self._reference(f, "feature", part, "Feature"),
                    "type": f.FeatureType,
                    "suppressed": bool(f.Suppressed),
                    "parents": [
                        self._reference(v, "feature", part, "Feature") for v in f.GetParents()
                    ],
                    "expressions": [
                        {"name": e.Name, "formula": e.RightHandSide} for e in f.GetExpressions()
                    ],
                }
                for f in result["items"]
            ]
            return result
        if section == "sketches":
            result = page(list(part.Sketches), offset, limit)
            result["items"] = [
                {
                    "object": self._reference(s, "sketch", part, "Sketch"),
                    "curve_count": len(s.GetAllGeometry()),
                    "frame": self._sketch_frame(s),
                }
                for s in result["items"]
            ]
            return result
        try:
            bounds = self._get_bounding_box()
        except NXToolError as err:
            if err.code != "NX_NO_TARGET_BODY":
                raise
            bounds = None
        return {
            "part": self._reference(part, "part", part, "Part"),
            "modified": bool(part.IsModified),
            "units": self._units(),
            "counts": {
                "bodies": len(list(part.Bodies)),
                "features": len(list(part.Features)),
                "sketches": len(list(part.Sketches)),
                "expressions": len(list(part.Expressions)),
                "component_occurrences": len(self._walk_components(part)),
            },
            "bounds": {
                k: bounds[k]
                for k in ("min", "max", "dimensions", "bounds_type", "coordinate_frame")
            }
            if bounds
            else None,
            "diagnostics": self._model_health(limit=10),
            "sections": ["components", "features", "expressions", "sketches"],
            "warning": "Counts of owned bodies differ from recursive assembly geometry.",
        }

    def _preview_plan(self, operations):
        # Component placement moved to catalogue tools; no legacy tool is previewable.
        allowed: set[str] = set()
        if not isinstance(operations, list) or not 1 <= len(operations) <= 25:
            raise NXToolError("NX_INVALID_ARGUMENT", "Preview requires 1–25 supported edits")
        locators = {}

        def visit(value):
            if isinstance(value, str) and value.startswith("obj_"):
                obj = self._resolve(value)
                kind = self.objects._objects[value].reference.kind
                locators[value] = self._locator(obj, kind)
            elif isinstance(value, dict):
                for v in value.values():
                    visit(v)
            elif isinstance(value, list):
                for v in value:
                    visit(v)

        for op in operations:
            if (
                not isinstance(op, dict)
                or set(op) != {"method", "params"}
                or op["method"] not in allowed
                or not isinstance(op["params"], dict)
            ):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT",
                    "Preview supports expression, parameter, feature, sketch and placement edits only",
                )
            self._child_handler(op["method"], op["params"])
            visit(op["params"])
        return locators

    def _preview_snapshot(self):
        result = self._model_summary()
        result["parameters"] = [
            {k: self._expression_record(e)[k] for k in ("name", "formula", "value", "units")}
            for e in self._work_part().Expressions
            if e.Type == "Number"
        ]
        try:
            result["volume"] = self._measure_volume()
        except NXToolError as err:
            if err.code != "NX_NO_TARGET_BODY":
                raise
            result["volume"] = None
        return result

    def _preview_change(self, operations, capture=True):
        import copy

        locators = self._preview_plan(operations)
        before = self._preview_snapshot()
        part = self._work_part()
        pid = self._part_id(part)
        mark = self.session.SetUndoMark(
            self.nxopen.Session.MarkVisibility.Invisible, "NX MCP change preview"
        )
        previous = self._active_mark
        camera = self._view_info() if capture else None
        original = None
        try:
            self._active_mark = mark
            for op in operations:
                self._call_child(op["method"], op["params"])
            after = self._preview_snapshot()
            image = self._capture_view(fit=True) if capture else None
        except BaseException as exc:
            original = exc
            raise
        finally:
            self._active_mark = previous
            try:
                self.session.UndoToMark(mark, None)
                self.session.DeleteUndoMark(mark, None)
                self.objects.invalidate_part(pid)
            except Exception as exc:
                if original is None:
                    raise NXToolError(
                        "NX_ROLLBACK_FAILED",
                        "Preview rollback failed: " + str(exc),
                        details={"mutation_outcome": "partial"},
                    ) from exc
                # Keep the preview's own error; the rollback failure goes in its details.
                error = with_cleanup_failure(original, exc)
                if error is original:
                    raise original  # noqa: B904 - re-raise the preview's own error.
                raise error from original
            finally:
                if camera:
                    self._restore_camera(camera)
        token = "preview_" + uuid.uuid4().hex
        if not hasattr(self, "_previews"):
            self._previews = {}
        if len(self._previews) >= 20:
            self._previews.pop(next(iter(self._previews)))
        self._previews[token] = {
            "operations": copy.deepcopy(operations),
            "locators": locators,
            "part_id": pid,
            "epoch": getattr(self, "_review_epoch", 0),
        }
        return {
            "preview_id": token,
            "before": before,
            "after": after,
            "capture": image,
            "model_outcome": "rolled_back",
            "warnings": [
                "Preview geometry has already been rolled back; returned geometry references are stale. Accept reapplies the stored edits only if no intervening mutation or manual handoff occurred."
            ],
        }

    def _finish_preview(self, preview_id, action):
        if action not in {"accept", "discard"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "action must be accept or discard")
        preview = getattr(self, "_previews", {}).get(preview_id)
        if preview is None:
            raise NXToolError("NX_PREVIEW_STALE", "Unknown or consumed preview")
        if action == "discard":
            del self._previews[preview_id]
            return {"preview_id": preview_id, "action": "discard", "geometry_changed": False}
        if preview["epoch"] != getattr(self, "_review_epoch", 0) or preview[
            "part_id"
        ] != self._part_id(self._work_part()):
            raise NXToolError("NX_PREVIEW_STALE", "Model/session changed; create a fresh preview")
        replacements = {
            key: self._reference(self._locate(v), v["kind"], self._work_part(), "Preview target")[
                "id"
            ]
            for key, v in preview["locators"].items()
        }

        def translate(value):
            if isinstance(value, str):
                return replacements.get(value, value)
            if isinstance(value, dict):
                return {k: translate(v) for k, v in value.items()}
            if isinstance(value, list):
                return [translate(v) for v in value]
            return value

        results = [
            self._call_child(op["method"], translate(op["params"])) for op in preview["operations"]
        ]
        del self._previews[preview_id]
        return {
            "preview_id": preview_id,
            "action": "accept",
            "results": results,
            "geometry_changed": True,
        }
