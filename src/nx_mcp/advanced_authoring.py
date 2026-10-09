"""NX 2606 native patterns, geometric rules and constrained parameter editing."""

from __future__ import annotations


class AdvancedAuthoringMixin:
    def _component_patterns(self, part):
        assembly = part.ComponentAssembly
        if not assembly.RootComponent or not hasattr(assembly, "ComponentPatterns"):
            return []
        self._require_api(assembly.ComponentPatterns, "GetAllComponentPatterns")
        return list(assembly.ComponentPatterns.GetAllComponentPatterns())
