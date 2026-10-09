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
from nx_mcp.kit import geom

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
    if kind not in ("Edge", "Face", "Body"):
        raise ValueError("collector kind must be Edge, Face or Body")
    items = [tx.resolve(o, kind.lower()) if isinstance(o, str) else o for o in objects]
    part = tx.part
    rule = getattr(part.ScRuleFactory, "CreateRule" + kind + "Dumb")(items)
    result = part.ScCollectors.CreateCollector()
    result.ReplaceRules([rule], False)
    return result


def section(tx, sketch):
    """A section over every curve of a sketch feature (profile of extrude/revolve)."""
    part = tx.part
    result = part.Sections.CreateSection()
    options = part.ScRuleFactory.CreateRuleOptions()
    try:
        rule = part.ScRuleFactory.CreateRuleCurveFeature([sketch.Feature], None, options)
    finally:
        options.Dispose()
    result.AddToSection([rule], None, None, None, sketch.Origin, tx.nx.Section.Mode.Create, False)
    return result
