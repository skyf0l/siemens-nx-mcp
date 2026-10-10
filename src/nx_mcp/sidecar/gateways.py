"""The four gateway tools (tool-surface 2.1).

- ``nx_find_tools``: ranked search after simple stemming and synonym expansion. A
  word matching the name outranks keywords and the label, which outrank the
  description; a keyword or label phrase in the query and a word naming the
  tool's domain add to the score; an exact name wins. Tools matching every word
  come first; when none do, tools matching most words are returned, flagged
  ``partial``. An empty query returns a domain overview.
- ``nx_get_tool_schemas``: input schema, example, prerequisites and evidence for
  1-10 tools or variants (``nx_create_datum_plane#offset``).
- ``nx_invoke``: call any tool; ``intent`` is shown in the NX panel.
- ``nx_invoke_read``: the same for read tools only; others are refused before
  their arguments are validated, so hosts can auto-approve it.
"""

from __future__ import annotations

import difflib
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nx_mcp.core.catalogue import Domain, Effect
from nx_mcp.core.errors import NXError
from nx_mcp.sidecar import schema

NAMES = ("nx_find_tools", "nx_get_tool_schemas", "nx_invoke", "nx_invoke_read")
TOOL_NAME = r"^nx_[a-z0-9_]{1,80}$"
SUMMARY_CHARS = 48

# Words agents use for an NX command of another name. Keywords on each tool add
# its own synonyms; these cover terms shared by several tools.
SYNONYMS = {
    "fillet": ("blend",),
    "round": ("blend",),
    "pad": ("extrude",),
    "boss": ("extrude",),
    "protrude": ("extrude",),
    "mate": ("constraint",),
    "screenshot": ("capture", "screenshot"),
    "image": ("capture", "screenshot"),
    "remove": ("delete",),
    "rename": ("name",),
    "inventory": ("list",),
    "show": ("list", "get"),
}
WEIGHTS = {"name": 8, "keyword": 4, "variant": 4, "label": 4, "description": 1}
MISSING = 6  # score lost per query word a tool does not match (partial results)
PHRASE = 6  # bonus when a whole keyword or the label appears in the query
DOMAIN = 3  # bonus when a query word names the tool's domain
# The empty-query overview names these first in each domain (others: alphabetical).
KEY_TOOLS = {
    "assembly": ("nx_create_component", "nx_create_assembly_constraint", "nx_list_components"),
    "display": ("nx_capture_view", "nx_set_camera", "nx_set_appearance"),
    "drawing": (
        "nx_create_drawing_sheet",
        "nx_create_drawing_view",
        "nx_create_drawing_dimension",
    ),
    "freeform": ("nx_create_spline", "nx_create_bridge_surface", "nx_create_thicken"),
    "inspection": ("nx_list_objects", "nx_find_geometry", "nx_measure_mass"),
    "modeling": ("nx_create_extrude", "nx_create_revolve", "nx_create_blend", "nx_set_expression"),
    "parts_files": ("nx_create_part", "nx_open_part", "nx_save_part"),
    "session": ("nx_get_status", "nx_undo_operation", "nx_run_batch"),
    "sheet_metal": ("nx_create_sheet_metal_feature", "nx_export_flat_pattern"),
    "sketch": ("nx_create_sketch", "nx_create_sketch_curves", "nx_finish_sketch"),
}
# Words that carry no meaning in a tool search ("create a new part in the work part").
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "into",
        "from",
        "and",
        "or",
        "my",
        "this",
        "that",
        "it",
        "nx",
        "please",
        "using",
    ]
)
DOMAIN_WORDS = {
    "part": "parts_files",
    "file": "parts_files",
    "sketch": "sketch",
    "assembly": "assembly",
    "component": "assembly",
    "drawing": "drawing",
    "sheet": "sheet_metal",
    "inspect": "inspection",
    "display": "display",
    "view": "display",
    "session": "session",
    "freeform": "freeform",
    "surface": "freeform",
    "feature": "modeling",
    "model": "modeling",
}


def stem(word: str) -> str:
    """Lower-case and strip a plural or simple verb ending ("edges" -> "edge")."""
    word = word.casefold()
    for suffix, keep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", "e")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + keep
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", str(text).casefold())]


class Entry:
    """One searchable catalogue tool."""

    def __init__(self, name, *, effect, domain, label, description, keywords=()):
        self.name = name
        self.effect = effect
        self.domain = domain
        self.summary = _clip(label, SUMMARY_CHARS)
        self.description = description
        self.fields = {
            "name": set(words(name.removeprefix("nx_"))),
            "keyword": {w for k in keywords for w in words(k)},
            "label": set(words(label)),
            "description": set(words(description)),
        }
        # Multi-word keywords and the label, matched as phrases in the query.
        self.phrases = {" ".join(p) for p in (words(k) for k in (*keywords, label)) if len(p) > 1}

    def score(self, query: list[str], variants=()) -> int | None:
        """Sum of the best field weight per query word; None when a word is missing.
        ``variants``: the tool's variant values offered on this build (searchable too)."""
        matched, total = self.match(query, variants)
        return total if matched == len(query) else None

    def match(self, query: list[str], variants=()) -> tuple[int, int]:
        """``(matched words, score)``: field weights, phrase and domain bonuses, minus
        ``MISSING`` per unmatched word."""
        fields = dict(self.fields)
        fields["variant"] = {w for v in variants for w in words(v)}
        total = matched = 0
        for word in query:
            synonyms = [stem(s) for s in SYNONYMS.get(word, ())]
            best = 0
            for field, tokens in fields.items():
                if any(_match(word, t) for t in tokens):
                    best = max(best, WEIGHTS[field])
                elif any(_match(s, t) for s in synonyms for t in tokens):
                    best = max(best, WEIGHTS[field] // 2)  # a synonym ranks below the word
            if best:
                matched += 1
                total += best
            else:
                total -= MISSING
        text = " " + " ".join(query) + " "
        total += sum(PHRASE for p in self.phrases if f" {p} " in text)
        if any(DOMAIN_WORDS.get(w) == self.domain for w in query):
            total += DOMAIN
        return matched, total


def _match(word: str, token: str) -> bool:
    return token == word or (len(word) >= 3 and token.startswith(word))


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def query_words(query: str) -> list[str]:
    """Stemmed query words without stop words (all of them if every word is one)."""
    found = words(query)
    return [w for w in found if w not in STOP_WORDS] or found


def search(entries, query: str, domain=None, effect=None, variants=None) -> list:
    """Ranked entries; ``variants(entry)``: the entry's offered variant values (None: not
    searched). See :func:`ranked` for the partial flag."""
    return ranked(entries, query, domain, effect, variants)[0]


def ranked(entries, query: str, domain=None, effect=None, variants=None) -> tuple[list, bool]:
    """``(entries, partial)``, best score first: tools matching every query word, plus tools
    matching at least half of them that outscore the best full match (a strong name match
    beats a word found only in a description). ``partial``: no tool matched every word."""
    wanted = query_words(query)
    exact = query.strip()
    full, part = [], []
    for entry in entries:
        if (domain and entry.domain != domain) or (effect and entry.effect != effect):
            continue
        if entry.name == exact:
            full.append((-(10**6), entry))
            continue
        matched, score = entry.match(wanted, variants(entry) if variants else ())
        if not wanted or matched == len(wanted):
            full.append((-score, entry))
        elif matched and 2 * matched >= len(wanted):
            part.append((-score, entry))
    best = min((key for key, _ in full), default=None)
    chosen = full + [item for item in part if best is None or item[0] <= best]
    chosen.sort(key=lambda item: (item[0], item[1].name))
    return [entry for _, entry in chosen], not full and bool(part)


def close_matches(name: str, known) -> list[str]:
    return difflib.get_close_matches(str(name), list(known), n=3, cutoff=0.6)


def unknown_tool(name: str, known, *, field: str | None = "/tool") -> NXError:
    matches = close_matches(name, known)
    return NXError(
        "NX_UNKNOWN_TOOL",
        f"Unknown tool: {name}",
        field=field,
        hint=("Did you mean " + ", ".join(matches) + "? " if matches else "")
        + "Find tools with nx_find_tools.",
        details={"close_matches": matches},
    )


# ---------------------------------------------------------------------------
# Gateway argument models


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FindTools(_Strict):
    query: Annotated[
        str,
        Field(
            max_length=120,
            description="Task words, e.g. 'fillet edges'; empty: an overview of the domains",
        ),
    ] = ""
    domain: Domain | None = None
    effect: Effect | None = None
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0
    limit: Annotated[int, Field(ge=1, le=20)] = 10


class GetToolSchemas(_Strict):
    names: Annotated[
        list[
            Annotated[
                str, Field(max_length=100, pattern=r"^nx_[a-z0-9_]{1,80}(#[a-z0-9_]{1,40})?$")
            ]
        ],
        Field(
            min_length=1, max_length=10, description="Tool names; 'tool#variant' for one variant"
        ),
    ]
    include_output: bool = False


class Invoke(_Strict):
    tool: Annotated[str, Field(max_length=100, pattern=TOOL_NAME)]
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments; own operation_id (to retry a mutation): 8-128 of A-Za-z0-9_-",
    )
    intent: Annotated[
        str | None,
        Field(max_length=120, description="Why, in the engineer's terms (shown in the NX panel)"),
    ] = None
    detail: Literal["compact", "full"] = "compact"


class InvokeRead(_Strict):
    tool: Annotated[str, Field(max_length=100, pattern=TOOL_NAME)]
    arguments: dict[str, Any] = Field(default_factory=dict)
    detail: Literal["compact", "full"] = "compact"


MODELS: dict[str, type[BaseModel]] = {
    "nx_find_tools": FindTools,
    "nx_get_tool_schemas": GetToolSchemas,
    "nx_invoke": Invoke,
    "nx_invoke_read": InvokeRead,
}
DESCRIPTIONS = {
    "nx_find_tools": (
        "Search NX tools by task words (synonyms count: fillet finds blend). Rows: name, "
        "summary, effect, domain. Fetch schemas with nx_get_tool_schemas, then call the tools "
        "through nx_invoke (nx_invoke_read for effect read), not as MCP tools."
    ),
    "nx_get_tool_schemas": (
        "Exact input schemas, examples, prerequisites and evidence for 1-10 tools; "
        "'tool#variant' narrows a tool to one variant. Output schemas on request."
    ),
    "nx_invoke": (
        "Call any NX tool with its exact arguments; the kernel checks and undoes failures. "
        "operation_id is optional (made when omitted). intent is shown in the NX panel. "
        "Compact results keep result_id (expand with nx_get_result)."
    ),
    "nx_invoke_read": (
        "Call a read-only NX tool (effect read) with its exact arguments; any other tool is "
        "refused before validation. Safe to auto-approve."
    ),
}


def tool_schema(name: str) -> dict:
    out = schema.tidy(MODELS[name].model_json_schema())
    for key, prop in list(out.get("properties", {}).items()):
        # An optional field is documented by omission, not by null (as for catalogue tools).
        prop = schema._without_null(prop)
        if "default" in prop and prop["default"] is None:
            prop = {k: v for k, v in prop.items() if k != "default"}
        out["properties"][key] = prop
    return out
