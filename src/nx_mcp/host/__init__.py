"""NX-side runtime: dispatch of catalogue tools and the services adapter.

During migration only ``dispatch`` and ``legacy_services`` live here; the
startup app, interactive host and panel move here in step F4. Modules in this
package receive NXOpen through the executor and never import it at module level.
"""
