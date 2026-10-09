"""Geometric queries and guarded authoring on the serialized NX thread."""

from __future__ import annotations

from nx_mcp.runtime import NXToolError


def page(items, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise NXToolError("NX_INVALID_ARGUMENT", "offset >= 0 and limit 1–200 are required")
    end = offset + limit
    return {
        "items": items[offset:end],
        "total": len(items),
        "offset": offset,
        "next_offset": end if end < len(items) else None,
    }


def _expression_list(exp, method):
    """Expressions returned by an optional NXOpen dependency query, [] if absent."""
    query = getattr(exp, method, None)
    return list(query()) if query is not None else []


class AuthoringMixin:
    def _require_api(self, obj, *names):
        missing = [n for n in names if not hasattr(obj, n)]
        if missing:
            raise NXToolError("NX_API_UNAVAILABLE", "Installed NX API lacks: " + ", ".join(missing))

    def _update_model(self):
        count = self.session.UpdateManager.DoUpdate(self._active_mark)
        if count:
            raise NXToolError("NX_UPDATE_FAILED", str(count) + " native update errors")

    def _solve_active_sketch(self, expressions):
        """Solve an active sketch after its dimension expressions were edited.

        NX 2406: a model update leaves the active sketch unsolved (the expression
        changes, the curves do not) until the sketch is finished. LocalUpdate
        solves it now; each edited driving dimension must then measure its value.
        """
        sketch = self.session.ActiveSketch
        if not sketch:
            return
        tags, pending = set(), list(expressions)
        while pending and len(tags) < 10000:  # edited expressions and all that use them
            exp = pending.pop()
            if int(exp.Tag) not in tags:
                tags.add(int(exp.Tag))
                pending.extend(_expression_list(exp, "GetReferencingExpressions"))
        dimensions = [
            c
            for c in sketch.GetAllConstraintsOfType(
                self.nxopen.Sketch.ConstraintClass.Dimension,
                self.nxopen.Sketch.ConstraintType.NoCon,
            )
            if getattr(c, "AssociatedExpression", None) is not None
            and int(c.AssociatedExpression.Tag) in tags
        ]
        if not dimensions:
            return
        if hasattr(sketch, "LocalUpdate"):
            sketch.LocalUpdate()
        sketch.Update()
        for constraint in dimensions:
            value = float(constraint.AssociatedExpression.Value)
            measured = float(constraint.AssociatedDimension.ComputedSize)
            if abs(measured - value) > 1e-6 * max(1.0, abs(value)):
                raise NXToolError(
                    "NX_SKETCH_CONFLICT",
                    "NX could not solve the active sketch to the new dimension value; rolling back",
                    details={
                        "expression": constraint.AssociatedExpression.Name,
                        "requested": value,
                        "measured": measured,
                    },
                )

    def _expression(self, reference):
        if reference.startswith("obj_"):
            return self._resolve(reference, {"expression"})
        matches = [e for e in self._work_part().Expressions if e.Name == reference]
        if len(matches) != 1:
            raise NXToolError(
                "NX_NOT_FOUND", "Expression names are case-sensitive; use an expression ID"
            )
        return matches[0]

    def _expression_record(self, exp):
        part = self._work_part()
        return {
            "object": self._reference(exp, "expression", part, "Expression"),
            "name": exp.Name,
            "formula": exp.RightHandSide,
            "type": exp.Type,
            "value": (
                exp.GetValueUsingUnits(self.nxopen.Expression.UnitsOption.Expression)
                if exp.Type == "Number"
                else exp.IntegerValue
                if exp.Type == "Integer"
                else None
            ),
            "units": exp.Units.Abbreviation if exp.Units else "unitless",
            "editable": not (
                exp.IsNoEdit or exp.IsRightHandSideLockedFromEdit or exp.IsInterpartExpression
            ),
            # NX 2406 lacks GetExpressionParents; dependents are still available.
            "parents": [
                self._reference(e, "expression", part, "Expression")
                for e in _expression_list(exp, "GetExpressionParents")
            ],
            "dependents": [
                self._reference(e, "expression", part, "Expression")
                for e in _expression_list(exp, "GetReferencingExpressions")
            ],
            "dependency_scope": (
                "stored immediate expression dependencies; conditional branches may be incomplete"
                if hasattr(exp, "GetExpressionParents")
                else "parents unavailable in this NX version; dependents are immediate referencing expressions"
            ),
        }

    def _list_expressions(self, name_contains=None, offset=0, limit=50):
        values = sorted(self._work_part().Expressions, key=lambda e: e.Name)
        if name_contains is not None:
            values = [e for e in values if name_contains.casefold() in e.Name.casefold()]
        result = page(values, offset, limit)
        result["items"] = [self._expression_record(e) for e in result["items"]]
        return result

    def _highlight_objects(self, objects):
        self._visual_part()
        values = self._display_targets(objects)
        self._clear_highlights()
        try:
            for obj in values:
                obj.Highlight()
                self._highlighted_objects.append(obj)
        except Exception:
            self._clear_highlights()
            raise
        return {"highlighted": [self._display_ref(v) for v in values], "count": len(values)}

    def _consistency_fault(self, owner, code, tag):
        result = {"code": int(code), "native_tag": int(tag), "object": None}
        try:
            result["message"] = self.nxopen.NXException(int(code)).GetMessage()
        except Exception:
            result["message"] = "Native diagnostic message unavailable"
        try:
            obj = self.nxopen.TaggedObjectManager.GetTaggedObject(tag)
            kind = next(
                k
                for k, cls in [
                    ("face", self.nxopen.Face),
                    ("edge", self.nxopen.Edge),
                    ("body", self.nxopen.Body),
                ]
                if isinstance(obj, cls)
            )
            result["object"] = self._reference(obj, kind, owner, "Faulty entity")
        except Exception as error:
            result["reference_warning"] = str(error)
        return result

    def _model_health(self, scope="part", offset=0, limit=50):
        import NXOpen.UF

        if scope not in {"part", "assembly"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "scope must be part or assembly")
        part = self._work_part()
        uf = NXOpen.UF.UFSession.GetUFSession()
        issues = []
        feature_count = 0
        parts = {int(part.Tag): part}
        components = self._walk_components(part) if scope == "assembly" else []
        for c, path in components:
            if c.IsSuppressed:
                issues.append(
                    {"severity": "info", "kind": "suppressed_component", "occurrence_path": path}
                )
            elif c.Prototype is None or not hasattr(c.Prototype, "Features"):
                issues.append(
                    {"severity": "warning", "kind": "unloaded_component", "occurrence_path": path}
                )
            else:
                parts[int(c.Prototype.Tag)] = c.Prototype
        checked = 0
        for owner in parts.values():
            for f in owner.Features:
                feature_count += 1
                self._require_api(f, "GetFeatureErrorMessages", "GetFeatureWarningMessages")
                for severity, messages in [
                    ("error", f.GetFeatureErrorMessages()),
                    ("warning", f.GetFeatureWarningMessages()),
                ]:
                    for message in messages:
                        issues.append(
                            {
                                "severity": severity,
                                "kind": "feature_diagnostic",
                                "part": owner.FullPath,
                                "feature": f.JournalIdentifier,
                                "message": message,
                            }
                        )
                if f.Suppressed:
                    issues.append(
                        {
                            "severity": "info",
                            "kind": "suppressed_feature",
                            "part": owner.FullPath,
                            "feature": f.JournalIdentifier,
                        }
                    )
            for body in owner.Bodies:
                checked += 1
                self._require_api(uf.Modeling, "AskBodyConsistency")
                n, codes, tags = uf.Modeling.AskBodyConsistency(body.Tag)
                if n:
                    issues.append(
                        {
                            "severity": "error",
                            "kind": "body_consistency",
                            "part": owner.FullPath,
                            "body": body.JournalIdentifier,
                            "object": self._reference(body, "body", owner, "Faulty body"),
                            "faults": [
                                self._consistency_fault(owner, code, tag)
                                for code, tag in zip(codes, tags, strict=False)
                            ],
                            "repair_guidance": "Inspect the identified native entities. Self-intersection requires repairing or replacing the source face; nx_edit_faces heal is an explicit local edit, not a guaranteed body repair. Re-run nx_check_model after any repair and compare bounds/volume before acceptance.",
                            "fault_codes": list(codes),
                            "native_fault_tags": [int(t) for t in tags],
                        }
                    )
                if not body.IsSolidBody:
                    issues.append(
                        {
                            "severity": "info",
                            "kind": "sheet_body",
                            "part": owner.FullPath,
                            "body": body.JournalIdentifier,
                        }
                    )
        errors = sum(i["severity"] == "error" for i in issues)
        warnings = sum(i["severity"] == "warning" for i in issues)
        return {
            **page(issues, offset, limit),
            "healthy": errors == 0 and warnings == 0,
            "error_count": errors,
            "warning_count": warnings,
            "parts_checked": len(parts),
            "features_checked": feature_count,
            "bodies_checked": checked,
            "scope": scope,
            "checks": [
                "native feature diagnostics",
                "suppression",
                "loaded prototype availability",
                "UF body consistency",
            ],
            "limitations": [
                "Does not force a rebuild, prove design intent or check external files not loaded into NX.",
                "Shared prototypes checked once; suppressed components are reported, not loaded.",
            ],
        }
