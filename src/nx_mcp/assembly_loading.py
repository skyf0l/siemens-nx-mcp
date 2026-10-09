"""Read assembly load state without assuming an occurrence has a loaded Part."""

from nx_mcp.runtime import NXToolError


def component_info(component):
    prototype = component.Prototype
    path = getattr(prototype, "FullPath", None)
    loaded = bool(path)
    result = {
        "part_path": path or None,
        "load_state": "fully_loaded"
        if loaded and getattr(prototype, "IsFullyLoaded", True)
        else "partially_loaded"
        if loaded
        else "unloaded",
        "prototype_type": type(prototype).__name__ if prototype is not None else None,
    }
    if not loaded:
        import NXOpen.UF as U

        try:
            path = U.UFSession.GetUFSession().Assem.AskComponentData(component.Tag)[0]
            result["part_path"] = path or None
        except Exception as error:
            result["load_diagnostic"] = str(error)
    return result


def require_loaded(executor, part):
    incomplete = []
    for component, path in executor._walk_components(part):
        if component.IsSuppressed:
            continue
        info = component_info(component)
        if info["load_state"] != "fully_loaded":
            incomplete.append({"occurrence_path": path, **info})
    if incomplete:
        raise NXToolError(
            "NX_ASSEMBLY_NOT_LOADED",
            "Load assembly components before updating or exporting drawings",
            details={"components": incomplete, "mutation_outcome": "not_started"},
        )
