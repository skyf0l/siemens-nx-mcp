"""String user attributes as NXOpen objects hold them, for fakes that carry records.

``UserAttributes`` keeps one store, ``attributes`` (title -> string), behind both the
NX 8+ calls (``SetUserAttribute``, ``HasUserAttribute``, ``GetUserAttributeAsString``,
``GetStringUserAttribute``, ``DeleteUserAttribute``) and the deprecated ones
(``SetAttribute``, ``GetStringAttribute``, ``DeleteAttributeByTypeAndTitle``), so a
record written one way reads back the other. ``attribute_updates`` logs ``(call,
title, option)`` for every write through the NX 8+ calls.
"""

from __future__ import annotations

from tests.kit.objects import NXException

NOT_FOUND = 1  # a native error, as NX raises for a missing attribute


class UserAttributes:
    """Mixin: string user attributes (scalars only, index -1)."""

    def _store(self) -> dict:
        return self.__dict__.setdefault("attributes", {})

    def _log(self, call, title, option):
        self.__dict__.setdefault("attribute_updates", []).append((call, title, option))

    def _value(self, title):
        try:
            return self._store()[title]
        except KeyError:
            raise NXException(NOT_FOUND, f"Attribute {title} not found") from None

    # ---- NX 8+ ----------------------------------------------------------------------
    def HasUserAttribute(self, title, _type=None, _index=-1):
        return title in self._store()

    def GetUserAttributeAsString(self, title, _type, index):
        assert index == -1, "records are scalars"
        return self._value(title)

    def GetStringUserAttribute(self, title, index):
        assert index == -1, "records are scalars"
        return self._value(title)

    def SetUserAttribute(self, title, index, value, option):
        assert index == -1, "records are scalars"
        assert isinstance(value, str), "string attribute overload"
        self._store()[title] = value
        self._log("SetUserAttribute", title, option)

    def DeleteUserAttribute(self, _type, title, _entire_array, option):
        self._store().pop(title, None)
        self._log("DeleteUserAttribute", title, option)

    # ---- deprecated since NX 8 --------------------------------------------------------
    def SetAttribute(self, title, value):
        self._store()[title] = value

    def GetStringAttribute(self, title):
        return self._value(title)

    def DeleteAttributeByTypeAndTitle(self, _type, title):
        self._store().pop(title, None)


class OldApiAttributes:
    """Mixin: only the deprecated calls (plus ``HasUserAttribute``), as on an object
    whose NX build lacks the NX 8+ user-attribute calls."""

    def _store(self) -> dict:
        return self.__dict__.setdefault("attributes", {})

    def HasUserAttribute(self, title, _type=None, _index=-1):
        return title in self._store()

    def SetAttribute(self, title, value):
        self._store()[title] = value

    def GetStringAttribute(self, title):
        try:
            return self._store()[title]
        except KeyError:
            raise NXException(NOT_FOUND, f"Attribute {title} not found") from None

    def DeleteAttributeByTypeAndTitle(self, _type, title):
        self._store().pop(title, None)
