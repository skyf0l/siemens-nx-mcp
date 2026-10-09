"""Shared NX know-how, written once (architecture.md section 1.4).

Modules: ``geom`` (pure vector math), ``args`` (argument rules and value
validators), ``units``, ``builders``, ``select``, ``sketch``, ``drawing``,
``expr``, ``readback`` and ``result``. Errors are raised only through
``nx_mcp.core.errors`` (``fail``/``native``).

Rules: stdlib only, and nothing from the package but ``nx_mcp.core``. NXOpen
is never imported here: NX is reached only through the objects passed in
(``tx.nx``, ``tx.uf``, ``tx.session``, ``tx.part`` and live NX objects), so
every module imports in a process without NX.
"""
