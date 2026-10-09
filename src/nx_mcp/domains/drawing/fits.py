"""ISO 286 limits and fits: tolerance classes such as ``H7`` or ``g6`` to deviations.

NX-free. ``nx_edit_dimension(tolerance={"type": "fit", ...})`` lets NX compute the
limits from its own tables and checks them against these: the standard tolerance
grades IT5-IT11 and the fundamental deviations of ISO 286-1 for nominal sizes up to
500 mm, holes D E F G H JS K M N P and shafts d e f g h js k m n p (K, M, N up to
grade 8 and P up to grade 7, where the delta rule applies). Other classes are refused
before NX. Deviations are in micrometres.
"""

from __future__ import annotations

import re

# Upper limits of the nominal size ranges (a size on a limit belongs to the lower range).
RANGES = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)

# Standard tolerance grades (um) per size range.
IT = {
    4: (3, 4, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20),
    5: (4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27),
    6: (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40),
    7: (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63),
    8: (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97),
    9: (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155),
    10: (40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400),
}
GRADES = (5, 11)

# Shaft fundamental deviations (um): upper deviation es for d-h, lower ei for k-p.
SHAFT_UPPER = {
    "d": (-20, -30, -40, -50, -65, -80, -100, -120, -145, -170, -190, -210, -230),
    "e": (-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135),
    "f": (-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68),
    "g": (-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20),
    "h": (0,) * 13,
}
SHAFT_LOWER = {
    "k": (0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5),  # grades 4-7; 0 otherwise
    "m": (2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23),
    "n": (4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40),
    "p": (6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68),
}
# Holes K-P with the delta rule: ES = -ei(shaft) + delta, delta = IT(n) - IT(n-1).
HOLE_DELTA_MAX = {"K": 8, "M": 8, "N": 8, "P": 7}

CLASS = re.compile(r"^(JS|js|[A-Za-z])(\d{1,2})$")


def size_range(size_mm):
    """Index of the ISO 286 size range of a nominal size in millimetres, or None."""
    if not 0.0 < size_mm <= RANGES[-1] + 1e-9:
        return None
    return next(i for i, top in enumerate(RANGES) if size_mm <= top + 1e-9)


def parse(text, role):
    """``("H", 7)`` from ``"H7"``; ``role`` hole needs upper case, shaft lower case.

    Returns an error message instead of a tuple when the class is not supported."""
    match = CLASS.match(text or "")
    if not match:
        return f"{text!r} is not a tolerance class such as {'H7' if role == 'hole' else 'g6'}"
    letter, grade = match.group(1), int(match.group(2))
    if role == "hole" and letter != letter.upper():
        return f"A hole class uses an upper-case letter ({letter.upper()}{grade})"
    if role == "shaft" and letter != letter.lower():
        return f"A shaft class uses a lower-case letter ({letter.lower()}{grade})"
    known = (
        set(SHAFT_UPPER) | set(SHAFT_LOWER) | {"js"}
        if role == "shaft"
        else {k.upper() for k in SHAFT_UPPER} | set(HOLE_DELTA_MAX) | {"JS"}
    )
    if letter not in known:
        return f"{letter}{grade}: deviation {letter} is not supported (use {sorted(known)})"
    if not GRADES[0] <= grade <= GRADES[1]:
        return f"{letter}{grade}: grades IT{GRADES[0]}-IT{GRADES[1]} are supported"
    if role == "hole" and grade > HOLE_DELTA_MAX.get(letter, 99):
        return f"{letter}{grade}: {letter} is supported up to grade {HOLE_DELTA_MAX[letter]}"
    return letter, grade


def _js(it, grade):
    """Half the grade. ISO 286-1 allows rounding odd IT7-IT11 values down to the even
    micrometre; NX 2406 does not (JS7 at 10 mm is +/-7.5 um), and neither does this."""
    return it / 2.0


def shaft(letter, grade, size_mm):
    """``(upper, lower)`` deviations in micrometres of a shaft class at a nominal size."""
    r = size_range(size_mm)
    it = IT[grade][r]
    if letter == "js":
        half = _js(it, grade)
        return half, -half
    if letter in SHAFT_UPPER:
        es = SHAFT_UPPER[letter][r]
        return float(es), float(es - it)
    ei = SHAFT_LOWER[letter][r]
    if letter == "k" and not 4 <= grade <= 7:
        ei = 0
    return float(ei + it), float(ei)


def hole(letter, grade, size_mm):
    """``(upper, lower)`` deviations in micrometres of a hole class at a nominal size."""
    r = size_range(size_mm)
    it = IT[grade][r]
    if letter == "JS":
        half = _js(it, grade)
        return half, -half
    if letter.lower() in SHAFT_UPPER:
        ei = -SHAFT_UPPER[letter.lower()][r]
        return float(ei + it), float(ei)
    delta = it - IT[grade - 1][r]
    es = -SHAFT_LOWER[letter.lower()][r] + delta
    return float(es), float(es - it)


def deviations(role, text, size_mm):
    """Deviations (um) of a supported class; a string explains a refusal."""
    parsed = parse(text, role)
    if isinstance(parsed, str):
        return parsed
    if size_range(size_mm) is None:
        return f"ISO 286 fits are supported for nominal sizes over 0 up to {RANGES[-1]} mm"
    return (hole if role == "hole" else shaft)(*parsed, size_mm)
