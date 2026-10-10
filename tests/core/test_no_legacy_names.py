"""The migration to the catalogue and kernel is finished: no "legacy" layer is left.

The package, its tests and the design docs read as one design, so the word does not
appear in ``src/nx_mcp``, ``tests`` or ``docs/design`` (``docs/history`` keeps the
record). Binary files such as ``.prt`` fixtures are not scanned. An exception needs a
written reason here.
"""

import re
from pathlib import Path

import nx_mcp

PACKAGE = Path(nx_mcp.__file__).parent
REPO = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()
WORD = re.compile("legacy", re.IGNORECASE)
# Tokens that contain the word but are not ours: name -> reason.
ALLOWED_TOKENS = {
    "UsesLegacySolver": "NXOpen API property (NXOpen.Sketch), cited in NX findings and "
    "mirrored by the sketch fakes",
}
# Package files that may still contain the word: path relative to src/nx_mcp -> reason.
ALLOWED_FILES = {}


def _mentions(root: Path, allowed=()) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".prt"):
            continue
        if path.resolve() == THIS:
            continue
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for token in ALLOWED_TOKENS:
                line = line.replace(token, "")
            if WORD.search(line):
                found.append(f"{root.name}/{rel}:{number}: {line.strip()}")
    return found


def test_src_never_mentions_legacy():
    found = _mentions(PACKAGE, ALLOWED_FILES)
    assert not found, "Say what the code is, not what it replaced:\n" + "\n".join(found)


def test_tests_and_design_docs_never_mention_legacy():
    found = _mentions(REPO / "tests") + _mentions(REPO / "docs" / "design")
    assert not found, "Describe today's behaviour, not what it replaced:\n" + "\n".join(found)


def test_allowed_files_still_need_their_entry():
    stale = [
        rel for rel in ALLOWED_FILES if not WORD.search((PACKAGE / rel).read_text(encoding="utf-8"))
    ]
    assert not stale, f"Remove these entries from ALLOWED_FILES: {stale}"
