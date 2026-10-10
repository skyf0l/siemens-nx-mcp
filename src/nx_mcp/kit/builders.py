"""NX builders: create -> configure -> commit -> ``Destroy()``, written once.

    with builders.build(tx, tx.part.Features.CreateShellBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx)
        b.option("AllowPartialDelete", False)      # absent on NX 2406: skipped
        feature = b.commit()

``b`` forwards attribute reads and writes to the native builder (``b.native``).
Creating, configuring and committing run under ``core.errors.native`` with
``default="NX_BUILD_FAILED"``; ``Destroy()`` always runs, and a failing
``Destroy()`` never hides the error that is already propagating.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import facts, geom, units

_OWN = ("native", "committed", "_codes")


class Builder:
    """A native builder handle; unknown attributes go to the native builder."""

    __slots__ = _OWN

    def __init__(self, native_builder, codes=None):
        object.__setattr__(self, "native", native_builder)
        object.__setattr__(self, "committed", None)
        object.__setattr__(self, "_codes", codes)

    def __getattr__(self, name):
        return getattr(self.native, name)

    def __setattr__(self, name, value):
        if name in _OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.native, name, value)

    def has(self, name):
        """Whether this NX build's builder exposes ``name``."""
        return hasattr(self.native, name)

    def option(self, name, value):
        """Set an option only where this NX build has it; returns whether it was set.

        NX 2406 lacks some options newer builds offer (``AllowPartialDelete`` on
        Delete Face), and their absence must not fail the call.
        """
        if not hasattr(self.native, name):
            return False
        setattr(self.native, name, value)
        return True

    def commit(self, validate=False):
        """``CommitFeature()`` when the builder has it, else ``Commit()``.

        With ``validate`` a builder whose ``Validate()`` returns False fails as
        ``NX_BUILD_FAILED`` before committing.
        """
        b = self.native
        with native(self._codes, "NX_BUILD_FAILED"):
            if validate and hasattr(b, "Validate") and not b.Validate():
                fail("NX_BUILD_FAILED", "NX rejected the feature inputs")
            commit = getattr(b, "CommitFeature", None) or b.Commit
            result = commit()
        object.__setattr__(self, "committed", result)
        return result


@contextlib.contextmanager
def build(tx, factory, *args, codes=None):
    """Create a builder with ``factory(*args)``, yield a ``Builder``, always destroy it.

    ``codes`` maps native error codes to stable codes for this builder;
    unmapped native refusals become ``NX_BUILD_FAILED``. ``tx`` is accepted
    for symmetry with the other kit calls (NX is reached via ``factory``).
    """
    with native(codes, "NX_BUILD_FAILED"):
        native_builder = factory(*args)
    handle = Builder(native_builder, codes)
    try:
        with native(codes, "NX_BUILD_FAILED"):
            yield handle
    except BaseException:
        with contextlib.suppress(Exception):
            native_builder.Destroy()
        raise
    native_builder.Destroy()


def destroy(*builders):
    """Destroy native builders, ignoring ones that are None; first error re-raised."""
    first = None
    for b in builders:
        if b is None:
            continue
        try:
            b.Destroy()
        except Exception as error:  # keep destroying the rest
            first = first or error
    if first is not None:
        raise first


def point(tx, p):
    """A smart point at work-part coordinates ``p``."""
    x, y, z = (float(v) for v in p)
    return tx.part.Points.CreatePoint(tx.nx.Point3d(x, y, z))


def direction(tx, v, origin=(0.0, 0.0, 0.0), field="direction"):
    """A smart direction along unit(``v``) through ``origin`` (updates within modeling)."""
    u = geom.unit(v, field)
    return tx.part.Directions.CreateDirection(
        tx.nx.Point3d(*(float(c) for c in origin)),
        tx.nx.Vector3d(*u),
        tx.nx.SmartObject.UpdateOption.WithinModeling,
    )


def collector(tx, objects, kind):
    """A selection collector over ``objects`` with one dumb rule.

    ``kind`` is ``"Edge"``, ``"Face"`` or ``"Body"``; ``objects`` are live NX
    objects or references (resolved through ``tx.resolve``).
    """
    return fill(tx, tx.part.ScCollectors.CreateCollector(), objects, kind)


def fill(tx, collector, objects, kind):
    """Replace the rules of an existing ``collector`` (a builder's own collector)
    with one dumb rule over ``objects``; returns the collector.

    ``kind`` is ``"Edge"``, ``"Face"`` or ``"Body"``; ``objects`` are live NX
    objects or references (resolved through ``tx.resolve``).
    """
    if kind not in ("Edge", "Face", "Body"):
        raise ValueError("collector kind must be Edge, Face or Body")
    items = [tx.resolve(o, kind.lower()) if isinstance(o, str) else o for o in objects]
    rule = getattr(tx.part.ScRuleFactory, "CreateRule" + kind + "Dumb")(items)
    collector.ReplaceRules([rule], False)
    return collector


def section(tx, sketch, into=None):
    """A section over every curve of a sketch feature (profile of extrude/revolve).

    ``into`` fills a builder's own section (rib, tube) instead of a new one.
    """
    part = tx.part
    result = part.Sections.CreateSection() if into is None else into
    options = part.ScRuleFactory.CreateRuleOptions()
    try:
        rule = part.ScRuleFactory.CreateRuleCurveFeature([sketch.Feature], None, options)
    finally:
        options.Dispose()
    result.AddToSection([rule], None, None, None, sketch.Origin, tx.nx.Section.Mode.Create, False)
    return result


BOOLEAN_TYPES = {"unite": "Unite", "subtract": "Subtract", "intersect": "Intersect"}


def boolean(tx, operation, combine, field="/combine"):
    """Configure a feature builder's ``BooleanOperation`` from a ``Combine`` argument.

    ``combine`` is ``None`` (a new body) or ``{"operation", "targets"}`` with
    target body references owned by the work part. Returns the target bodies.
    """
    from nx_mcp.kit import select

    types = tx.nx.GeometricUtilities.BooleanOperation.BooleanType
    if combine is None:
        operation.Type = types.Create
        return []
    targets = select.bodies(tx, combine["targets"], field + "/targets")
    operation.Type = getattr(types, BOOLEAN_TYPES[combine["operation"]])
    operation.SetTargetBodies(targets)
    return targets


def matrix(tx, r):
    """An NX ``Matrix3x3`` for a row-major rotation R (p_abs = R p_local): the matrix rows
    X, Y, Z are the columns of R (the inverse of ``geom.rows``)."""
    m = tx.nx.Matrix3x3()
    for i, prefix in enumerate("XYZ"):
        for j, suffix in enumerate("xyz"):
            setattr(m, prefix + suffix, float(r[j][i]))
    return m


def fixed_plane(tx, origin, normal):
    """A fixed (non-feature) datum plane through ``origin`` with unit ``normal``."""
    x, y, z = geom.frame_from_normal(normal)
    rows = [[x[i], y[i], z[i]] for i in range(3)]  # columns are the plane's axes
    return tx.part.Datums.CreateFixedDatumPlane(
        tx.nx.Point3d(*(float(v) for v in origin)), matrix(tx, rows)
    )


def curve_section(tx, objects, start=None, into=None):
    """A section over curves and edges (dumb rules) for loft, mesh, trim and bridge inputs.

    ``start`` (a work-part point) picks the section's start: NX chains from the end
    nearest it. ``into`` fills a builder's own section instead of a new one. Distance and chaining tolerances are 0.001 mm in part units, so
    inch parts scale; the angle tolerance is 0.5 degrees.
    """
    objects = list(objects)
    if not objects:
        fail("NX_INVALID_ARGUMENT", "A section needs at least one curve")
    part = tx.part
    tolerance = units.part_tolerance(tx)
    if into is None:
        result = part.Sections.CreateSection(tolerance, tolerance, 0.5)
    else:
        result = into
        result.DistanceTolerance = result.ChainingTolerance = tolerance
        result.AngleTolerance = 0.5
    result.SetAllowedEntityTypes(tx.nx.Section.AllowTypes.OnlyCurves)
    edges = [o for o in objects if facts.is_edge(o)]
    curves = [o for o in objects if not facts.is_edge(o)]
    rules = []
    if curves:
        rules.append(part.ScRuleFactory.CreateRuleCurveDumb(curves))
    if edges:
        rules.append(part.ScRuleFactory.CreateRuleEdgeDumb(edges))
    help_point = tx.nx.Point3d(*(float(v) for v in (start or (0.0, 0.0, 0.0))))
    result.AddToSection(rules, objects[0], None, None, help_point, tx.nx.Section.Mode.Create, False)
    return result
