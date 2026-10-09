"""NX-side runtime: ``dispatch`` (bridge call -> catalogue kernel) and ``services``
(``NXServices``, the kernel's session state). The interactive host and the batch
bridge create one ``NXServices`` each. Modules in this package receive NXOpen
through the services and never import it at module level.
"""
