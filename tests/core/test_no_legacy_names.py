"""The migration to the catalogue and kernel is finished: no "legacy" layer is left.

The package reads as one design, so the word does not appear in ``src/nx_mcp``.
An exception needs a written reason here.
"""

import re
from pathlib import Path

import nx_mcp

ROOT = Path(nx_mcp.__file__).parent
WORD = re.compile("legacy", re.IGNORECASE)
# Tokens that contain the word but are not ours: name -> reason.
ALLOWED_TOKENS = {
    "UsesLegacySolver": "NXOpen API property (NXOpen.Sketch), cited in NX findings",
}
# Files that may still contain the word: relative path -> reason.
ALLOWED_FILES = {
    "kit/measure.py": "docstring wording; the file is being edited by another agent (remove "
    "this entry with that change)",
}


def test_src_never_mentions_legacy():
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for token in ALLOWED_TOKENS:
                line = line.replace(token, "")
            if WORD.search(line):
                found.append(f"{rel}:{number}: {line.strip()}")
    assert not found, "Say what the code is, not what it replaced:\n" + "\n".join(found)


def test_allowed_files_still_need_their_entry():
    stale = [
        rel for rel in ALLOWED_FILES if not WORD.search((ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert not stale, f"Remove these entries from ALLOWED_FILES: {stale}"
