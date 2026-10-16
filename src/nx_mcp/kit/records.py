"""Private records on NX objects: text or JSON kept in a string user attribute.

    records.set_json(tx, view, DETAIL, {"parent": handle, "radius": 5.0})
    records.get_json(tx, view, DETAIL)              # the decoded value, or None
    records.set_text(tx, view, CONSTRUCTION, "hide")
    records.get_text(tx, view, CONSTRUCTION, "")    # the string, or the default
    records.has(tx, view, CONSTRUCTION)
    records.delete(tx, view, CONSTRUCTION)

Built on the NX 8+ user-attribute calls of ``NXObject``: ``SetUserAttribute(title, -1,
value, option)``, ``HasUserAttribute(title, String, -1)``,
``GetUserAttributeAsString(title, String, -1)`` and ``DeleteUserAttribute(String,
title, True, option)``. Records the deprecated ``SetAttribute(title, value)`` wrote are
the same string user attributes, so records already saved in parts read back
unchanged, and the titles and stored strings are the ones those calls used.

Update option: the deprecated two-argument ``SetAttribute`` and
``DeleteAttributeByTypeAndTitle`` performed an immediate update, except on a
``Features.Feature`` (NXOpen reference). These helpers pass ``Update.Option.Now``, or
``Update.Option.Later`` for a feature, so NX does exactly what it did before; callers
that relied on that update (a drafting view drawn right after its record is set)
keep working.

Objects without the user-attribute calls fall back to ``SetAttribute``,
``GetStringAttribute`` and ``DeleteAttributeByTypeAndTitle``.
"""

from __future__ import annotations

import json
from typing import Any

_INDEX = -1  # a single value, not an array element


def _string(tx):
    return tx.nx.NXObject.AttributeType.String


def _option(tx, obj):
    """``Later`` for a feature, else ``Now``: the update the deprecated calls made."""
    feature = getattr(getattr(tx.nx, "Features", None), "Feature", None)
    later = isinstance(feature, type) and isinstance(obj, feature)
    return tx.nx.Update.Option.Later if later else tx.nx.Update.Option.Now


def has(tx, obj, title) -> bool:
    """Whether ``obj`` carries the string attribute ``title``."""
    return bool(obj.HasUserAttribute(title, _string(tx), _INDEX))


def get_text(tx, obj, title, default=None):
    """The string attribute ``title`` of ``obj``, or ``default`` when it has none."""
    if not has(tx, obj, title):
        return default
    read = getattr(obj, "GetUserAttributeAsString", None)
    if read is None:
        return str(obj.GetStringAttribute(title))
    return str(read(title, _string(tx), _INDEX))


def set_text(tx, obj, title, value: str) -> None:
    """Create or replace the string attribute ``title`` of ``obj``."""
    write = getattr(obj, "SetUserAttribute", None)
    if write is None:
        obj.SetAttribute(title, str(value))
    else:
        write(title, _INDEX, str(value), _option(tx, obj))


def get_json(tx, obj, title, default: Any = None) -> Any:
    """The JSON record ``title`` of ``obj`` decoded, or ``default`` when it has none."""
    text = get_text(tx, obj, title)
    return default if text is None else json.loads(text)


def set_json(tx, obj, title, value, *, sort_keys=False) -> str:
    """Store ``value`` as the JSON record ``title`` of ``obj``; returns the stored text."""
    text = json.dumps(value, sort_keys=sort_keys)
    set_text(tx, obj, title, text)
    return text


def delete(tx, obj, title) -> bool:
    """Remove the string attribute ``title`` of ``obj``; returns whether it was there."""
    if not has(tx, obj, title):
        return False
    remove = getattr(obj, "DeleteUserAttribute", None)
    if remove is None:
        obj.DeleteAttributeByTypeAndTitle(_string(tx), title)
    else:
        remove(_string(tx), title, True, _option(tx, obj))
    return True
