"""NX-side handlers: Feature tree operations: suppress/unsuppress, make current, reorder.

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; read back the native feature state and raise
NX_VERIFICATION_FAILED on mismatch so the operation's undo mark rolls it back.

Stale references: the executor already invalidates every face/edge ID after a
model mutation and every object (body, feature, sketch, ...) that left the
part. Suppressed and inactive features stay in the timeline, so their feature
IDs remain valid (they are needed to unsuppress or make current again). Bodies
are re-listed with fresh IDs in every result so callers can reacquire.
"""

from __future__ import annotations

from nx_mcp.runtime import NXToolError

MAX_FEATURES = 100
POSITIONS = ("before", "after")


def _id_list(values, name, limit=MAX_FEATURES):
    """Validate a non-empty list of distinct object-ID/name strings."""
    if not isinstance(values, (list, tuple)) or not values:
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be a non-empty list of feature IDs")
    if len(values) > limit:
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} accepts at most {limit} features")
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must contain non-empty feature IDs")
    if len(set(values)) != len(values):
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must not repeat a feature")
    return list(values)


def _id(value, name):
    if not isinstance(value, str) or not value.strip():
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be a feature ID")
    return value


def reorder_conflicts(order, parents, children, moved, target, position):
    """Features that make moving `moved` before/after `target` illegal.

    order: timeline keys; parents/children: {key: set(keys)} direct relations.
    A feature may not end up before any parent or after any child. Direct
    relations suffice: an indirect ancestor inside the moved range implies a
    direct parent inside it too. Returns [(key, "parent"|"child")].
    """
    if position not in POSITIONS:
        raise NXToolError("NX_INVALID_ARGUMENT", "position must be before or after")
    if moved == target:
        raise NXToolError("NX_INVALID_ARGUMENT", "feature and target must differ")
    i, j = order.index(moved), order.index(target)
    if i < j:  # Moving later: it passes the features between it and its new slot.
        passed = order[i + 1 : j + (1 if position == "after" else 0)]
        return [(k, "child") for k in passed if k in children.get(moved, set())]
    passed = order[j + (1 if position == "after" else 0) : i]
    return [(k, "parent") for k in passed if k in parents.get(moved, set())]


def child_map(features):
    """{parent tag: {child tag}} from every feature's GetParents and GetChildren.

    NX 2406 Feature.GetChildren is one-sided: a block extrude reports no
    children although a boss extrude united into its body and a hole cutting it
    both list the block in GetParents. Children are therefore derived from the
    parents of every feature in the part, plus whatever GetChildren reports.
    """
    children = {}
    for f in features:
        tag = int(f.Tag)
        for p in f.GetParents():
            children.setdefault(int(p.Tag), set()).add(tag)
        for c in f.GetChildren():
            children.setdefault(tag, set()).add(int(c.Tag))
    return children


def descendants(children, roots):
    """All tags reachable from roots through the child map (roots excluded)."""
    seen, stack, out = set(roots), list(roots), []
    while stack:
        for tag in sorted(children.get(stack.pop(), ())):
            if tag not in seen:
                seen.add(tag)
                stack.append(tag)
                out.append(tag)
    return out


def expected_order(order, moved, target, position):
    """Timeline order after moving `moved` before/after `target`."""
    rest = [k for k in order if k != moved]
    at = rest.index(target) + (1 if position == "after" else 0)
    return rest[:at] + [moved] + rest[at:]


class FeatureTreeMixin:
    # ---- helpers --------------------------------------------------------
    def _ft_feature(self, ref):
        feature = self._resolve(ref, {"feature"})
        if getattr(feature, "IsOccurrence", False) or feature.OwningPart != self._work_part():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Select features owned by the work part")
        if self._ft_internal(feature):
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                "Internal features (a sketch's datum system) follow their owner; use the owner",
            )
        return feature

    @staticmethod
    def _ft_internal(feature):
        return bool(getattr(feature, "IsInternal", False))

    def _ft_order(self, part):
        """User-visible features in timeline order.

        Timestamps are not unique (a sketch shares its stamp with its internal
        datum CSYS), so the stable sort keeps NX's collection order for ties.
        Internal features (sketch datum systems) are omitted.
        """
        features = list(part.Features)
        if features and all(hasattr(f, "Timestamp") for f in features):
            features.sort(key=lambda f: int(f.Timestamp))
        return [f for f in features if not self._ft_internal(f)]

    def _ft_suppress_code(self):
        """UF_MODL_ask_suppress_feature, or None when the UF seam is unavailable.

        NX 2406 returns a bit mask: 1 = suppressed by the user, 16 = inactive
        because the model is rolled back to an earlier current feature. The
        NXOpen Suppressed property is True in both cases.
        """
        try:
            import NXOpen.UF
        except ImportError:
            return None
        modeling = getattr(NXOpen.UF.UFSession.GetUFSession(), "Modeling", None)
        return getattr(modeling, "AskSuppressFeature", None)

    def _ft_states(self, part):
        """{tag: (feature, suppressed, active)} in timeline order."""
        order = self._ft_order(part)
        current = getattr(part, "CurrentFeature", None)
        tags = [int(f.Tag) for f in order]
        cut = (
            tags.index(int(current.Tag))
            if current is not None and int(current.Tag) in tags
            else None
        )
        ask = self._ft_suppress_code()
        states = {}
        for i, f in enumerate(order):
            active = cut is None or i <= cut
            if ask is not None:
                code = int(ask(f.Tag))
                suppressed = bool(code & 1)
                active = active and not code & 16
            else:
                suppressed = bool(getattr(f, "Suppressed", False)) and active
            states[int(f.Tag)] = (f, suppressed, active)
        return states

    def _ft_current(self, part, states):
        """Current feature: NX's Part.CurrentFeature, else the last active one."""
        current = getattr(part, "CurrentFeature", None)
        if current is not None:
            return current
        active = [f for f, _, a in states.values() if a]
        return active[-1] if active else None

    def _ft_entry(self, feature, part, suppressed=None, active=None, index=None):
        entry = {
            "feature": self._reference(feature, "feature", part, "Feature"),
            "name": self._name(feature, "Feature"),
            "type": str(getattr(feature, "FeatureType", "")),
        }
        if index is not None:
            entry["index"] = index
        if suppressed is not None:
            entry["suppressed"] = suppressed
        if active is not None:
            entry["active"] = active
        return entry

    def _ft_timeline(self, part, states):
        return [
            self._ft_entry(f, part, suppressed=s, active=a, index=i)
            for i, (f, s, a) in enumerate(states.values())
        ]

    def _ft_bodies(self, part):
        return [self._reference(b, "body", part, "Body") for b in part.Bodies]

    def _ft_result(self, part, states, **extra):
        return {
            **extra,
            "timeline": self._ft_timeline(part, states),
            "feature_count": len(states),
            "bodies": self._ft_bodies(part),
            "warnings": [
                "Face and edge IDs were invalidated; reacquire topology (nx_list_topology) "
                "before selecting geometry."
            ],
        }

    # ---- suppress / unsuppress -------------------------------------------
    def _suppress_features(self, features, suppress):
        ids = _id_list(features, "features")
        if not isinstance(suppress, bool):
            raise NXToolError("NX_INVALID_ARGUMENT", "suppress must be true or false")
        part = self._work_part()
        targets = [self._ft_feature(ref) for ref in ids]
        if len({int(f.Tag) for f in targets}) != len(targets):
            raise NXToolError("NX_INVALID_ARGUMENT", "features resolve to the same feature twice")
        before = self._ft_states(part)
        # NX 2406 sets the flag of a feature after the current feature but does
        # not re-evaluate it when the model is rolled forward again (verified
        # live: a hole suppressed while inactive was still cut after making the
        # last feature current), so suppression changes need active features.
        inactive = [f for f in targets if not before[int(f.Tag)][2]]
        if inactive:
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                "Features after the current feature are inactive; make the last feature "
                "current (nx_make_current_feature without feature) before suppressing or "
                "unsuppressing them",
                details={"inactive": [self._ft_entry(f, part) for f in inactive]},
            )
        pending = [f for f in targets if before[int(f.Tag)][1] != suppress]
        unchanged = [f for f in targets if before[int(f.Tag)][1] == suppress]
        collection = part.Features
        if pending:
            if suppress:
                self._require_api(collection, "SuppressFeatures")
                collection.SuppressFeatures(pending)
            else:
                self._require_api(collection, "UnsuppressFeatures")
                failed = list(collection.UnsuppressFeatures(pending) or [])
                if failed:
                    raise NXToolError(
                        "NX_VERIFICATION_FAILED",
                        "NX could not unsuppress some features",
                        details={"features": [self._ft_entry(f, part) for f in failed]},
                    )
            self._update_model()
        after = self._ft_states(part)
        wrong = [f for f in targets if after.get(int(f.Tag), (None, not suppress))[1] != suppress]
        if wrong:
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "NX left features in the wrong suppression state",
                details={"features": [self._ft_entry(f, part) for f in wrong]},
            )
        changed = [
            {
                **self._ft_entry(f, part),
                "requested": any(int(t.Tag) == tag for t in targets),
                "before": {"suppressed": before[tag][1]},
                "after": {"suppressed": s},
            }
            for tag, (f, s, _) in after.items()
            if tag in before and before[tag][1] != s
        ]
        # NX unsuppresses only the listed features: dependents it suppressed
        # together with a parent stay suppressed until they are listed too.
        dependents = []
        if not suppress:
            children = child_map(list(part.Features))
            for tag in descendants(children, [int(t.Tag) for t in targets]):
                if tag in after and after[tag][1]:
                    dependents.append(self._ft_entry(after[tag][0], part, suppressed=True))
        return self._ft_result(
            part,
            after,
            suppress=suppress,
            changed=changed,
            still_suppressed_dependents=dependents,
            changed_count=len(changed),
            unchanged=[self._ft_entry(f, part, suppressed=suppress) for f in unchanged],
            modified=[c["feature"] for c in changed],
            message=(
                f"{'Suppressed' if suppress else 'Unsuppressed'} {len(changed)} feature(s)"
                if changed
                else "All requested features were already in the requested state"
            ),
        )

    # ---- make current -----------------------------------------------------
    def _make_current_feature(self, feature=None):
        if feature is not None:
            _id(feature, "feature")
        part = self._work_part()
        order = self._ft_order(part)
        if not order:
            raise NXToolError("NX_NOT_FOUND", "The work part has no features")
        chosen = self._ft_feature(feature) if feature is not None else order[-1]
        self._require_api(chosen, "MakeCurrentFeature")
        before = self._ft_states(part)
        chosen.MakeCurrentFeature()
        self._update_model()
        after = self._ft_states(part)
        current = self._ft_current(part, after)
        if current is None or int(current.Tag) != int(chosen.Tag):
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "NX made a different feature current",
                details={"current": current and self._ft_entry(current, part)},
            )
        inactive = [self._ft_entry(f, part, suppressed=s) for f, s, a in after.values() if not a]
        changed = [
            self._ft_entry(f, part, active=a)
            for tag, (f, _, a) in after.items()
            if tag in before and before[tag][2] != a
        ]
        return self._ft_result(
            part,
            after,
            current=self._ft_entry(
                chosen,
                part,
                suppressed=after[int(chosen.Tag)][1],
                index=list(after).index(int(chosen.Tag)),
            ),
            inactive=inactive,
            inactive_count=len(inactive),
            activity_changed=changed,
            modified=[c["feature"] for c in changed],
            message=(
                f"Current feature: {self._name(chosen, 'Feature')}; "
                f"{len(inactive)} later feature(s) inactive"
            ),
        )

    # ---- reorder ----------------------------------------------------------
    def _reorder_feature(self, feature, target, position):
        _id(feature, "feature")
        _id(target, "target")
        if position not in POSITIONS:
            raise NXToolError("NX_INVALID_ARGUMENT", "position must be before or after")
        part = self._work_part()
        moved, anchor = self._ft_feature(feature), self._ft_feature(target)
        if int(moved.Tag) == int(anchor.Tag):
            raise NXToolError("NX_INVALID_ARGUMENT", "feature and target must differ")
        before = self._ft_states(part)
        order = list(before)
        key = int(moved.Tag)
        relations = {
            "parents": {key: {int(p.Tag) for p in moved.GetParents()}},
            "children": {key: child_map(list(part.Features)).get(key, set())},
        }
        conflicts = reorder_conflicts(
            order, relations["parents"], relations["children"], key, int(anchor.Tag), position
        )
        if conflicts:
            raise NXToolError(
                "NX_REORDER_DEPENDENCY",
                f"Cannot move {self._name(moved, 'Feature')} {position} "
                f"{self._name(anchor, 'Feature')}: it would precede a parent or follow a child",
                details={
                    "conflicts": [
                        {**self._ft_entry(before[k][0], part), "relation": relation}
                        for k, relation in conflicts
                    ]
                },
            )
        wanted = expected_order(order, key, int(anchor.Tag), position)
        if wanted == order:
            return self._ft_result(
                part,
                before,
                feature=self._ft_entry(moved, part, index=order.index(key)),
                timeline_before=self._ft_timeline(part, before),
                moved=False,
                modified=[],
                message="Feature is already in the requested position",
            )
        reorder = self.nxopen.Features.FeatureCollection.ReorderType
        self._require_api(part.Features, "ReorderFeature")
        try:
            part.Features.ReorderFeature(
                [moved], anchor, reorder.Before if position == "before" else reorder.After
            )
        except Exception as error:
            raise NXToolError(
                "NX_REORDER_REJECTED",
                "NX refused the reorder: " + str(error),
                nx_code=getattr(error, "ErrorCode", None),
            ) from error
        self._update_model()
        after = self._ft_states(part)
        if list(after) != wanted:
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "NX timeline order differs from the requested move",
                details={"timeline": self._ft_timeline(part, after)},
            )
        return self._ft_result(
            part,
            after,
            feature=self._ft_entry(moved, part, index=wanted.index(key)),
            target=self._ft_entry(anchor, part, index=wanted.index(int(anchor.Tag))),
            position=position,
            moved=True,
            timeline_before=self._ft_timeline(part, before),
            modified=[self._reference(moved, "feature", part, "Feature")],
            message=(
                f"Moved {self._name(moved, 'Feature')} {position} {self._name(anchor, 'Feature')}"
            ),
        )
