"""Typed-reference replacements for legacy NX 2606 entry points."""

from __future__ import annotations


class LegacyRepairsMixin:
    def _delete_feature(self, name):
        feature = self._resolve(name, {"feature"})
        ref = self._reference(feature, "feature", self._work_part(), "Feature")
        manager = self.session.UpdateManager
        self._require_api(manager, "AddToDeleteList", "DoUpdate")
        manager.AddToDeleteList(feature)
        self._update_model()
        return {"deleted": [ref]}
